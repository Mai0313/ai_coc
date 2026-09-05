from __future__ import annotations

from typing import TYPE_CHECKING, Any, get_args
import logging
from contextlib import ExitStack

from PyQt5.QtGui import QPixmap, QDesktopServices
from PyQt5.QtCore import Qt, QUrl, QTimer, QSettings, QThreadPool
from PyQt5.QtWidgets import (
    QLabel,
    QWidget,
    QSpinBox,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QLineEdit,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QTableWidget,
    QTextBrowser,
    QTableWidgetItem,
)

from ai_coc import commands
from ai_coc.models import (
    Frame,
    RunLog,
    WallOptions,
    AttackSeries,
    AttackOptions,
    DisplayTarget,
    DonateOptions,
    GeminiSetting,
    ThinkingLevel,
    VillageExport,
    LootThresholds,
    UpgradeOptions,
    EmulatorInstance,
)
from ai_coc.constants import (
    LOG_DIR,
    APP_NAME,
    COC_PACKAGE,
    ORGANISATION,
    VERSION_LABEL,
    DEFAULT_GEMINI_MODEL,
)
from ai_coc.adapters.ai import GeminiClient
from ai_coc.adapters.mumu import MuMuAdapter
from ai_coc.logging_setup import configure_logging
from ai_coc.adapters.config import ConfigStore
from ai_coc.adapters.secrets import SecretStore

from .workers import Worker, LogBridge, UiLogHandler

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

logger = logging.getLogger(__name__)

RUN_LABEL_START = "▶  開始自動化"
RUN_LABEL_STOP = "■  停止"
RUN_BUTTON_IDLE = "background:#1f9d55;font-size:12pt;font-weight:bold;border-radius:8px"
RUN_BUTTON_RUNNING = "background:#c2410c;font-size:12pt;font-weight:bold;border-radius:8px"
# A finished job queues the next pass straight away, so the loop runs back to
# back; the interval timer is only what retries when a pass had nothing to do.
NEXT_CYCLE_DELAY = 3000
# Two frames a second: enough to watch an attack unfold, and slow enough that the
# preview's own `screencap` does not compete with the loop taking the frames it
# reads. A tick whose frame is still in flight is dropped rather than queued.
LIVE_INTERVAL = 500
# Taken from the model so the picker cannot drift from what Gemini accepts.
THINKING_LEVELS = list(get_args(ThinkingLevel))


class MainWindow(QMainWindow):
    def __init__(self, session: RunLog | None = None) -> None:
        super().__init__()
        # The run `main()` opened for the window itself. Every job opens one of
        # its own and hands the log back here when it ends, so without this the
        # window's own lines — the preview, the chat, the next cycle's setup —
        # would go to no file at all between jobs, which on the packaged build
        # means the in-memory panel and nothing else.
        self.session = session
        self.setWindowTitle(f"{APP_NAME} — {VERSION_LABEL}")
        self.resize(1260, 820)
        self.pool = QThreadPool.globalInstance()
        self.secrets = SecretStore()
        self.settings = QSettings(ORGANISATION, "CoCAIController")
        self.config = ConfigStore().load()
        self.mumu: MuMuAdapter | None = None
        self.instances: list[EmulatorInstance] = []
        self.active: EmulatorInstance | None = None
        self.current_frame: Frame | None = None
        self.frame_sequence = 0
        self.current_account_tag = ""
        # One flag for "a pass is in flight", because every job in the cycle is
        # now a `commands.*` call and they all share one emulator: two of them
        # running at once interleave taps on the same display. It replaces the
        # pair that used to do this — a task-row id and an attack-only bool —
        # which needed two only because the upkeep jobs went through the agent
        # loop and the attack did not.
        self.job_running = False
        # Read from the worker threads as well as the UI one, so it is a plain
        # bool rather than the timer's own state: stopping has to reach the loop
        # that is playing, not just the next scheduled cycle.
        self.automation_active = False
        # Whether `_stopping` has seen a stop in the state file. Latched rather
        # than read again later, because the claim around each pass writes
        # `idle` back on the way out, so the `stopping` a terminal asked for is
        # gone by the time the pass returns.
        self.stop_seen = False
        # The claim the automation holds for its whole run, kept as a field
        # because it spans two methods: `start_automation` takes it and
        # `stop_automation` hands it back. Each pass's own claim nests inside
        # this one and does nothing, which is what keeps the state file on
        # `running` through the gaps between passes instead of flickering.
        self.holding = ExitStack()
        self.automation_timer = QTimer(self)
        self.automation_timer.timeout.connect(self.automation_cycle)
        self.automation_step = 0
        # The preview polls `screencap`, so the display is resolved once and kept
        # rather than paying two `dumpsys` calls for every frame; a failed frame
        # drops it so the next tick looks the game up again.
        self.live_display: DisplayTarget | None = None
        self.live_busy = False
        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self._live_tick)
        self._build_ui()
        self._attach_log_panel()
        self.statusBar().showMessage("Ready — 偵測 MuMu 以開始")
        self.refresh_instances()
        # Straight rather than through a worker: it reads one file this machine
        # already has, and the table is worth having filled before the emulator
        # has even been found.
        self._show_export(commands.export(last=True))
        if self.live_view.isChecked():
            self.live_timer.start(LIVE_INTERVAL)

    def _build_ui(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #121722; color: #e8edf7; font-size: 10pt; }
            QTabWidget::pane { border: 1px solid #2b3548; background: #151c2a; }
            QTabBar::tab { background: #1d2636; color: #aebbd0; padding: 10px 18px; border: 0; }
            QTabBar::tab:selected { background: #367bf5; color: white; }
            QGroupBox { border: 1px solid #33415a; border-radius: 10px; margin-top: 14px; padding: 16px 12px 12px; font-weight: bold; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #70a4ff; }
            QPushButton { background: #2b6de0; color: white; border: 0; border-radius: 6px; padding: 8px 14px; }
            QPushButton:hover { background: #4385f2; } QPushButton:pressed { background: #1d54b6; }
            QLineEdit, QTextBrowser, QComboBox, QTableWidget { background: #0e141f; color: #e8edf7; border: 1px solid #34435d; border-radius: 6px; padding: 6px; }
            QHeaderView::section { background: #243149; color: #dbe6fa; padding: 6px; border: 0; }
            QStatusBar { background: #0d121b; color: #91a3c0; }
        """)
        central = QWidget()
        central_layout = QVBoxLayout(central)
        tabs = QTabWidget()
        tabs.addTab(self._control_tab(), "主控")
        tabs.addTab(self._account_tab(), "帳號進度")
        tabs.addTab(self._settings_tab(), "設定")
        tabs.addTab(self._about_tab(), "關於")
        self.tabs = tabs
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(tabs)
        splitter.addWidget(self._log_panel())
        splitter.setSizes([620, 200])
        central_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

    def _log_panel(self) -> QGroupBox:
        self.log_view = QTextBrowser()
        self.log_view.document().setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(110)
        group = QGroupBox("執行紀錄")
        layout = QVBoxLayout(group)
        row = QHBoxLayout()
        self.log_level = QComboBox()
        self.log_level.addItems(["INFO", "DEBUG", "WARNING", "ERROR"])
        self.log_level.setToolTip("DEBUG 會額外記下送給 AI 的完整提示與回覆")
        self.log_level.currentTextChanged.connect(self._set_log_level)
        clear = QPushButton("清除")
        clear.clicked.connect(self.log_view.clear)
        open_log = QPushButton("開啟記錄資料夾")
        # The directory rather than one file: every run keeps its own, named
        # `<when>-<what>`, so the listing is the history and the newest is the
        # one at the top.
        open_log.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR)))
        )
        row.addWidget(QLabel("等級"))
        row.addWidget(self.log_level)
        row.addWidget(clear)
        row.addWidget(open_log)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(self.log_view)
        return group

    def _attach_log_panel(self) -> None:
        """Route the root logger into the panel; workers emit from other threads."""
        self.log_bridge = LogBridge()
        self.log_bridge.message.connect(self._append_log)
        logging.getLogger().addHandler(UiLogHandler(self.log_bridge))
        logger.info("%s %s started, logs under: %s", APP_NAME, VERSION_LABEL, LOG_DIR)

    def _append_log(self, html: str) -> None:
        """Append one rich-rendered record, without yanking the view off what is being read."""
        bar = self.log_view.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 4
        self.log_view.append(html)
        if follow:
            bar.setValue(bar.maximum())

    def _set_log_level(self, level: str) -> None:
        logging.getLogger().setLevel(getattr(logging, level, logging.INFO))
        logger.warning("Log level is now %s", level)

    def _emulator_group(self) -> QGroupBox:
        group = QGroupBox("模擬器")
        layout = QVBoxLayout(group)
        self.instance_combo = QComboBox()
        # **This picks what the buttons below and the preview act on, and not
        # what the automation drives.** Every job in the cycle is a `commands.*`
        # call and `_controller()` takes the first instance MuMu lists, so with
        # more than one the window would be watching one emulator and driving
        # another with nothing on screen saying so. Said here rather than fixed
        # by threading an index through eight command signatures, because this
        # account runs one instance and the two agree wherever that holds.
        self.instance_combo.setToolTip(
            "選擇下面幾個按鈕跟即時畫面要看哪一個模擬器。自動化一律驅動 MuMu 列出的第一個"
        )
        self.instance_combo.currentIndexChanged.connect(self._select_instance)
        layout.addWidget(self.instance_combo)
        toolbar = QHBoxLayout()
        for text, fn in (
            ("重新整理狀態", self.refresh_instances),
            ("一鍵啟動遊戲", self.launch_coc),
            ("擷取目前畫面", self.capture),
            ("關閉模擬器", self.close_emulator),
        ):
            button = QPushButton(text)
            button.clicked.connect(fn)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        return group

    def _control_tab(self) -> QWidget:
        """Everything needed to start a run, on one page: pick the emulator, say
        what it should do, press the button bottom right.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self._emulator_group())
        left_layout.addWidget(self._automation_behavior_group())
        left_layout.addWidget(self._automation_battle_group())
        left_layout.addStretch()
        right = QWidget()
        right_layout = QVBoxLayout(right)
        self._preview_switches(right_layout)
        self.frame_label = QLabel("尚無畫面")
        self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setMinimumSize(520, 300)
        self.frame_label.setStyleSheet("background:#16181d;color:#bbb;border:1px solid #444")
        right_layout.addWidget(self.frame_label)
        right_layout.addStretch(1)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([520, 720])
        layout.addWidget(splitter, 1)
        footer = QHBoxLayout()
        save = QPushButton("儲存設定")
        save.clicked.connect(self.save_automation)
        footer.addWidget(save)
        footer.addStretch()
        self.run_button = QPushButton(RUN_LABEL_START)
        self.run_button.setMinimumSize(200, 46)
        self.run_button.setStyleSheet(RUN_BUTTON_IDLE)
        self.run_button.clicked.connect(self.toggle_automation)
        footer.addWidget(self.run_button)
        layout.addLayout(footer)
        return page

    def _account_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        button = QPushButton("取得村莊資訊")
        button.clicked.connect(self.run_export)
        self.account_label = QLabel("尚未匯入帳號")
        row.addWidget(button)
        row.addWidget(self.account_label)
        row.addStretch()
        layout.addLayout(row)
        # The five the export actually carries. The other five this table used
        # to have came from `entity_levels`, which is empty by a stated product
        # decision and so drew `—` on every row it ever showed.
        self.account_table = QTableWidget(0, 5)
        self.account_table.setHorizontalHeaderLabels([
            "Section",
            "Data ID",
            "Name",
            "Level",
            "Count",
        ])
        self.account_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.account_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.account_table)
        return page

    def _preview_switches(self, layout: QVBoxLayout) -> None:
        """The two switches over the preview, in their own method for length.

        What they have in common is that each one is a mode this window is in
        rather than a permission it grants, which is what separates them from
        the 自主行為 group on the other side of the splitter.
        """
        self.live_view = QCheckBox("即時畫面")
        self.live_view.setToolTip("每半秒抓一張 CoC 畫面；關掉之後這裡只會顯示手動擷取的截圖")
        self.live_view.setChecked(str(self.settings.value("live_view", "true")).lower() == "true")
        self.live_view.toggled.connect(self._toggle_live_view)
        layout.addWidget(self.live_view)
        # Off by default, and for a different reason than 即時畫面: this one keeps
        # every frame a loop reads on disk, which is what turns a battle that
        # went wrong into something anyone can look at afterwards. It costs the
        # emulator a PNG encode per read and fills a directory per run.
        self.record_frames = QCheckBox("保留這次的畫面")
        self.record_frames.setToolTip(
            "把每一輪讀到的畫面存進 ~/.ai_coc/logs 底下這次執行的資料夾，"
            "事後可以逐張看它當時看到什麼；會多花一些硬碟跟模擬器的時間"
        )
        self.record_frames.setChecked(
            str(self.settings.value("record_frames", "false")).lower() == "true"
        )
        self.record_frames.toggled.connect(self._toggle_record_frames)
        layout.addWidget(self.record_frames)

    def _automation_behavior_group(self) -> QGroupBox:
        behavior = QGroupBox("自主行為")
        form = QFormLayout(behavior)
        self.auto_collect = QCheckBox("自主收取主村資源")
        self.auto_donate = QCheckBox("自主開啟部落聊天室並捐兵")
        self.auto_upgrade = QCheckBox("自主安排並執行建築升級")
        self.auto_walls = QCheckBox("自主刷牆")
        self.auto_attack = QCheckBox("自主搜尋對手並打資源")
        for key, widget in (
            ("auto_collect", self.auto_collect),
            ("auto_donate", self.auto_donate),
            ("auto_upgrade", self.auto_upgrade),
            ("auto_walls", self.auto_walls),
            ("auto_attack", self.auto_attack),
        ):
            widget.setChecked(str(self.settings.value(key, "false")).lower() == "true")
            form.addRow(widget)
        return behavior

    def _automation_battle_group(self) -> QGroupBox:
        battle = QGroupBox("進攻與資源門檻")
        battle_form = QFormLayout(battle)
        self.min_gold = QSpinBox()
        self.min_gold.setRange(0, 2000000)
        self.min_gold.setSingleStep(50000)
        self.min_elixir = QSpinBox()
        self.min_elixir.setRange(0, 2000000)
        self.min_elixir.setSingleStep(50000)
        self.min_dark = QSpinBox()
        self.min_dark.setRange(0, 50000)
        self.min_dark.setSingleStep(500)
        # One row for both villages: it is a share of what each storage actually
        # holds, and the loop reads those ceilings off the bars themselves.
        self.stop_at = QSpinBox()
        self.stop_at.setRange(0, 100)
        self.stop_at.setSuffix(" %")
        # The row text only names the resource, so what the number means lives here.
        for box in (self.min_gold, self.min_elixir, self.min_dark):
            box.setToolTip("對手身上至少要有這麼多，才值得出手")
        # 0 is the minimum, so this labels it in place rather than in the row text.
        self.stop_at.setSpecialValueText("不監控")
        self.stop_at.setToolTip("每一種資源都滿到這個比例就停止刷資源，主村跟夜世界共用")
        self.cycle_minutes = QSpinBox()
        self.cycle_minutes.setRange(1, 120)
        for widget, value in (
            (self.min_gold, self.config.thresholds.min_gold),
            (self.min_elixir, self.config.thresholds.min_elixir),
            (self.min_dark, self.config.thresholds.min_dark),
            (self.stop_at, self.config.stop_at),
            # Only the window ever waits, so this one stays in the registry.
            (self.cycle_minutes, int(self.settings.value("cycle_minutes", 10))),
        ):
            widget.setValue(value)
        battle_form.addRow("對手金幣", self.min_gold)
        battle_form.addRow("對手聖水", self.min_elixir)
        battle_form.addRow("對手黑水", self.min_dark)
        battle_form.addRow("儲量停手", self.stop_at)
        battle_form.addRow("閒置重試（分鐘）", self.cycle_minutes)
        return battle

    def save_automation(self) -> None:
        for key, widget in (
            ("auto_collect", self.auto_collect),
            ("auto_donate", self.auto_donate),
            ("auto_upgrade", self.auto_upgrade),
            ("auto_walls", self.auto_walls),
            ("auto_attack", self.auto_attack),
        ):
            self.settings.setValue(key, widget.isChecked())
        self.settings.setValue("cycle_minutes", self.cycle_minutes.value())
        self._save_config(thresholds=self._thresholds(), stop_at=self.stop_at.value())
        logger.info("Automation settings saved")

    def _save_config(self, **changes: object) -> None:
        """Change part of the shared config and write the whole of it back.

        The window keeps reading its own widgets while it runs, so this is only
        about what the next run finds — including the next one started from a
        terminal, which has nowhere else to look.
        """
        self.config = self.config.model_copy(update=changes)
        ConfigStore().save(self.config)

    def toggle_automation(self) -> None:
        if self.automation_active:
            self.stop_automation()
        else:
            self.start_automation()

    def _paint_run_button(self) -> None:
        running = self.automation_active
        self.run_button.setText(RUN_LABEL_STOP if running else RUN_LABEL_START)
        self.run_button.setStyleSheet(RUN_BUTTON_RUNNING if running else RUN_BUTTON_IDLE)

    def _queue_next_cycle(self) -> None:
        """Start the next pass as soon as this one finishes, so the loop is
        continuous rather than paced by the idle timer.
        """
        if self.automation_active:
            QTimer.singleShot(NEXT_CYCLE_DELAY, self.automation_cycle)

    def start_automation(self) -> None:
        self.save_automation()
        self.automation_active = True
        # Both halves of the stop, taken back for the reason the old `clear_stop`
        # gave: whichever of them a stopped run left behind would stand this one
        # down before it had done anything, reported as a stop nobody asked for.
        # **The claim belongs here and not only inside each pass**, because
        # `_stood_down` runs before any job does — measured on the flag this
        # replaced, one left by `ai_coc stop` at the end of a farming session
        # made every press of this button stand down on the spot, with nothing
        # ever reaching the pass that would have cleared it.
        #
        # Held for the whole run rather than per pass, so the state file reads
        # `running` across the three seconds between one pass and the next: each
        # pass's own claim nests inside this one and does nothing.
        self.stop_seen = False
        self.holding.close()
        self.holding.enter_context(commands.claim("automation", self.session.directory))
        self.automation_timer.start(self.cycle_minutes.value() * 60000)
        self._paint_run_button()
        logger.info("自動化已啟動,第一輪開始")
        QTimer.singleShot(300, self.automation_cycle)

    def stop_automation(self) -> None:
        self.automation_active = False
        self.automation_timer.stop()
        # A pass already in flight keeps the claim until it really ends, since
        # this stops the *next* one from starting rather than the battle under
        # way — `_run_job`'s `finished` hands it back at that point. With
        # nothing in flight there is no later moment, so it goes back here.
        if not self.job_running:
            self.holding.close()
        self._paint_run_button()
        logger.info("已停止:不再開始新的一輪,已經開打的這一場會打完再回營")

    def _stopping(self) -> bool:
        """Whether the pass that is running should stand down.

        Both sources, because a run started here answers to two: this window's
        own button, and `ai_coc stop`, which is the only thing that reaches a
        run from outside the process. Reading the state file here is only safe
        because `start_automation` claims before any pass runs — a window
        reading it with nothing of its own written down would take a `stopping`
        left by an earlier terminal run, or the file's own absence on a fresh
        machine, and stand itself down the moment it started.

        **That claim is also why this is latched rather than read again later.**
        Handing the emulator back writes `idle`, so the `stopping` a terminal
        asked for is gone by the time the pass ends — and without the latch that
        stop would end one pass while the cycle started the next one
        `NEXT_CYCLE_DELAY` later, which is a stop that did not stop anything.
        This runs on a worker thread, so it only records; `_stood_down` is where
        the UI thread acts on it.
        """
        if commands.stop_requested():
            self.stop_seen = True
        return self.stop_seen or not self.automation_active

    def _stood_down(self) -> bool:
        """Switch the automation off for a stop that came from outside, once.

        False when the button was what stopped it, since `stop_automation` has
        already run and said so.
        """
        if not self._stopping() or not self.automation_active:
            return False
        logger.info("收到外部的停止要求(ai_coc stop)")
        self.stop_automation()
        self.stop_seen = False
        return True

    def automation_cycle(self) -> None:
        if not self.automation_active or self.job_running or not self.active:
            return
        # A stop that arrived between passes rather than during one; the pass
        # itself answers `_stopping` on its own thread.
        if self._stood_down():
            return
        jobs: list[Callable[[], None]] = [
            job
            for enabled, job in (
                (self.auto_collect.isChecked(), self.run_collect),
                (self.auto_donate.isChecked(), self.run_donate),
                (self.auto_upgrade.isChecked(), self.run_upgrade),
                (self.auto_walls.isChecked(), self.run_walls),
                (self.auto_attack.isChecked(), self.run_attack),
            )
            if enabled
        ]
        if not jobs:
            logger.info("巡檢完成:尚未啟用任何自主行為")
            return
        job = jobs[self.automation_step % len(jobs)]
        self.automation_step += 1
        job()

    def _run_job(
        self,
        label: str,
        what: str,
        task: Callable[[RunLog], BaseModel],
        done: Callable[[Any], None] | None = None,
    ) -> None:
        """Run one pass of the cycle as the same function the CLI runs.

        Every job here is a `commands.*` call, which is what keeps the window
        and the terminal playing the same way: read that module's own docstring
        — the only thing the window ever provided was the wiring, and those
        functions are that wiring. So what is left for the window is the three
        things a terminal gets from `cli.py` instead: this pass's own run
        directory, handing the log back afterwards, and starting the next pass.

        `RunLog.open` and `configure_logging` are called here rather than inside
        the worker because both belong on the thread that owns the widgets this
        reads. `done` defaults to printing the report's own message, which is
        what four of the five jobs want.
        """
        run = RunLog.open(what, recording=self.record_frames.isChecked())
        configure_logging(run)
        self.job_running = True

        def answered(report: Any) -> None:  # noqa: ANN401 - whichever report model the job returns
            # Beside its own log, so a pass started from the window leaves the
            # same evidence a headless one does.
            run.answer(report.model_dump_json(indent=2))
            if done:
                done(report)
            else:
                # The message alone, because `label` is already the line above
                # it: together they read 正在收取採集器… 收了 8 個採集器.
                logger.info("%s", report.message)

        def finished() -> None:
            self.job_running = False
            # This run is over, so the log goes back to the window's own.
            # Without that every later line the window writes — the preview, the
            # next cycle's own setup — keeps landing in a finished pass's
            # `run.log`, which is the one file someone opens to reconstruct that
            # pass; and handing back None instead would leave them nowhere.
            configure_logging(self.session)
            if not self._stood_down():
                self._queue_next_cycle()
            # The automation is over and this was its last pass, so the claim it
            # held across the whole cycle goes back now rather than when the
            # button was pressed — that moment left a battle still playing.
            # Closing an already-closed stack is a no-op, and a one-off job
            # never opened this one at all: its claim lives and dies in
            # `driving` above.
            if not self.automation_active:
                self.holding.close()

        def driving() -> BaseModel:
            # Around the job rather than around `_run_job`, because everything
            # above this runs on the UI thread while the job itself is the part
            # that reaches the emulator. A pass of the automation cycle finds
            # the claim `start_automation` already took and leaves it alone, so
            # the state file reads `running` across the three seconds between
            # one pass and the next instead of flickering to `idle` in each gap.
            with commands.claim(what, run.directory):
                return task(run)

        logger.info("%s", label)
        self.run_async(label, driving, answered, finished)

    def run_collect(self) -> None:
        self._run_job("正在收取採集器…", "collect", lambda run: commands.collect(run.frames))

    def run_donate(self) -> None:
        self._run_job(
            "正在檢查部落增援請求…",
            "donate",
            lambda run: commands.donate(DonateOptions(frame_dir=run.frames)),
        )

    def run_upgrade(self) -> None:
        self._run_job(
            "正在安排建築升級…",
            "upgrade",
            lambda run: commands.upgrade(UpgradeOptions(frame_dir=run.frames), self._stopping),
        )

    def run_walls(self) -> None:
        # One batch per pass, for the reason `run_attack` takes one round: the
        # cycle is what decides what comes next, and `WallOptions.rounds` of 0 —
        # the CLI's default, where somebody is deliberately spending the loot —
        # keeps buying until neither storage will pay for another wall. That is
        # the whole session rather than a pass, and the collectors, the builders
        # and the attack would wait it out. A batch is up to `MAX_BATCH` walls,
        # so this is not a pass that barely does anything.
        self._run_job(
            "正在升級城牆…",
            "walls",
            lambda run: commands.walls(
                WallOptions(frame_dir=run.frames, rounds=1), self._stopping
            ),
        )

    def _thresholds(self) -> LootThresholds:
        return LootThresholds(
            min_gold=self.min_gold.value(),
            min_elixir=self.min_elixir.value(),
            min_dark=self.min_dark.value(),
        )

    def run_attack(self) -> None:
        def done(series: AttackSeries) -> None:
            if not series.root:
                logger.info("這一輪沒有打成任何一場")
                return
            report = series.root[-1]
            # The storage is full, so the next pass would only read it again and
            # come back here. Stopping is the whole point of the threshold. The
            # skip count is left out of this one: it returns before any opponent
            # is scouted, and this is the only line saying why the automation
            # switched itself off.
            if report.stock_full:
                logger.info("%s", report.message)
                self.stop_automation()
                return
            logger.info("進攻巡檢結束(跳過 %d 個對手):%s", report.skipped, report.message)

        # One round per pass, because the cycle is what decides what comes next:
        # `commands.attack` would otherwise keep playing and the collectors,
        # the builders and the walls would never get their turn. Thresholds,
        # the storage share and the planner all come out of the shared config
        # file in there, which is what `save_automation` writes on the way in.
        self._run_job(
            "AI 正在搜尋對手並進攻…",
            "attack",
            lambda run: commands.attack(
                AttackOptions(frame_dir=run.frames, plan_log=run.plan_log, rounds=1),
                self._stopping,
            ),
            done,
        )

    def _settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        group = QGroupBox("AI／API 設定")
        form = QFormLayout(group)
        self.provider_combo = QComboBox()
        self.provider_combo.addItem("Google Gemini")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("Stored with Windows DPAPI")
        self.model_combo = QComboBox()
        self.model_combo.addItem(self.config.gemini.main.model or DEFAULT_GEMINI_MODEL)
        self.model_combo.setToolTip("按「測試連線並載入模型」後會列出這把金鑰可用的文字模型")
        self.endpoint = QLineEdit(self.config.gemini.main.base_url)
        self.endpoint.setPlaceholderText("留空即使用 Google 官方端點")
        # Every call this app makes is a screen read against a fixed prompt, so
        # the thinking budget mostly buys latency. It is a picker rather than a
        # constant because a model that refuses the level, or a prompt that turns
        # out to want the reasoning, should be one choice away from working.
        self.thinking_combo = QComboBox()
        self.thinking_combo.addItems(THINKING_LEVELS)
        self.thinking_combo.setCurrentText(self.config.gemini.main.thinking_level)
        self.thinking_combo.setToolTip(
            "模型回答前思考多久。進攻計畫是看圖判讀,low 已經夠用而且快得多"
        )
        try:
            self.api_key.setText(self.secrets.load())
        except Exception:
            logger.debug("Unable to load the saved API key", exc_info=True)
        form.addRow("Provider", self.provider_combo)
        form.addRow("API Key", self.api_key)
        form.addRow("Model", self.model_combo)
        form.addRow("思考程度", self.thinking_combo)
        form.addRow("Endpoint", self.endpoint)
        buttons = QHBoxLayout()
        for text, fn in (
            ("儲存設定", self.save_api),
            ("測試連線並載入模型", self.test_api),
            ("清除 API Key", self.clear_api),
        ):
            b = QPushButton(text)
            b.clicked.connect(fn)
            buttons.addWidget(b)
        form.addRow(buttons)
        layout.addWidget(group)
        layout.addStretch()
        return page

    def _about_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        text = QLabel(f"<h1>{APP_NAME}</h1><p>{VERSION_LABEL}</p>")
        text.setTextFormat(Qt.RichText)
        layout.addWidget(text)
        layout.addStretch()
        return page

    def run_async(
        self,
        label: str,
        fn: Callable[[], Any],
        done: Callable[[Any], None] | None = None,
        finished: Callable[[], None] | None = None,
    ) -> None:
        logger.info("Started: %s", label)
        self.statusBar().showMessage(label)
        worker = Worker(fn, label)
        if done:
            worker.signals.result.connect(done)
        if finished:
            # Runs whether the work succeeded or raised, unlike `done`.
            worker.signals.finished.connect(finished)
        worker.signals.error.connect(lambda message: self._error(label, message))
        worker.signals.finished.connect(lambda: self.statusBar().showMessage("Ready"))
        self.pool.start(worker)

    def _error(self, title: str, message: str) -> None:
        logger.error("%s: %s", title, message)
        QMessageBox.critical(self, title, message)

    def refresh_instances(self) -> None:
        def task() -> tuple[MuMuAdapter, str, list[EmulatorInstance]]:
            adapter = MuMuAdapter()
            return adapter, adapter.version(), adapter.enumerate_instances()

        def done(result: tuple[MuMuAdapter, str, list[EmulatorInstance]]) -> None:
            self.mumu, version, self.instances = result
            self.instance_combo.blockSignals(True)
            self.instance_combo.clear()
            for item in self.instances:
                self.instance_combo.addItem(
                    f"{item.name} — {item.state} — ADB {item.adb_serial}", item.emulator_id
                )
            self.instance_combo.blockSignals(False)
            if self.instances:
                self.instance_combo.setCurrentIndex(0)
                self._select_instance(0)
            self.statusBar().showMessage(
                f"MuMu {version}: {len(self.instances)} instance(s)", 6000
            )

        self.run_async("Detecting MuMu instances…", task, done)

    def _select_instance(self, index: int) -> None:
        """Which emulator the buttons and the preview act on.

        It used to also fill a panel of thirteen fields — window handles, pid,
        dpi, the account tag — under the dropdown. What the dropdown itself
        shows is the part anyone reads, and the rest is in the run log.
        """
        if 0 <= index < len(self.instances):
            self.active = self.instances[index]

    def _require(self) -> tuple[MuMuAdapter, EmulatorInstance]:
        if not self.mumu or not self.active:
            raise RuntimeError("請先選擇 MuMu instance")
        return self.mumu, self.active

    def close_emulator(self) -> None:
        m, a = self._require()
        self.run_async(
            "Closing MuMu…", lambda: m.close_instance(a.index), lambda _: self.refresh_instances()
        )

    def launch_coc(self) -> None:
        m, a = self._require()
        self.run_async(
            "正在自動啟動部落衝突…",
            lambda: m.ensure_coc(a.index),
            lambda _: self.refresh_instances(),
        )

    def _paint_frame(self, png: bytes) -> None:
        pix = QPixmap()
        pix.loadFromData(png)
        self.frame_label.setPixmap(
            pix.scaled(self.frame_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def _toggle_live_view(self, on: bool) -> None:
        self.settings.setValue("live_view", on)
        if on:
            self.live_timer.start(LIVE_INTERVAL)
        else:
            self.live_timer.stop()

    def _toggle_record_frames(self, on: bool) -> None:
        # Nothing but the setting: what reads it is `run_attack`, when it opens
        # the run. A round already under way keeps whatever it started with.
        self.settings.setValue("record_frames", on)

    def _live_tick(self) -> None:
        """Put one fresh frame in the preview, unless the last one is still in flight.

        Everything here is deliberately silent. It runs twice a second whether
        anyone is watching or not, so an emulator that is not up yet must not
        raise a message box the way `run_async` would, and a stale display id
        must clear itself rather than need a restart.
        """
        if self.live_busy or not self.mumu or not self.active:
            return
        m, a = self.mumu, self.active
        self.live_busy = True

        def frame() -> bytes | None:
            try:
                adb = m.controller(a.adb_serial)
                if self.live_display is None:
                    self.live_display = adb.display_for(COC_PACKAGE)
                return adb.screenshot(self.live_display)
            except Exception:
                self.live_display = None
                logger.debug("Live preview frame failed", exc_info=True)
                return None

        def done(png: bytes | None) -> None:
            if png:
                self._paint_frame(png)

        def released() -> None:
            self.live_busy = False

        worker = Worker(frame, "live preview")
        worker.signals.result.connect(done)
        # On `finished` rather than `result`: a worker that raised emits `error`
        # and never `result`, and the flag left set would stop every later tick
        # at the guard, freezing the preview for the rest of the session.
        worker.signals.finished.connect(released)
        self.pool.start(worker)

    def capture(self) -> None:
        m, a = self._require()

        def done(png: bytes) -> None:
            self.frame_sequence += 1
            self.current_frame = Frame.create(
                a.emulator_id, self.current_account_tag, png, self.frame_sequence
            )
            self._paint_frame(png)
            self.statusBar().showMessage(f"Captured {self.current_frame.frame_id}", 7000)

        self.run_async("Capturing current MuMu frame…", lambda: m.screenshot(a), done)

    def run_export(self) -> None:
        """Get the village out of the game, through the same call a terminal makes.

        **The only `_run_job` a widget can start**, and therefore the only one
        that has to check `job_running` itself: the other five are reached from
        `automation_cycle`, which returns early on that flag before it gets
        here. Without this, pressing the button during an automation pass puts
        two passes on one display — and the one that finishes first clears the
        flag and points the log back at the window's own run, so the next tick
        starts a third on top of the one still going.
        """
        if self.job_running:
            # The status bar rather than `account_label`: that label is now the
            # only thing saying which account the table below belongs to, and a
            # notice written over it stays there until the next export succeeds.
            self.statusBar().showMessage("正在跑別的工作,等它做完再按一次", 7000)
            return
        self._run_job(
            "正在取得村莊資訊…",
            "export",
            lambda run: commands.export(run.frames),
            self._show_export,
        )

    def _show_export(self, export: VillageExport) -> None:
        """Draw an export into the table, whether it was just taken or read off disk.

        The label carries the count when there is one and the export's own
        message when there is not, which is what a failure has instead of rows:
        no village on screen, a menu that did not open, nothing saved yet. It
        used to have a box of its own below the table, repeating the count and
        the path a line at a time; the count is here and the path is in the log.
        """
        if export.tag:
            self.current_account_tag = export.tag
        self.account_label.setText(
            f"帳號：{export.tag} — {len(export.entities)} 筆資料"
            if export.tag
            else export.message or "尚未匯入帳號"
        )
        self.account_table.setRowCount(len(export.entities))
        for index, entity in enumerate(export.entities):
            values = (entity.section, entity.data_id, entity.name, entity.level, entity.count)
            for column, value in enumerate(values):
                self.account_table.setItem(
                    index, column, QTableWidgetItem("—" if value is None else str(value))
                )

    def gemini_client(self) -> GeminiClient:
        return GeminiClient(
            api_key=self.api_key.text(),
            settings=GeminiSetting(
                model=self.model_combo.currentText().strip() or DEFAULT_GEMINI_MODEL,
                base_url=self.endpoint.text().strip(),
                thinking_level=self.thinking_combo.currentText(),
            ),
        )

    def save_api(self) -> None:
        try:
            self.secrets.save(self.api_key.text())
            # Only the main tier is on this tab; the lite one has no picker
            # because nothing about it is a judgement call the user makes.
            self._save_config(
                gemini=self.config.gemini.model_copy(
                    update={
                        "main": GeminiSetting(
                            model=self.model_combo.currentText(),
                            base_url=self.endpoint.text(),
                            thinking_level=self.thinking_combo.currentText(),
                        )
                    }
                )
            )
            logger.info("Saved API settings, model=%s", self.model_combo.currentText())
            QMessageBox.information(self, "Saved", "API Key 已使用 Windows DPAPI 儲存。")
        except Exception as exc:
            self._error("Save API Key", str(exc))

    def clear_api(self) -> None:
        self.secrets.clear()
        self.api_key.clear()
        QMessageBox.information(self, "Cleared", "API Key 已清除。")

    def test_api(self) -> None:
        client = self.gemini_client()

        def task() -> tuple[list[str], str]:
            # The list has to come first. Testing generation against a model the
            # endpoint does not serve would raise and leave the picker empty,
            # which is exactly the situation the picker exists to get out of.
            models = client.list_text_models()
            try:
                return models, client.test()
            except Exception as exc:
                logger.warning("Model list loaded but generation failed: %s", exc)
                return models, f"模型清單已載入，但用 {client.settings.model} 生成失敗：{exc}"

        def done(result: tuple[list[str], str]) -> None:
            models, answer = result
            self._fill_model_combo(models)
            QMessageBox.information(
                self,
                "Gemini",
                f"{answer}\n\n已載入 {len(models)} 個文字模型，可在 Model 選單挑選。",
            )

        self.run_async("正在測試 Gemini 連線並取得模型清單…", task, done)

    def _fill_model_combo(self, models: list[str]) -> None:
        if not models:
            logger.warning(
                "The endpoint returned no text model; keeping %s", self.model_combo.currentText()
            )
            return
        current = self.model_combo.currentText().strip()
        self.model_combo.clear()
        self.model_combo.addItems(models)
        preferred = current if current in models else DEFAULT_GEMINI_MODEL
        index = self.model_combo.findText(preferred)
        self.model_combo.setCurrentIndex(max(index, 0))

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any
import logging
from pathlib import Path
from functools import partial

from PyQt5.QtGui import QPixmap, QDesktopServices
from PyQt5.QtCore import (
    Qt,
    QUrl,
    QEvent,
    QTimer,
    QBuffer,
    QObject,
    QIODevice,
    QSettings,
    QByteArray,
    QThreadPool,
)
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
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QApplication,
    QTableWidget,
    QTextBrowser,
    QPlainTextEdit,
    QTableWidgetItem,
)

from ai_coc.models import (
    Frame,
    ChatRole,
    AppConfig,
    AgentAction,
    ChatMessage,
    StockLimits,
    AttackReport,
    AttackTimings,
    DisplayTarget,
    LocatedTarget,
    UiElementList,
    AccountRowList,
    ChatTranscript,
    GeminiSettings,
    LootThresholds,
    AccountSnapshot,
    EmulatorInstance,
)
from ai_coc.prompts import PROMPTS, render
from ai_coc.constants import (
    APP_NAME,
    LOG_PATH,
    COC_PACKAGE,
    ORGANISATION,
    VERSION_LABEL,
    SCHEMA_VERSION,
    ACCOUNT_JSON_DIR,
    MASTER_DB_VERSION,
    ENTITY_MAPPING_URL,
    DEFAULT_GEMINI_MODEL,
    AGENT_PROFILE_VERSION,
)
from ai_coc.adapters.ai import AGENT_PROFILE, GeminiClient, vision_prompt
from ai_coc.adapters.mumu import MuMuAdapter
from ai_coc.adapters.config import ConfigStore
from ai_coc.parsers.village import parse_village, parse_village_text
from ai_coc.adapters.mapping import fetch_entity_mapping
from ai_coc.adapters.secrets import SecretStore
from ai_coc.adapters.database import Database

from .attack import AttackRunner
from .render import CHAT_STYLESHEET, transcript_to_html
from .workers import Worker, LogBridge, StreamWorker, UiLogHandler

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from PyQt5.QtGui import QDropEvent, QDragEnterEvent

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
# A queen wants her cloak almost at once; a warden's tome is worth holding until
# the push is deep enough to be worth saving. A hero's delay runs from that hero
# landing; the freeze's runs from the attack opening, which is why it is labelled
# apart. It used to have no time of its own at all and simply followed the last
# ability, which put it a minute and a half in whenever a champion was out. The
# numbers themselves are `AttackTimings`' own defaults; only the labels are here.
TIMING_FIELDS = (
    ("king", "野蠻人之王（落地後）"),
    ("queen", "弓箭女皇（落地後）"),
    ("warden", "大守護者（落地後）"),
    ("champion", "皇家守護（落地後）"),
    ("minion_prince", "飛盾王子（落地後）"),
    ("freeze", "冰凍法術（開打後）"),
)
# What the registry used to hold, read once so a machine that has been running
# this for months keeps its settings when they move into the config file. Saving
# through the window is what writes the file, so without this step the first
# terminal run after the upgrade would use the defaults and say nothing.
THRESHOLD_KEYS = ("min_gold", "min_elixir", "min_dark")
STOCK_KEYS = ("stop_gold", "stop_elixir", "stop_dark")


def _migrated_config(settings: QSettings) -> AppConfig | None:
    """The registry's own values as an `AppConfig`, or None if it holds none."""
    if not any(settings.contains(key) for key in (*THRESHOLD_KEYS, *STOCK_KEYS, "gemini_model")):
        return None
    defaults = AppConfig()
    endpoint = str(settings.value("gemini_endpoint", ""))

    def saved(key: str, fallback: int) -> int:
        return int(settings.value(key, fallback))

    return AppConfig(
        thresholds=LootThresholds(**{
            key: saved(key, getattr(defaults.thresholds, key)) for key in THRESHOLD_KEYS
        }),
        stock=StockLimits(**{key: saved(key, getattr(defaults.stock, key)) for key in STOCK_KEYS}),
        timings=AttackTimings(**{
            key: saved(f"hero_{key}", getattr(defaults.timings, key))
            for key, _label in TIMING_FIELDS
        }),
        gemini_model=str(settings.value("gemini_model", defaults.gemini_model)),
        # The OpenAI-compatible endpoint the previous release defaulted to is not
        # a google-genai base URL. This is the one moment it could be carried
        # forward, so it is the moment to drop it: past here the terminal reads
        # the file directly and has no window to filter it on the way through.
        gemini_endpoint="" if "openai" in endpoint.lower() else endpoint,
    )


def migrate_settings() -> None:
    """Move whatever the registry still holds into the config file, once.

    Called from `main()` before anything else reads the file, rather than from
    the window alone: a terminal run can easily be the first thing to look after
    an upgrade, and it would otherwise find no file and use the defaults while
    the user's own settings sat in the registry unread.
    """
    store = ConfigStore()
    if store.path.is_file():
        return
    migrated = _migrated_config(QSettings(ORGANISATION, "CoCAIController"))
    if migrated is not None:
        store.save(migrated)
        logger.info("Carried the saved settings over into %s", store.path)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — {VERSION_LABEL}")
        self.resize(1260, 820)
        self.pool = QThreadPool.globalInstance()
        self.db = Database()
        self.secrets = SecretStore()
        self.settings = QSettings(ORGANISATION, "CoCAIController")
        self.config = ConfigStore().load()
        self.mumu: MuMuAdapter | None = None
        self.instances: list[EmulatorInstance] = []
        self.active: EmulatorInstance | None = None
        self.current_frame: Frame | None = None
        self.chat_image_pending = False
        self.frame_sequence = 0
        self.current_account_tag = ""
        self.running_task_id: int | None = None
        self.attack_running = False
        # Read from the worker threads as well as the UI one, so it is a plain
        # bool rather than the timer's own state: stopping has to reach the
        # attack loop and the agent loop, not just the next scheduled cycle.
        self.automation_active = False
        self.chat = ChatTranscript()
        # A streamed reply arrives token by token; repaint on a beat instead.
        self.chat_repaint = QTimer(self)
        self.chat_repaint.setSingleShot(True)
        self.chat_repaint.setInterval(120)
        self.chat_repaint.timeout.connect(self._paint_chat)
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
        self.setAcceptDrops(True)
        self._build_ui()
        self._attach_log_panel()
        self.statusBar().showMessage("Ready — 偵測 MuMu 以開始")
        self.refresh_instances()
        self.refresh_entity_mapping()
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
            QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QTableWidget { background: #0e141f; color: #e8edf7; border: 1px solid #34435d; border-radius: 6px; padding: 6px; }
            QHeaderView::section { background: #243149; color: #dbe6fa; padding: 6px; border: 0; }
            QStatusBar { background: #0d121b; color: #91a3c0; }
        """)
        central = QWidget()
        central_layout = QVBoxLayout(central)
        tabs = QTabWidget()
        tabs.addTab(self._control_tab(), "主控")
        tabs.addTab(self._account_tab(), "帳號進度")
        tabs.addTab(self._agent_tab(), "AI 助手")
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
        open_log = QPushButton("開啟記錄檔")
        open_log.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_PATH)))
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
        logger.info("%s %s started, log file: %s", APP_NAME, VERSION_LABEL, LOG_PATH)

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
        self.emulator_details = QPlainTextEdit()
        self.emulator_details.setReadOnly(True)
        self.emulator_details.setMaximumHeight(150)
        layout.addWidget(self.emulator_details)
        return group

    def _timing_group(self) -> QGroupBox:
        """One delay per hero, not per card slot: an upgrading hero cannot take
        the field, so its card is absent and every slot after it shifts. Freeze
        is the one spell held back, and the one timed from the attack opening
        rather than from a landing, so its row says which.

        The keys are `AttackTimings`' own field names. The `hero_` prefix they
        carried in the registry survives in `_migrated_config` alone, which is
        the only thing that still reads what was saved there.
        """
        group = QGroupBox("大招與法術時機（秒）")
        form = QFormLayout(group)
        self.timing_delays: dict[str, QSpinBox] = {}
        for key, label in TIMING_FIELDS:
            box = QSpinBox()
            box.setRange(0, 180)
            box.setValue(getattr(self.config.timings, key))
            self.timing_delays[key] = box
            form.addRow(label, box)
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
        left_layout.addWidget(self._timing_group())
        left_layout.addStretch()
        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.live_view = QCheckBox("即時畫面")
        self.live_view.setToolTip("每半秒抓一張 CoC 畫面；關掉之後這裡只會顯示手動擷取的截圖")
        self.live_view.setChecked(str(self.settings.value("live_view", "true")).lower() == "true")
        self.live_view.toggled.connect(self._toggle_live_view)
        right_layout.addWidget(self.live_view)
        self.frame_label = QLabel("尚無畫面")
        self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setMinimumSize(520, 300)
        self.frame_label.setStyleSheet("background:#16181d;color:#bbb;border:1px solid #444")
        right_layout.addWidget(self.frame_label)
        run_log = QGroupBox("自動化進度")
        run_log_layout = QVBoxLayout(run_log)
        self.automation_log = QPlainTextEdit()
        self.automation_log.setReadOnly(True)
        run_log_layout.addWidget(self.automation_log)
        right_layout.addWidget(run_log, 1)
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
        button = QPushButton("AI 自動取得 JSON")
        button.clicked.connect(self.ai_import_village)
        paste = QPushButton("匯入剪貼簿 JSON")
        paste.clicked.connect(self.import_clipboard_village)
        file_button = QPushButton("選擇 JSON 檔案")
        file_button.clicked.connect(self.import_village)
        self.account_label = QLabel("尚未匯入帳號")
        row.addWidget(button)
        row.addWidget(paste)
        row.addWidget(file_button)
        row.addWidget(self.account_label)
        row.addStretch()
        layout.addLayout(row)
        self.account_table = QTableWidget(0, 10)
        self.account_table.setHorizontalHeaderLabels([
            "Section",
            "Data ID",
            "Name",
            "World",
            "Category",
            "Level",
            "Count",
            "Next",
            "Cost",
            "Time",
        ])
        self.account_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.account_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.account_table)
        self.account_summary = QPlainTextEdit()
        self.account_summary.setReadOnly(True)
        self.account_summary.setMaximumHeight(140)
        layout.addWidget(self.account_summary)
        return page

    def _agent_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        buttons = QHBoxLayout()
        teach = QPushButton("儲存為使用者教學")
        teach.clicked.connect(self.save_teaching)
        choose_image = QPushButton("選擇圖片")
        choose_image.clicked.connect(self.choose_chat_image)
        paste_image = QPushButton("貼上圖片")
        paste_image.clicked.connect(self.paste_chat_image)
        buttons.addWidget(choose_image)
        buttons.addWidget(paste_image)
        buttons.addWidget(teach)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.chat_image_preview = QLabel("尚未附加圖片（也可以將圖片拖進視窗）")
        self.chat_image_preview.setAlignment(Qt.AlignCenter)
        self.chat_image_preview.setMaximumHeight(180)
        self.chat_image_preview.setStyleSheet(
            "background:#0e141f;border:1px dashed #486083;border-radius:6px;padding:8px;color:#91a3c0"
        )
        layout.addWidget(self.chat_image_preview)
        self.chat_history = QTextBrowser()
        self.chat_history.document().setDefaultStyleSheet(CHAT_STYLESHEET)
        layout.addWidget(self.chat_history)
        row = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("Ask or teach the CoC Agent…")
        self.chat_input.installEventFilter(self)
        self.chat_input.returnPressed.connect(self.send_chat)
        send = QPushButton("送出")
        send.clicked.connect(self.send_chat)
        row.addWidget(self.chat_input)
        row.addWidget(send)
        layout.addLayout(row)
        return page

    def _say(self, role: ChatRole, heading: str, body: str = "") -> ChatMessage:
        """Add one entry to the transcript and repaint it."""
        message = self.chat.add(role, heading, body)
        self._paint_chat()
        return message

    def _paint_chat(self) -> None:
        bar = self.chat_history.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 4
        position = bar.value()
        self.chat_history.setHtml(transcript_to_html(self.chat))
        bar.setValue(bar.maximum() if follow else min(position, bar.maximum()))

    def _grow(self, message: ChatMessage, chunk: str) -> None:
        message.body += chunk
        if not self.chat_repaint.isActive():
            self.chat_repaint.start()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if (
            watched is getattr(self, "chat_input", None)
            and event.type() == QEvent.KeyPress
            and event.key() == Qt.Key_V
            and event.modifiers() & Qt.ControlModifier
            and not QApplication.clipboard().image().isNull()
        ):
            self.paste_chat_image()
            return True
        return super().eventFilter(watched, event)

    def _set_chat_image(self, png: bytes, label: str) -> None:
        pix = QPixmap()
        if not pix.loadFromData(png):
            raise ValueError("無法讀取圖片")
        self.frame_sequence += 1
        self.current_frame = Frame.create(
            "uploaded-image", self.current_account_tag, png, self.frame_sequence
        )
        self.chat_image_pending = True
        self.chat_image_preview.setPixmap(
            pix.scaled(900, 170, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        self.chat_image_preview.setToolTip(label)
        self._say("system", f"已附加圖片：{label}")

    def choose_chat_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "選擇要傳給 AI 的圖片", "", "圖片 (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if path:
            try:
                self._set_chat_image(Path(path).read_bytes(), Path(path).name)
            except Exception as exc:
                self._error("圖片載入失敗", str(exc))

    def paste_chat_image(self) -> None:
        image = QApplication.clipboard().image()
        if image.isNull():
            self._error("貼上圖片", "剪貼簿裡沒有圖片")
            return
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        self._set_chat_image(bytes(data), "剪貼簿圖片")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt override
        urls = event.mimeData().urls()
        if urls and Path(urls[0].toLocalFile()).suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".bmp",
        }:
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt override
        path = Path(event.mimeData().urls()[0].toLocalFile())
        try:
            self._set_chat_image(path.read_bytes(), path.name)
            self.tabs.setCurrentIndex(2)
            event.acceptProposedAction()
        except Exception as exc:
            self._error("圖片載入失敗", str(exc))

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
        self.stop_gold = QSpinBox()
        self.stop_gold.setRange(0, 20000000)
        self.stop_gold.setSingleStep(100000)
        self.stop_elixir = QSpinBox()
        self.stop_elixir.setRange(0, 20000000)
        self.stop_elixir.setSingleStep(100000)
        self.stop_dark = QSpinBox()
        self.stop_dark.setRange(0, 500000)
        self.stop_dark.setSingleStep(10000)
        # 0 is the minimum, so this labels it in place rather than in the row text.
        for box in (self.stop_gold, self.stop_elixir, self.stop_dark):
            box.setSpecialValueText("不監控")
        self.cycle_minutes = QSpinBox()
        self.cycle_minutes.setRange(1, 120)
        for widget, value in (
            (self.min_gold, self.config.thresholds.min_gold),
            (self.min_elixir, self.config.thresholds.min_elixir),
            (self.min_dark, self.config.thresholds.min_dark),
            (self.stop_gold, self.config.stock.stop_gold),
            (self.stop_elixir, self.config.stock.stop_elixir),
            (self.stop_dark, self.config.stock.stop_dark),
            # Only the window ever waits, so this one stays in the registry.
            (self.cycle_minutes, int(self.settings.value("cycle_minutes", 10))),
        ):
            widget.setValue(value)
        battle_form.addRow("最低金幣", self.min_gold)
        battle_form.addRow("最低聖水", self.min_elixir)
        battle_form.addRow("最低黑水", self.min_dark)
        battle_form.addRow("金幣達到此值停止刷資源", self.stop_gold)
        battle_form.addRow("聖水達到此值停止刷資源", self.stop_elixir)
        battle_form.addRow("黑水達到此值停止刷資源", self.stop_dark)
        battle_form.addRow("閒置時重試間隔（分鐘）", self.cycle_minutes)
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
        self._save_config(
            thresholds=self._thresholds(),
            stock=self._limits(),
            timings=AttackTimings(**{key: box.value() for key, box in self.timing_delays.items()}),
        )
        self.automation_log.appendPlainText("自動化設定已保存。")

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
        self.automation_timer.start(self.cycle_minutes.value() * 60000)
        self._paint_run_button()
        self.automation_log.appendPlainText("自動化已啟動，第一輪開始。")
        # Nothing runs before this button, so an unfinished task from a previous
        # session is picked back up here. It goes first because a PENDING row
        # makes `automation_cycle` stand down, and `execute_agent_command` claims
        # `running_task_id` straight away, so the cycle below sees it and yields.
        QTimer.singleShot(100, self.resume_pending_tasks)
        QTimer.singleShot(300, self.automation_cycle)

    def stop_automation(self) -> None:
        self.automation_active = False
        self.automation_timer.stop()
        self._paint_run_button()
        self.automation_log.appendPlainText(
            "已停止：不再搜尋新對手，也不再接續未完成任務。已經開打的這一場會打完再回營。"
        )

    def automation_cycle(self) -> None:
        # An attack holds no task row, so it needs its own flag here: a second
        # runner started mid-attack would interleave taps on the same display.
        if (
            not self.automation_active
            or self.running_task_id is not None
            or self.attack_running
            or self.db.pending_tasks()
            or not self.active
        ):
            return
        jobs: list[Callable[[], None]] = [
            partial(self._queue_agent_job, instruction)
            for enabled, instruction in (
                (
                    self.auto_collect.isChecked(),
                    "回到主村，收取所有金礦、聖水收集器和黑水鑽井的資源，完成後回到主村畫面",
                ),
                (
                    self.auto_donate.isChecked(),
                    "打開部落聊天室，檢查可捐兵請求並依現有軍隊安全捐兵，完成後返回主村",
                ),
                (
                    self.auto_upgrade.isChecked(),
                    "檢查空閒建築工人與目前資源，依已保存的升級優先順序安排一項建築升級",
                ),
                (
                    self.auto_walls.isChecked(),
                    "檢查保留資源門檻後，使用超出保留量的資源升級一段城牆",
                ),
            )
            if enabled
        ]
        # Attacking is a fixed sequence against a screen that expires in 30
        # seconds, so it drives itself instead of going through the agent loop.
        if self.auto_attack.isChecked():
            jobs.append(self.run_attack)
        if not jobs:
            self.automation_log.appendPlainText("巡檢完成：尚未啟用任何自主行為。")
            return
        job = jobs[self.automation_step % len(jobs)]
        self.automation_step += 1
        job()

    def _queue_agent_job(self, instruction: str) -> None:
        task_id = self.db.add_task(instruction)
        self.automation_log.appendPlainText(f"建立自主任務 #{task_id}：{instruction}")
        self.execute_agent_command(instruction, task_id, automated=True)

    def _thresholds(self) -> LootThresholds:
        return LootThresholds(
            min_gold=self.min_gold.value(),
            min_elixir=self.min_elixir.value(),
            min_dark=self.min_dark.value(),
        )

    def _limits(self) -> StockLimits:
        return StockLimits(
            stop_gold=self.stop_gold.value(),
            stop_elixir=self.stop_elixir.value(),
            stop_dark=self.stop_dark.value(),
        )

    def run_attack(self) -> None:
        m, a = self._require()
        thresholds = self._thresholds()
        limits = self._limits()

        # The client is only used once an opponent has passed the thresholds, to
        # pick the flank and the spell targets; screen reading never needs it.
        planner = self.gemini_client() if self.api_key.text().strip() else None
        abilities = AttackTimings(**{key: box.value() for key, box in self.timing_delays.items()})

        def task() -> AttackReport:
            active = m.ensure_coc(a.index)
            adb = m.controller(active.adb_serial)
            return AttackRunner(
                adb=adb,
                display=adb.display_for(COC_PACKAGE),
                thresholds=thresholds,
                stock=limits,
                abilities=abilities,
                ai=planner,
                should_stop=lambda: not self.automation_active,
            ).run()

        def done(report: AttackReport) -> None:
            # The storage is full, so the next pass would only read it again and
            # come back here. Stopping is the whole point of the threshold. The
            # skip count is left out of this one: it returns before any opponent
            # is scouted, and this is the only line saying why the automation
            # switched itself off.
            if report.stock_full:
                self.automation_log.appendPlainText(report.message)
                self.stop_automation()
                return
            self.automation_log.appendPlainText(
                f"進攻巡檢結束（跳過 {report.skipped} 個對手）：{report.message}"
            )

        def finished() -> None:
            self.attack_running = False
            self._queue_next_cycle()

        self.automation_log.appendPlainText("開始搜尋對手…")
        self.attack_running = True
        self.run_async("AI 正在搜尋對手並進攻…", task, done, finished)

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
        self.model_combo.addItem(self.config.gemini_model or DEFAULT_GEMINI_MODEL)
        self.model_combo.setToolTip("按「測試連線並載入模型」後會列出這把金鑰可用的文字模型")
        self.endpoint = QLineEdit(self.config.gemini_endpoint)
        self.endpoint.setPlaceholderText("留空即使用 Google 官方端點")
        try:
            self.api_key.setText(self.secrets.load())
        except Exception:
            logger.debug("Unable to load the saved API key", exc_info=True)
        form.addRow("Provider", self.provider_combo)
        form.addRow("API Key", self.api_key)
        form.addRow("Model", self.model_combo)
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
        text = QLabel(
            f"<h1>{APP_NAME}</h1><p>{VERSION_LABEL}</p>"
            f"<p>Master DB: {MASTER_DB_VERSION}<br>Schema: {SCHEMA_VERSION}<br>Agent Profile: {AGENT_PROFILE_VERSION}</p>"
            "<p>AI General Operator platform. Live battle tactics are reserved for future RL.</p>"
        )
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

    def run_stream(
        self,
        label: str,
        fn: Callable[[], Iterator[str]],
        into: ChatMessage,
        done: Callable[[], None] | None = None,
    ) -> None:
        """Grow one transcript entry from a generator running on the pool."""
        logger.info("Started: %s", label)
        self.statusBar().showMessage(label)

        failures: list[str] = []

        def failed(message: str) -> None:
            # Otherwise the dismissed message box leaves an empty reply with no reason.
            failures.append(message)
            into.body += f"\n\n**失敗**：{message}"
            self._error(label, message)

        def finished() -> None:
            self._paint_chat()
            self.statusBar().showMessage("Ready")
            # StreamWorker signals `finished` from a `finally`, so a failed stream
            # gets here too; `done` may only run when the reply actually arrived.
            if done and not failures:
                done()

        worker = StreamWorker(fn, label)
        worker.signals.delta.connect(lambda chunk: self._grow(into, chunk))
        worker.signals.error.connect(failed)
        worker.signals.finished.connect(finished)
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
        if 0 <= index < len(self.instances):
            self.active = self.instances[index]
            a = self.active
            self.emulator_details.setPlainText(
                f"emulator_id: {a.emulator_id}\ninstance_id: {a.index}\nname: {a.name}\nstate: {a.state}\n"
                f"android: {a.android_version} / ready={a.android_started}\nadb_serial: {a.adb_serial}\n"
                f"pid: {a.pid}\nmain_hwnd: 0x{a.main_hwnd:X}\nrender_hwnd: 0x{a.render_hwnd:X}\n"
                f"resolution: {a.resolution}\ndpi: {a.dpi}\nCoC running: {a.coc_running}\naccount_tag: {self.current_account_tag or 'unbound'}"
            )

    def _require(self) -> tuple[MuMuAdapter, EmulatorInstance]:
        if not self.mumu or not self.active:
            raise RuntimeError("請先選擇 MuMu instance")
        return self.mumu, self.active

    def launch_instance(self) -> None:
        m, a = self._require()
        self.run_async(
            "Launching MuMu…",
            lambda: m.launch_instance(a.index),
            lambda _: self.refresh_instances(),
        )

    def restart_emulator(self) -> None:
        m, a = self._require()
        self.run_async(
            "Restarting MuMu…",
            lambda: m.restart_instance(a.index),
            lambda _: self.refresh_instances(),
        )

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

    def restart_coc(self) -> None:
        m, a = self._require()
        self.run_async(
            "Restarting Clash of Clans…",
            lambda: m.restart_coc(a),
            lambda _: self.refresh_instances(),
        )

    def back(self) -> None:
        m, a = self._require()
        self.run_async("Sending Back…", lambda: m.back(a))

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

    def import_village(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Village JSON", "", "JSON (*.json);;All Files (*)"
        )
        if not path:
            return
        try:
            self._apply_village_snapshot(parse_village(path))
        except Exception as exc:
            self._error("村莊 JSON 匯入失敗", str(exc))

    def _apply_village_snapshot(self, snapshot: AccountSnapshot) -> None:
        logger.info(
            "Importing village snapshot %s (%d entities)", snapshot.tag, len(snapshot.entities)
        )
        self.db.save_account(snapshot)
        safe_tag = "".join(ch for ch in snapshot.tag if ch.isalnum() or ch in "-_#") or "UNKNOWN"
        saved_path = ACCOUNT_JSON_DIR / f"{safe_tag}.json"
        saved_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        self.current_account_tag = snapshot.tag
        rows = self.db.account_rows(snapshot.tag)
        self.account_label.setText(f"帳號：{snapshot.tag} — {len(rows)} 筆資料")
        self.account_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                row.section,
                row.data_id,
                row.name or "UNKNOWN",
                row.world or "—",
                row.category or "—",
                row.level,
                row.count,
                row.next_level,
                row.upgrade_cost,
                row.upgrade_seconds,
            ]
            for column, value in enumerate(values):
                self.account_table.setItem(
                    row_index, column, QTableWidgetItem("—" if value is None else str(value))
                )
        unknown = sum(1 for row in rows if not row.name)
        self.account_summary.setPlainText(
            f"已匯入村莊 JSON，共 {len(rows)} 筆，其中 {unknown} 筆是尚未收錄的 data_id。\n"
            f"資料庫與 JSON 檔都已保存：{saved_path}"
        )
        self._select_instance(self.instance_combo.currentIndex())

    def refresh_entity_mapping(self) -> None:
        def task() -> int:
            entries = fetch_entity_mapping().registry_entries(ENTITY_MAPPING_URL)
            self.db.import_registry(entries)
            return len(entries)

        def done(count: int) -> None:
            logger.info("Entity registry now holds %d community names", count)

        self.run_async("正在更新實體名稱對照表…", task, done)

    def import_clipboard_village(self) -> None:
        try:
            text = QApplication.clipboard().text().strip()
            logger.info("Clipboard holds %d characters", len(text))
            if not text:
                raise ValueError("剪貼簿是空的")
            self._apply_village_snapshot(parse_village_text(text))
        except Exception as exc:
            self._error("剪貼簿 JSON 匯入失敗", str(exc))

    def ai_import_village(self) -> None:
        m, a = self._require()
        client = self.gemini_client()
        self.account_summary.setPlainText("AI 正在尋找 JSON／複製按鈕，請稍候…")

        def locate(png: bytes, goal: str) -> tuple[int, int]:
            target = client.generate_structured(
                render("locate_target", goal=goal), LocatedTarget, png
            )
            logger.info("AI located %s: %s", goal, target.model_dump())
            if not target.found:
                raise RuntimeError(f"AI 找不到{goal}：{target.reason}")
            return int(target.x_pct * 16), int(target.y_pct * 9)

        def task() -> None:
            active = m.ensure_coc(a.index)
            # 匯出村莊 JSON 藏在設定 → 更多設定底下，是這兩層都要點過才會出現的。
            for goal in ("畫面右側直排圖示裡的設定齒輪按鈕", "設定視窗中的「更多設定」按鈕"):
                png = m.screenshot(active)
                x, y = locate(png, goal)
                m.tap(active, x, y)
                time.sleep(2)
            # 「更多設定」開啟時停在列表最上面，數據匯出那一段在最底下，捲到底才看得到。
            for _ in range(5):
                m.swipe(active, (800, 650), (800, 250), 400)
                time.sleep(1)
            png = m.screenshot(active)
            x, y = locate(png, "「以 JSON 格式匯出村莊數據」這一列右邊的「複製」按鈕")
            m.tap(active, x, y)
            time.sleep(2)

        def done(_: None) -> None:
            self.import_clipboard_village()

        self.run_async("AI 正在取得村莊 JSON…", task, done)

    def gemini_client(self) -> GeminiClient:
        return GeminiClient(
            settings=GeminiSettings(
                api_key=self.api_key.text(),
                model=self.model_combo.currentText().strip() or DEFAULT_GEMINI_MODEL,
                base_url=self.endpoint.text().strip(),
            )
        )

    def save_api(self) -> None:
        try:
            self.secrets.save(self.api_key.text())
            self._save_config(
                gemini_model=self.model_combo.currentText(), gemini_endpoint=self.endpoint.text()
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

    def account_context(self) -> str:
        if not self.current_account_tag:
            return "No Village JSON imported."
        return AccountRowList(
            self.db.account_rows(self.current_account_tag)[:40]
        ).model_dump_json()

    def knowledge_context(self) -> str:
        items = self.db.recent_knowledge(40)
        return (
            "\n".join(f"- [{item.status}] {item.statement}" for item in items)
            or "尚無使用者教學。"
        )

    def analyze_frame(self) -> None:
        if not self.current_frame:
            self.capture()
            QMessageBox.information(self, "Screenshot", "已開始取得畫面；完成後請再按 Analyze。")
            return
        frame = self.current_frame
        client = self.gemini_client()
        prompt = vision_prompt(frame.emulator_id, frame.frame_id, self.account_context())
        analysis = self._say("assistant", f"畫面分析 [{frame.frame_id}]")
        self.run_stream("AI 正在分析目前畫面…", lambda: client.stream(prompt, frame.png), analysis)

    def live_ai_test(self) -> None:
        m, a = self._require()
        client = self.gemini_client()
        self.tabs.setCurrentIndex(2)
        self._say("system", "實機測試：正在啟動 CoC、擷取畫面並等待 AI 回覆…")
        answer = self._say("assistant", "實機 AI 回覆")
        captured: list[bytes] = []

        def task() -> Iterator[str]:
            active = m.ensure_coc(a.index)
            png = m.screenshot(active)
            captured.append(png)
            yield from client.stream(PROMPTS["live_test"], png)

        def done() -> None:
            # Only reached once the whole generator ran, so the capture is there.
            self.frame_sequence += 1
            self.current_frame = Frame.create(
                a.emulator_id, self.current_account_tag, captured[0], self.frame_sequence
            )
            proof_path = os.environ.get("COC_LIVE_TEST_SCREENSHOT", "").strip()
            if proof_path:
                QTimer.singleShot(800, lambda: self.grab().save(proof_path, "PNG"))

        self.run_stream("實機 AI 測試進行中，請等待回覆…", task, answer, done)

    def send_chat(self) -> None:
        text = self.chat_input.text().strip()
        if not text:
            return
        should_remember = any(
            word in text for word in ("記住", "記下", "以後要", "下次要", "我教你")
        )
        if should_remember:
            self.db.add_knowledge(
                self.active.emulator_id if self.active else "",
                self.current_frame.frame_id if self.current_frame else "",
                text,
                "USER_CONFIRMED",
            )
        recent = self.chat.tail(3500)
        self.chat_input.clear()
        self._say("user", "你", text)
        if (
            any(
                word in text
                for word in (
                    "打開",
                    "開啟",
                    "點擊",
                    "按下",
                    "進入",
                    "返回",
                    "關閉",
                    "收取",
                    "捐兵",
                    "升級",
                    "刷牆",
                    "進攻",
                    "搜尋",
                )
            )
            and self.active
        ):
            task_id = self.db.add_task(text)
            self._say("system", "AI 正在操作 MuMu 並確認畫面，請稍候…")
            self.execute_agent_command(text, task_id)
            return
        frame = self.current_frame if self.chat_image_pending else None
        context = render(
            "chat",
            profile=AGENT_PROFILE,
            knowledge=self.knowledge_context(),
            account=self.account_context(),
            emulator=self.active.emulator_id if self.active else "none",
            recent=recent,
            text=text,
        )

        def done() -> None:
            if frame:
                self.chat_image_pending = False
                self.chat_image_preview.clear()
                self.chat_image_preview.setText("尚未附加圖片（也可以將圖片拖進視窗）")

        client = self.gemini_client()
        reply = self._say("assistant", "AI 回覆")
        self.run_stream(
            "AI 正在回覆…",
            lambda: client.stream(context, frame.png if frame else None),
            reply,
            done,
        )

    def resume_pending_tasks(self) -> None:
        # Without the flag, pressing stop only kept the *timer* quiet: the task
        # in flight still finished, and its completion resumed the next pending
        # one, so the automation carried on as if nothing had been pressed.
        if not self.automation_active or self.running_task_id is not None or not self.active:
            return
        pending = self.db.pending_tasks()
        if pending:
            item = pending[0]
            logger.info("Resuming pending task #%d: %s", item.id, item.instruction)
            self._say("system", f"自動繼續未完成任務 #{item.id}：{item.instruction}")
            self.tabs.setCurrentIndex(2)
            self.execute_agent_command(item.instruction, item.id, automated=True)

    def _apply_agent_action(
        self, m: MuMuAdapter, active: EmulatorInstance, action: AgentAction
    ) -> bool:
        """Perform one AI-proposed action; False when the action is not executable."""
        if action.action == "tap":
            m.tap(active, int(action.x_pct * 16), int(action.y_pct * 9))
        elif action.action == "back":
            m.back(active)
        elif action.action == "swipe_up":
            m.swipe(active, (800, 720), (800, 220), 500)
        elif action.action == "swipe_down":
            m.swipe(active, (800, 220), (800, 720), 500)
        else:
            return False
        return True

    def execute_agent_command(self, command: str, task_id: int, automated: bool = False) -> None:
        m, a = self._require()
        client = self.gemini_client()
        self.running_task_id = task_id
        self.db.update_task(task_id, "RUNNING", "正在觀察目前畫面")
        reference_frame = self.current_frame if self.chat_image_pending else None

        def task() -> tuple[bytes, str, bool]:
            reference = ""
            if reference_frame:
                reference = client.generate(PROMPTS["reference_image"], reference_frame.png)
            active = m.ensure_coc(a.index)
            last_png = b""
            max_steps = (
                25 if any(word in command for word in ("進攻", "戰鬥", "搜尋資源村")) else 8
            )
            logger.info("Agent task #%d starts, at most %d steps: %s", task_id, max_steps, command)
            for step in range(max_steps):
                # A command typed into the AI tab is the user's own, so only the
                # automation's own jobs answer to the stop button. Left PENDING
                # rather than COMPLETED, so starting the automation again
                # picks the task back up where it stopped.
                if automated and not self.automation_active:
                    logger.info(
                        "Agent task #%d stops after %d step(s): stop pressed", task_id, step
                    )
                    return last_png, "已停止自動化，這個任務保留為未完成", False
                self.db.update_task(task_id, "RUNNING", f"第 {step + 1} 步：截圖、判斷與驗證")
                last_png = m.screenshot(active)
                elements = UiElementList(m.ui_elements(active)).model_dump_json()
                prompt = render(
                    "agent_step",
                    command=command,
                    reference=reference,
                    knowledge=self.knowledge_context(),
                    elements=elements,
                    may_upgrade=self.auto_upgrade.isChecked(),
                    may_walls=self.auto_walls.isChecked(),
                    may_attack=self.auto_attack.isChecked(),
                )
                action = client.generate_structured(prompt, AgentAction, last_png)
                logger.info("Agent step %d/%d: %s", step + 1, max_steps, action.model_dump())
                if action.done:
                    return last_png, action.message or "指令已完成", True
                if not self._apply_agent_action(m, active, action):
                    return last_png, action.message or "AI 無法安全執行這個操作", False
                time.sleep(2)
            return last_png, f"已執行操作，但 {max_steps} 次畫面確認後仍無法確認完成。", False

        def done(result: tuple[bytes, str, bool]) -> None:
            png, message, completed = result
            self.frame_sequence += 1
            self.current_frame = Frame.create(
                a.emulator_id, self.current_account_tag, png, self.frame_sequence
            )
            self.chat_image_pending = False
            self._say("assistant", "AI 操作結果", message)
            self.db.update_task(task_id, "COMPLETED" if completed else "PENDING", message)
            self.running_task_id = None
            proof_path = os.environ.get("COC_AGENT_SCREENSHOT", "").strip()
            if proof_path:
                QTimer.singleShot(800, lambda: self.grab().save(proof_path, "PNG"))
            if completed:
                QTimer.singleShot(1200, self.resume_pending_tasks)
            self._queue_next_cycle()

        self.run_async("AI 正在操作並確認 MuMu 畫面…", task, done)

    def save_teaching(self) -> None:
        statement = self.chat_input.text().strip()
        if not statement:
            QMessageBox.information(self, "Teaching", "請先在輸入框輸入教學內容。")
            return
        self.db.add_knowledge(
            self.active.emulator_id if self.active else "",
            self.current_frame.frame_id if self.current_frame else "",
            statement,
            "USER_CONFIRMED",
        )
        self._say("system", "TEACHING [USER_CONFIRMED]", statement)
        self.chat_input.clear()

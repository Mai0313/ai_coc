from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

from PyQt5.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QPlainTextEdit, QSplitter, QStatusBar, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .ai import AGENT_PROFILE, GoogleGeminiProvider, vision_prompt
from .battle import load_battle_script
from .constants import (AGENT_PROFILE_VERSION, APP_NAME, MASTER_DB_VERSION,
                        SCHEMA_VERSION, UPDATED_DATE, VERSION_LABEL, bundle_root)
from .database import Database
from .models import EmulatorInstance, Frame
from .mumu import MuMuAdapter
from .secrets import SecretStore
from .village import parse_village


class WorkerSignals(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()


class Worker(QRunnable):
    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            self.signals.result.emit(self.fn())
        except Exception as exc:
            self.signals.error.emit(f"{exc}\n\n{traceback.format_exc(limit=3)}")
        finally:
            self.signals.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — {VERSION_LABEL}")
        self.resize(1260, 820)
        self.pool = QThreadPool.globalInstance()
        self.db = Database()
        self.secrets = SecretStore()
        self.settings = QSettings("Hsien0818666", "CoCAIController")
        self.mumu: MuMuAdapter | None = None
        self.instances: list[EmulatorInstance] = []
        self.active: EmulatorInstance | None = None
        self.current_frame: Frame | None = None
        self.frame_sequence = 0
        self.current_account_tag = ""
        self._build_ui()
        self.statusBar().showMessage("Ready — 偵測 MuMu 以開始")
        self.refresh_instances()

    def _build_ui(self) -> None:
        tabs = QTabWidget()
        tabs.addTab(self._emulator_tab(), "Emulators")
        tabs.addTab(self._account_tab(), "Account Progress")
        tabs.addTab(self._agent_tab(), "AI Agent")
        tabs.addTab(self._battle_tab(), "Battle Preparation")
        tabs.addTab(self._settings_tab(), "Settings")
        tabs.addTab(self._about_tab(), "About")
        self.setCentralWidget(tabs)
        self.setStatusBar(QStatusBar())

    def _emulator_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        self.instance_combo = QComboBox(); self.instance_combo.currentIndexChanged.connect(self._select_instance)
        for text, fn in (("Refresh", self.refresh_instances), ("Launch", self.launch_instance),
                         ("Connect / Screenshot", self.capture), ("Launch CoC", self.launch_coc),
                         ("Restart CoC", self.restart_coc), ("Back", self.back),
                         ("Restart Emulator", self.restart_emulator), ("Close", self.close_emulator)):
            button = QPushButton(text); button.clicked.connect(fn); toolbar.addWidget(button)
        layout.addWidget(QLabel("Active Agent Target")); layout.addWidget(self.instance_combo); layout.addLayout(toolbar)
        splitter = QSplitter(Qt.Horizontal)
        left = QWidget(); left_layout = QVBoxLayout(left)
        self.emulator_details = QPlainTextEdit(); self.emulator_details.setReadOnly(True)
        left_layout.addWidget(self.emulator_details)
        right = QWidget(); right_layout = QVBoxLayout(right)
        self.frame_label = QLabel("No screenshot"); self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setMinimumSize(640, 360); self.frame_label.setStyleSheet("background:#16181d;color:#bbb;border:1px solid #444")
        right_layout.addWidget(self.frame_label)
        splitter.addWidget(left); splitter.addWidget(right); splitter.setSizes([360, 850]); layout.addWidget(splitter)
        return page

    def _account_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        row = QHBoxLayout(); button = QPushButton("Import Village JSON"); button.clicked.connect(self.import_village)
        self.account_label = QLabel("No account imported"); row.addWidget(button); row.addWidget(self.account_label); row.addStretch(); layout.addLayout(row)
        self.account_table = QTableWidget(0, 10)
        self.account_table.setHorizontalHeaderLabels(["Section", "Data ID", "Name", "World", "Category", "Level", "Count", "Next", "Cost", "Time"])
        self.account_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.account_table.horizontalHeader().setStretchLastSection(True); layout.addWidget(self.account_table)
        self.account_summary = QPlainTextEdit(); self.account_summary.setReadOnly(True); self.account_summary.setMaximumHeight(140); layout.addWidget(self.account_summary)
        return page

    def _agent_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        buttons = QHBoxLayout()
        analyze = QPushButton("Analyze Current Screenshot"); analyze.clicked.connect(self.analyze_frame)
        teach = QPushButton("Save as USER_CONFIRMED Teaching"); teach.clicked.connect(self.save_teaching)
        buttons.addWidget(analyze); buttons.addWidget(teach); buttons.addStretch(); layout.addLayout(buttons)
        self.chat_history = QPlainTextEdit(); self.chat_history.setReadOnly(True); layout.addWidget(self.chat_history)
        row = QHBoxLayout(); self.chat_input = QLineEdit(); self.chat_input.setPlaceholderText("Ask or teach the CoC Agent…")
        self.chat_input.returnPressed.connect(self.send_chat); send = QPushButton("Send"); send.clicked.connect(self.send_chat)
        row.addWidget(self.chat_input); row.addWidget(send); layout.addLayout(row)
        return page

    def _battle_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        row = QHBoxLayout(); load = QPushButton("Load Battle Script"); load.clicked.connect(self.load_battle)
        example = QPushButton("Load Example"); example.clicked.connect(lambda: self._show_battle(bundle_root() / "battle_scripts" / "BH10_BABY_DRAGON_01.json"))
        row.addWidget(load); row.addWidget(example); row.addStretch(); layout.addLayout(row)
        self.battle_view = QPlainTextEdit(); self.battle_view.setReadOnly(True); layout.addWidget(self.battle_view)
        layout.addWidget(QLabel("V1 boundary: requirements and preparation plan are available. Live tactical battle control remains RESERVED_RL."))
        return page

    def _settings_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); group = QGroupBox("AI / API"); form = QFormLayout(group)
        self.provider_combo = QComboBox(); self.provider_combo.addItem("Google Gemini")
        self.api_key = QLineEdit(); self.api_key.setEchoMode(QLineEdit.Password); self.api_key.setPlaceholderText("Stored with Windows DPAPI")
        self.model_name = QLineEdit(str(self.settings.value("gemini_model", "gemini-2.5-flash")))
        self.endpoint = QLineEdit(str(self.settings.value("gemini_endpoint", "https://generativelanguage.googleapis.com/v1beta/openai")))
        try: self.api_key.setText(self.secrets.load())
        except Exception: pass
        form.addRow("Provider", self.provider_combo); form.addRow("API Key", self.api_key); form.addRow("Model", self.model_name); form.addRow("Endpoint", self.endpoint)
        buttons = QHBoxLayout()
        for text, fn in (("Save", self.save_api), ("Test Connection", self.test_api), ("Clear", self.clear_api)):
            b = QPushButton(text); b.clicked.connect(fn); buttons.addWidget(b)
        form.addRow(buttons); layout.addWidget(group); layout.addStretch(); return page

    def _about_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        text = QLabel(f"<h1>{APP_NAME}</h1><p>{VERSION_LABEL}</p><p>Updated: {UPDATED_DATE}</p>"
                      f"<p>Master DB: {MASTER_DB_VERSION}<br>Schema: {SCHEMA_VERSION}<br>Agent Profile: {AGENT_PROFILE_VERSION}</p>"
                      "<p>AI General Operator platform. Live battle tactics are reserved for future RL.</p>")
        text.setTextFormat(Qt.RichText); layout.addWidget(text); layout.addStretch(); return page

    def run_async(self, label: str, fn: Callable[[], Any], done: Callable[[Any], None] | None = None) -> None:
        self.statusBar().showMessage(label)
        worker = Worker(fn)
        if done: worker.signals.result.connect(done)
        worker.signals.error.connect(lambda message: self._error(label, message))
        worker.signals.finished.connect(lambda: self.statusBar().showMessage("Ready"))
        self.pool.start(worker)

    def _error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def refresh_instances(self) -> None:
        def task():
            adapter = MuMuAdapter(); return adapter, adapter.version(), adapter.enumerate_instances()
        def done(result):
            self.mumu, version, self.instances = result
            self.instance_combo.blockSignals(True); self.instance_combo.clear()
            for item in self.instances:
                self.instance_combo.addItem(f"{item.name} — {item.state} — ADB {item.adb_serial}", item.emulator_id)
            self.instance_combo.blockSignals(False)
            if self.instances: self.instance_combo.setCurrentIndex(0); self._select_instance(0)
            self.statusBar().showMessage(f"MuMu {version}: {len(self.instances)} instance(s)", 6000)
        self.run_async("Detecting MuMu instances…", task, done)

    def _select_instance(self, index: int) -> None:
        if 0 <= index < len(self.instances):
            self.active = self.instances[index]
            a = self.active
            self.emulator_details.setPlainText(
                f"emulator_id: {a.emulator_id}\ninstance_id: {a.index}\nname: {a.name}\nstate: {a.state}\n"
                f"android: {a.android_version} / ready={a.android_started}\nadb_serial: {a.adb_serial}\n"
                f"pid: {a.pid}\nmain_hwnd: 0x{a.main_hwnd:X}\nrender_hwnd: 0x{a.render_hwnd:X}\n"
                f"resolution: {a.resolution}\ndpi: {a.dpi}\nCoC running: {a.coc_running}\naccount_tag: {self.current_account_tag or 'unbound'}")

    def _require(self) -> tuple[MuMuAdapter, EmulatorInstance]:
        if not self.mumu or not self.active: raise RuntimeError("請先選擇 MuMu instance")
        return self.mumu, self.active

    def launch_instance(self) -> None:
        m, a = self._require(); self.run_async("Launching MuMu…", lambda: m.launch_instance(a.index), lambda _: self.refresh_instances())
    def restart_emulator(self) -> None:
        m, a = self._require(); self.run_async("Restarting MuMu…", lambda: m.restart_instance(a.index), lambda _: self.refresh_instances())
    def close_emulator(self) -> None:
        m, a = self._require(); self.run_async("Closing MuMu…", lambda: m.close_instance(a.index), lambda _: self.refresh_instances())
    def launch_coc(self) -> None:
        m, a = self._require(); self.run_async("Launching Clash of Clans…", lambda: m.launch_coc(a), lambda _: self.refresh_instances())
    def restart_coc(self) -> None:
        m, a = self._require(); self.run_async("Restarting Clash of Clans…", lambda: m.restart_coc(a), lambda _: self.refresh_instances())
    def back(self) -> None:
        m, a = self._require(); self.run_async("Sending Back…", lambda: m.back(a))

    def capture(self) -> None:
        m, a = self._require()
        def done(png: bytes):
            self.frame_sequence += 1; self.current_frame = Frame.create(a.emulator_id, self.current_account_tag, png, self.frame_sequence)
            pix = QPixmap(); pix.loadFromData(png); self.frame_label.setPixmap(pix.scaled(self.frame_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.statusBar().showMessage(f"Captured {self.current_frame.frame_id}", 7000)
        self.run_async("Capturing current MuMu frame…", lambda: m.screenshot(a), done)

    def import_village(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Village JSON", "", "JSON (*.json);;All Files (*)")
        if not path: return
        try:
            snapshot = parse_village(path); self.db.save_account(snapshot.tag, snapshot.raw, snapshot.entities)
            self.current_account_tag = snapshot.tag; rows = self.db.account_rows(snapshot.tag)
            self.account_label.setText(f"Account: {snapshot.tag} — {len(rows)} entities")
            self.account_table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                values = [row.get("section"), row.get("data_id"), row.get("name") or "UNKNOWN", row.get("world") or "—",
                          row.get("category") or "—", row.get("level"), row.get("count"), row.get("next_level"),
                          row.get("upgrade_cost"), row.get("upgrade_seconds")]
                for column, value in enumerate(values): self.account_table.setItem(row_index, column, QTableWidgetItem("—" if value is None else str(value)))
            known = sum(1 for row in rows if row.get("name")); unknown = len(rows) - known
            self.account_summary.setPlainText(f"Imported tolerant snapshot. Known IDs: {known}; Unknown IDs queued: {unknown}.\nUnknown JSON fields were preserved in the account snapshot.")
            self._select_instance(self.instance_combo.currentIndex())
        except Exception as exc: self._error("Village JSON import failed", str(exc))

    def provider(self) -> GoogleGeminiProvider:
        return GoogleGeminiProvider(self.api_key.text(), self.model_name.text(), self.endpoint.text())
    def save_api(self) -> None:
        try:
            self.secrets.save(self.api_key.text()); self.settings.setValue("gemini_model", self.model_name.text()); self.settings.setValue("gemini_endpoint", self.endpoint.text()); QMessageBox.information(self, "Saved", "API Key 已使用 Windows DPAPI 儲存。")
        except Exception as exc: self._error("Save API Key", str(exc))
    def clear_api(self) -> None:
        self.secrets.clear(); self.api_key.clear(); QMessageBox.information(self, "Cleared", "API Key 已清除。")
    def test_api(self) -> None:
        self.run_async("Testing Gemini connection…", self.provider().test, lambda result: QMessageBox.information(self, "Gemini", result))

    def account_context(self) -> str:
        if not self.current_account_tag: return "No Village JSON imported."
        return json.dumps(self.db.account_rows(self.current_account_tag)[:250], ensure_ascii=False)

    def analyze_frame(self) -> None:
        if not self.current_frame:
            self.capture(); QMessageBox.information(self, "Screenshot", "已開始取得畫面；完成後請再按 Analyze。") ; return
        frame = self.current_frame; provider = self.provider(); prompt = vision_prompt(frame.emulator_id, frame.frame_id, self.account_context())
        self.run_async("Gemini semantic vision is analyzing…", lambda: provider.generate(prompt, frame.png), lambda result: self.chat_history.appendPlainText(f"\nVISION [{frame.frame_id}]\n{result}\n"))

    def send_chat(self) -> None:
        text = self.chat_input.text().strip()
        if not text: return
        self.chat_input.clear(); self.chat_history.appendPlainText(f"\nYOU\n{text}")
        frame = self.current_frame; context = f"{AGENT_PROFILE}\nCurrent account: {self.account_context()}\nCurrent emulator={self.active.emulator_id if self.active else 'none'}, frame={frame.frame_id if frame else 'none'}\nUser: {text}"
        self.run_async("CoC Agent is responding…", lambda: self.provider().generate(context, frame.png if frame else None), lambda result: self.chat_history.appendPlainText(f"\nAGENT\n{result}\n"))

    def save_teaching(self) -> None:
        statement = self.chat_input.text().strip()
        if not statement: QMessageBox.information(self, "Teaching", "請先在輸入框輸入教學內容。") ; return
        self.db.add_knowledge(self.active.emulator_id if self.active else "", self.current_frame.frame_id if self.current_frame else "", statement, "USER_CONFIRMED")
        self.chat_history.appendPlainText(f"\nTEACHING [USER_CONFIRMED]\n{statement}\n"); self.chat_input.clear()

    def load_battle(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load Battle Script", "", "JSON (*.json)")
        if path: self._show_battle(Path(path))
    def _show_battle(self, path: Path) -> None:
        try:
            script = load_battle_script(path); lines = [f"Script: {script.script_id}", f"Name: {script.name}", f"World: {script.world}", f"Controller: {script.battle_controller}", "", "Requirements:"]
            for category, values in script.requirements.items():
                lines.append(f"  {category}:")
                for value in values: lines.append(f"    - {value.get('data_id')} {value.get('name')} required={value.get('required', False)}")
            lines += ["", "Preparation state: REQUIREMENTS_LOADED", "Next: account/army verification through AI semantic vision.", "Enemy Preview handoff: RESERVED_RL"]
            self.battle_view.setPlainText("\n".join(lines))
        except Exception as exc: self._error("Battle Script", str(exc))


def main() -> int:
    app = QApplication(sys.argv); app.setApplicationName(APP_NAME); app.setApplicationVersion(VERSION_LABEL)
    window = MainWindow(); window.show(); return app.exec_()

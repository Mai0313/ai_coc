from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

from PyQt5.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QObject, QRunnable, QSettings, Qt, QThreadPool, QTimer, pyqtSignal
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
from .village import parse_village, parse_village_text


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
            self.signals.error.emit(str(exc))
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
        self.chat_image_pending = False
        self.frame_sequence = 0
        self.current_account_tag = ""
        self.setAcceptDrops(True)
        self._build_ui()
        self.statusBar().showMessage("Ready — 偵測 MuMu 以開始")
        self.refresh_instances()

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
            QLineEdit, QPlainTextEdit, QComboBox, QTableWidget { background: #0e141f; color: #e8edf7; border: 1px solid #34435d; border-radius: 6px; padding: 6px; }
            QHeaderView::section { background: #243149; color: #dbe6fa; padding: 6px; border: 0; }
            QStatusBar { background: #0d121b; color: #91a3c0; }
        """)
        tabs = QTabWidget()
        tabs.addTab(self._emulator_tab(), "模擬器")
        tabs.addTab(self._account_tab(), "帳號進度")
        tabs.addTab(self._agent_tab(), "AI 助手")
        tabs.addTab(self._battle_tab(), "戰鬥準備")
        tabs.addTab(self._settings_tab(), "設定")
        tabs.addTab(self._about_tab(), "關於")
        self.tabs = tabs
        self.setCentralWidget(tabs)
        self.setStatusBar(QStatusBar())

    def _emulator_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        self.instance_combo = QComboBox(); self.instance_combo.currentIndexChanged.connect(self._select_instance)
        for text, fn in (("重新整理", self.refresh_instances), ("啟動模擬器", self.launch_instance),
                         ("連線／截圖", self.capture), ("開啟部落衝突", self.launch_coc),
                         ("重啟部落衝突", self.restart_coc), ("返回", self.back),
                         ("重啟模擬器", self.restart_emulator), ("關閉模擬器", self.close_emulator)):
            button = QPushButton(text); button.clicked.connect(fn); toolbar.addWidget(button)
        layout.addWidget(QLabel("目前 AI 目標")); layout.addWidget(self.instance_combo); layout.addLayout(toolbar)
        splitter = QSplitter(Qt.Horizontal)
        left = QWidget(); left_layout = QVBoxLayout(left)
        self.emulator_details = QPlainTextEdit(); self.emulator_details.setReadOnly(True)
        left_layout.addWidget(self.emulator_details)
        right = QWidget(); right_layout = QVBoxLayout(right)
        self.frame_label = QLabel("尚無截圖"); self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setMinimumSize(640, 360); self.frame_label.setStyleSheet("background:#16181d;color:#bbb;border:1px solid #444")
        right_layout.addWidget(self.frame_label)
        splitter.addWidget(left); splitter.addWidget(right); splitter.setSizes([360, 850]); layout.addWidget(splitter)
        return page

    def _account_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        row = QHBoxLayout(); button = QPushButton("AI 自動取得 JSON"); button.clicked.connect(self.ai_import_village)
        paste = QPushButton("匯入剪貼簿 JSON"); paste.clicked.connect(self.import_clipboard_village)
        file_button = QPushButton("選擇 JSON 檔案"); file_button.clicked.connect(self.import_village)
        self.account_label = QLabel("尚未匯入帳號"); row.addWidget(button); row.addWidget(paste); row.addWidget(file_button); row.addWidget(self.account_label); row.addStretch(); layout.addLayout(row)
        self.account_table = QTableWidget(0, 10)
        self.account_table.setHorizontalHeaderLabels(["Section", "Data ID", "Name", "World", "Category", "Level", "Count", "Next", "Cost", "Time"])
        self.account_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.account_table.horizontalHeader().setStretchLastSection(True); layout.addWidget(self.account_table)
        self.account_summary = QPlainTextEdit(); self.account_summary.setReadOnly(True); self.account_summary.setMaximumHeight(140); layout.addWidget(self.account_summary)
        return page

    def _agent_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        buttons = QHBoxLayout()
        analyze = QPushButton("分析目前截圖"); analyze.clicked.connect(self.analyze_frame)
        teach = QPushButton("儲存為使用者教學"); teach.clicked.connect(self.save_teaching)
        live_test = QPushButton("實機測試 AI"); live_test.clicked.connect(self.live_ai_test)
        choose_image = QPushButton("選擇圖片"); choose_image.clicked.connect(self.choose_chat_image)
        paste_image = QPushButton("貼上圖片"); paste_image.clicked.connect(self.paste_chat_image)
        buttons.addWidget(analyze); buttons.addWidget(live_test); buttons.addWidget(choose_image); buttons.addWidget(paste_image); buttons.addWidget(teach); buttons.addStretch(); layout.addLayout(buttons)
        self.chat_image_preview = QLabel("尚未附加圖片（也可以將圖片拖進視窗）")
        self.chat_image_preview.setAlignment(Qt.AlignCenter); self.chat_image_preview.setMaximumHeight(180)
        self.chat_image_preview.setStyleSheet("background:#0e141f;border:1px dashed #486083;border-radius:6px;padding:8px;color:#91a3c0")
        layout.addWidget(self.chat_image_preview)
        self.chat_history = QPlainTextEdit(); self.chat_history.setReadOnly(True); layout.addWidget(self.chat_history)
        row = QHBoxLayout(); self.chat_input = QLineEdit(); self.chat_input.setPlaceholderText("Ask or teach the CoC Agent…")
        self.chat_input.installEventFilter(self)
        self.chat_input.returnPressed.connect(self.send_chat); send = QPushButton("送出"); send.clicked.connect(self.send_chat)
        row.addWidget(self.chat_input); row.addWidget(send); layout.addLayout(row)
        return page

    def eventFilter(self, watched, event) -> bool:
        if watched is getattr(self, "chat_input", None) and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_V and event.modifiers() & Qt.ControlModifier:
                if not QApplication.clipboard().image().isNull():
                    self.paste_chat_image(); return True
        return super().eventFilter(watched, event)

    def _set_chat_image(self, png: bytes, label: str) -> None:
        pix = QPixmap();
        if not pix.loadFromData(png): raise ValueError("無法讀取圖片")
        self.frame_sequence += 1
        self.current_frame = Frame.create("uploaded-image", self.current_account_tag, png, self.frame_sequence)
        self.chat_image_pending = True
        self.chat_image_preview.setPixmap(pix.scaled(900, 170, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.chat_image_preview.setToolTip(label)
        self.chat_history.appendPlainText(f"\n已附加圖片：{label}")

    def choose_chat_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "選擇要傳給 AI 的圖片", "", "圖片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            try: self._set_chat_image(Path(path).read_bytes(), Path(path).name)
            except Exception as exc: self._error("圖片載入失敗", str(exc))

    def paste_chat_image(self) -> None:
        image = QApplication.clipboard().image()
        if image.isNull(): self._error("貼上圖片", "剪貼簿裡沒有圖片"); return
        data = QByteArray(); buffer = QBuffer(data); buffer.open(QIODevice.WriteOnly); image.save(buffer, "PNG")
        self._set_chat_image(bytes(data), "剪貼簿圖片")

    def dragEnterEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if urls and Path(urls[0].toLocalFile()).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}: event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        path = Path(event.mimeData().urls()[0].toLocalFile())
        try: self._set_chat_image(path.read_bytes(), path.name); self.tabs.setCurrentIndex(2); event.acceptProposedAction()
        except Exception as exc: self._error("圖片載入失敗", str(exc))

    def _battle_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        row = QHBoxLayout(); load = QPushButton("載入戰鬥腳本"); load.clicked.connect(self.load_battle)
        example = QPushButton("載入範例"); example.clicked.connect(lambda: self._show_battle(bundle_root() / "battle_scripts" / "BH10_BABY_DRAGON_01.json"))
        row.addWidget(load); row.addWidget(example); row.addStretch(); layout.addLayout(row)
        self.battle_view = QPlainTextEdit(); self.battle_view.setReadOnly(True); layout.addWidget(self.battle_view)
        layout.addWidget(QLabel("V1 boundary: requirements and preparation plan are available. Live tactical battle control remains RESERVED_RL."))
        return page

    def _settings_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); group = QGroupBox("AI／API 設定"); form = QFormLayout(group)
        self.provider_combo = QComboBox(); self.provider_combo.addItem("Google Gemini")
        self.api_key = QLineEdit(); self.api_key.setEchoMode(QLineEdit.Password); self.api_key.setPlaceholderText("Stored with Windows DPAPI")
        self.model_name = QLineEdit(str(self.settings.value("gemini_model", "gemini-2.5-flash")))
        self.endpoint = QLineEdit(str(self.settings.value("gemini_endpoint", "https://generativelanguage.googleapis.com/v1beta/openai")))
        try: self.api_key.setText(self.secrets.load())
        except Exception: pass
        form.addRow("Provider", self.provider_combo); form.addRow("API Key", self.api_key); form.addRow("Model", self.model_name); form.addRow("Endpoint", self.endpoint)
        buttons = QHBoxLayout()
        for text, fn in (("儲存設定", self.save_api), ("測試連線", self.test_api), ("清除 API Key", self.clear_api)):
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
        m, a = self._require(); self.run_async("正在自動啟動部落衝突…", lambda: m.ensure_coc(a.index), lambda _: self.refresh_instances())
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

    def _apply_village_snapshot(self, snapshot) -> None:
        self.db.save_account(snapshot.tag, snapshot.raw, snapshot.entities)
        self.current_account_tag = snapshot.tag; rows = self.db.account_rows(snapshot.tag)
        self.account_label.setText(f"帳號：{snapshot.tag} — {len(rows)} 筆資料")
        self.account_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [row.get("section"), row.get("data_id"), row.get("name") or "UNKNOWN", row.get("world") or "—",
                      row.get("category") or "—", row.get("level"), row.get("count"), row.get("next_level"),
                      row.get("upgrade_cost"), row.get("upgrade_seconds")]
            for column, value in enumerate(values): self.account_table.setItem(row_index, column, QTableWidgetItem("—" if value is None else str(value)))
        self.account_summary.setPlainText(f"已從剪貼簿匯入村莊 JSON。已知／未知資料都已保存，共 {len(rows)} 筆。")

    def import_clipboard_village(self) -> None:
        try:
            text = QApplication.clipboard().text().strip()
            if not text: raise ValueError("剪貼簿是空的")
            self._apply_village_snapshot(parse_village_text(text))
        except Exception as exc: self._error("剪貼簿 JSON 匯入失敗", str(exc))

    def ai_import_village(self) -> None:
        m, a = self._require(); provider = self.provider()
        self.account_summary.setPlainText("AI 正在尋找 JSON／複製按鈕，請稍候…")
        def locate(png: bytes, goal: str) -> tuple[int, int]:
            prompt = ("分析這張 MuMu 畫面並尋找" + goal + "。只回傳 JSON："
                      '{"found":true,"x_pct":50.0,"y_pct":50.0}。座標是畫面百分比；找不到則 found=false。')
            raw = provider.generate(prompt, png).strip().replace("```json", "").replace("```", "")
            data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            if not data.get("found"): raise RuntimeError(f"AI 找不到{goal}")
            return int(float(data["x_pct"]) * 16), int(float(data["y_pct"]) * 9)
        def task() -> None:
            active = m.ensure_coc(a.index)
            png = m.screenshot(active); x, y = locate(png, "可開啟村莊 JSON 資料的入口")
            m.tap(active, x, y); import time; time.sleep(2)
            png = m.screenshot(active); x, y = locate(png, "複製完整 JSON 到剪貼簿的按鈕")
            m.tap(active, x, y); time.sleep(2)
        def done(_: None) -> None:
            self.import_clipboard_village()
        self.run_async("AI 正在取得村莊 JSON…", task, done)

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
        return json.dumps(self.db.account_rows(self.current_account_tag)[:40], ensure_ascii=False)

    def knowledge_context(self) -> str:
        items = self.db.recent_knowledge(40)
        return "\n".join(f"- [{item['status']}] {item['statement']}" for item in items) or "尚無使用者教學。"

    def analyze_frame(self) -> None:
        if not self.current_frame:
            self.capture(); QMessageBox.information(self, "Screenshot", "已開始取得畫面；完成後請再按 Analyze。") ; return
        frame = self.current_frame; provider = self.provider(); prompt = vision_prompt(frame.emulator_id, frame.frame_id, self.account_context())
        self.chat_history.appendPlainText("\nAI 正在分析目前畫面，請稍候…")
        self.run_async("AI 正在分析目前畫面…", lambda: provider.generate(prompt, frame.png), lambda result: self.chat_history.appendPlainText(f"\n畫面分析 [{frame.frame_id}]\n{result}\n"))

    def live_ai_test(self) -> None:
        m, a = self._require()
        self.tabs.setCurrentIndex(2)
        self.chat_history.appendPlainText("\n實機測試：正在啟動 CoC、擷取畫面並等待 AI 回覆…")
        def task() -> tuple[bytes, str]:
            active = m.ensure_coc(a.index)
            png = m.screenshot(active)
            answer = self.provider().generate(
                "你是部落衝突助手。請用繁體中文簡短回答：你現在看到什麼畫面？列出兩個可見重點。", png)
            return png, answer
        def done(result: tuple[bytes, str]) -> None:
            png, answer = result
            self.frame_sequence += 1
            self.current_frame = Frame.create(a.emulator_id, self.current_account_tag, png, self.frame_sequence)
            self.chat_history.appendPlainText(f"\n實機 AI 回覆\n{answer}\n")
            proof_path = os.environ.get("COC_LIVE_TEST_SCREENSHOT", "").strip()
            if proof_path:
                QTimer.singleShot(800, lambda: self.grab().save(proof_path, "PNG"))
        self.run_async("實機 AI 測試進行中，請等待回覆…", task, done)

    def send_chat(self) -> None:
        text = self.chat_input.text().strip()
        if not text: return
        should_remember = any(word in text for word in ("記住", "記下", "以後要", "下次要", "我教你"))
        if should_remember:
            self.db.add_knowledge(self.active.emulator_id if self.active else "", self.current_frame.frame_id if self.current_frame else "", text, "USER_CONFIRMED")
        recent = self.chat_history.toPlainText()[-3500:]
        self.chat_input.clear(); self.chat_history.appendPlainText(f"\n你\n{text}\n\nAI 正在思考，請稍候…")
        if any(word in text for word in ("打開", "開啟", "點擊", "按下", "進入", "返回", "關閉")) and self.active:
            self.execute_agent_command(text); return
        frame = self.current_frame if self.chat_image_pending else None
        context = (f"{AGENT_PROFILE}\n請用繁體中文簡潔回答。\n使用者已確認、必須長期遵守的教學：\n{self.knowledge_context()}\nCurrent account: {self.account_context()}\n"
                   f"Current emulator={self.active.emulator_id if self.active else 'none'}\nRecent conversation:\n{recent}\nUser: {text}")
        def done(result: str) -> None:
            self.chat_history.appendPlainText(f"\nAI 回覆\n{result}\n")
            if frame:
                self.chat_image_pending = False
                self.chat_image_preview.clear(); self.chat_image_preview.setText("尚未附加圖片（也可以將圖片拖進視窗）")
        self.run_async("AI 正在思考…", lambda: self.provider().generate(context, frame.png if frame else None), done)

    def execute_agent_command(self, command: str) -> None:
        m, a = self._require(); provider = self.provider()
        def task() -> tuple[bytes, str]:
            import time
            active = m.ensure_coc(a.index)
            last_png = b""
            for step in range(5):
                last_png = m.screenshot(active)
                prompt = (f"你正在控制部落衝突。使用者指令：{command}\n"
                          f"使用者過去確認的操作教學：\n{self.knowledge_context()}\n"
                          "檢查目前畫面是否已完成。只回傳單一 JSON，不要 markdown："
                          '{"done":false,"action":"tap|back|none","x_pct":50.0,"y_pct":50.0,"message":"繁體中文說明"}。'
                          "若已完成 done=true。禁止購買、花費資源、攻擊、刪除或確認不可逆操作。")
                raw = provider.generate(prompt, last_png).strip().replace("```json", "").replace("```", "")
                data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
                if data.get("done"):
                    return last_png, str(data.get("message") or "指令已完成")
                action = data.get("action")
                if action == "tap":
                    m.tap(active, int(float(data["x_pct"]) * 16), int(float(data["y_pct"]) * 9))
                elif action == "back": m.back(active)
                else: return last_png, str(data.get("message") or "AI 無法安全執行這個操作")
                time.sleep(2)
            return last_png, "已執行操作，但五次畫面確認後仍無法確認完成。"
        def done(result: tuple[bytes, str]) -> None:
            png, message = result
            self.frame_sequence += 1; self.current_frame = Frame.create(a.emulator_id, self.current_account_tag, png, self.frame_sequence)
            self.chat_history.appendPlainText(f"\nAI 操作結果\n{message}\n")
            proof_path = os.environ.get("COC_AGENT_SCREENSHOT", "").strip()
            if proof_path: QTimer.singleShot(800, lambda: self.grab().save(proof_path, "PNG"))
        self.run_async("AI 正在操作並確認 MuMu 畫面…", task, done)

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
    window = MainWindow(); window.show()
    if "--live-test" in sys.argv:
        QTimer.singleShot(2500, window.live_ai_test)
    for argument in sys.argv:
        if argument.startswith("--agent-command="):
            command = argument.split("=", 1)[1]
            def run_command(text=command):
                window.tabs.setCurrentIndex(2); window.chat_input.setText(text); window.send_chat()
            QTimer.singleShot(2500, run_command)
    return app.exec_()

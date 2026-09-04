# coding: utf-8
"""Phoenix 烧录独立窗口：小工具页入口弹出的完整烧录界面。

通过 PhoenixConsole.exe 命令行完成全志固件烧录（QProcess 异步，
不阻塞 UI）。窗口自管理：
- 工具路径（写 qconfig phoenixPath，供 MCP 桥共享）
- 固件/设备/擦除/重启/超时等参数拼装
- 进程输出流式展示，结束按退出码判定成败
- 关闭窗口即终止进程并释放全局烧录槽
"""
import ctypes
import sys
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import (
    QFileDialog, QGridLayout, QHBoxLayout, QPlainTextEdit, QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, FluentIcon,
    InfoBar, LineEdit, MessageBox, PrimaryPushButton, PushButton, SpinBox,
    isDarkTheme,
)

from app.adb_runner import AdbProbe, _parse_devices_text, find_adb
from app.config import cfg, qconfig
from app.phoenix_runner import (
    PhoenixProcess, build_command, find_phoenix, parse_version,
    release_burn, try_acquire_burn,
)
from app.ui.window_utils import center_window

_LABEL_W = 92
_ERASE_ITEMS = [
    ("不擦除", -1),
    ("0 产品模式（跟随固件擦除标志）", 0),
    ("1 产品模式（全部擦除）", 1),
    ("10 升级模式（保留用户数据）", 10),
    ("11 升级模式（擦除逻辑分区）", 11),
    ("12 升级模式（全部擦除）", 12),
]
_AUTO_SERIAL = "（自动检测）"


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class PhoenixWindow(QWidget):
    """Phoenix 烧录独立窗口（非模态，单例由小工具页保证）。"""

    def __init__(self, parent=None):
        super().__init__(None, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("Phoenix 烧录")
        self.resize(820, 660)
        self.setMinimumSize(720, 540)

        self._runner = PhoenixProcess(self)
        self._runner.started.connect(self._on_proc_started)
        self._runner.dataReceived.connect(self._on_data)
        self._runner.stopped.connect(self._on_stopped)
        self._mode = ""                # "" | "probe" | "burn"
        self._probe_buf = bytearray()
        self._holding_slot = False     # 已占用全局烧录槽（烧录结束后释放）
        self._manual_stop = False      # 本次结束是否由用户手动停止触发

        self._apply_window_palette()
        self._build_ui()
        center_window(self)
        if qconfig.get(cfg.phoenixPath):
            self.exeEdit.setText(qconfig.get(cfg.phoenixPath))
            QTimer.singleShot(300, self._probe_version)

    # ── 界面 ─────────────────────────────────────────────────

    def _apply_window_palette(self):
        palette = self.palette()
        palette.setColor(
            QPalette.ColorRole.Window,
            QColor(32, 39, 46) if isDarkTheme() else QColor(245, 247, 250))
        self.setPalette(palette)
        self.setAutoFillBackground(True)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(10)

        tip = CaptionLabel(
            "通过 PhoenixConsole 命令行烧录全志固件：设备需先在 ADB 可见"
            "（工具会自动引导其进入烧录模式），首次烧录前请安装 AW 驱动。",
            self)
        tip.setWordWrap(True)
        root.addWidget(tip)

        root.addWidget(self._build_config_card())
        root.addWidget(self._build_action_card())

        self.outView = QPlainTextEdit(self)
        self.outView.setReadOnly(True)
        self.outView.setMaximumBlockCount(5000)
        self.outView.setFont(QFont("Consolas", 9))
        pal = self.outView.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor("#1E1E1E"))
        pal.setColor(QPalette.ColorRole.Text, QColor("#DCDCDC"))
        self.outView.setPalette(pal)
        root.addWidget(self.outView, 1)

        self._set_running_ui(False)

    def _build_config_card(self) -> CardWidget:
        card = CardWidget(self)
        grid = QGridLayout(card)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        # 工具路径
        grid.addWidget(BodyLabel("工具路径", card), 0, 0)
        grid.setColumnMinimumWidth(0, _LABEL_W)
        self.exeEdit = LineEdit(card)
        self.exeEdit.setPlaceholderText("PhoenixConsole.exe 完整路径或安装目录")
        grid.addWidget(self.exeEdit, 0, 1)
        browseBtn = PushButton(FluentIcon.FOLDER, "浏览…", card)
        browseBtn.clicked.connect(self._browse_exe)
        grid.addWidget(browseBtn, 0, 2)
        self.versionLabel = CaptionLabel("", card)
        grid.addWidget(self.versionLabel, 0, 3)

        # 固件
        grid.addWidget(BodyLabel("固件镜像", card), 1, 0)
        self.imageEdit = LineEdit(card)
        self.imageEdit.setPlaceholderText("全志固件包（*.img）")
        grid.addWidget(self.imageEdit, 1, 1)
        imageBtn = PushButton(FluentIcon.FOLDER, "浏览…", card)
        imageBtn.clicked.connect(self._browse_image)
        grid.addWidget(imageBtn, 1, 2)

        # 设备与数量
        grid.addWidget(BodyLabel("烧录设备", card), 2, 0)
        devRow = QHBoxLayout()
        devRow.setSpacing(8)
        self.devCombo = ComboBox(card)
        self.devCombo.addItem(_AUTO_SERIAL)
        devRow.addWidget(self.devCombo, 1)
        self.refreshDevBtn = PushButton(FluentIcon.SYNC, "刷新设备", card)
        self.refreshDevBtn.clicked.connect(self._refresh_devices)
        devRow.addWidget(self.refreshDevBtn)
        self.countSpin = SpinBox(card)
        self.countSpin.setRange(1, 8)
        self.countSpin.setValue(1)
        self.countSpin.setFixedWidth(128)
        devRow.addWidget(self.countSpin)
        devRow.addWidget(CaptionLabel("台", card))
        devRow.addStretch(1)
        grid.addLayout(devRow, 2, 1, 1, 3)

        # 擦除 / 重启 / 超时
        grid.addWidget(BodyLabel("擦除模式", card), 3, 0)
        optRow = QHBoxLayout()
        optRow.setSpacing(8)
        self.eraseCombo = ComboBox(card)
        for text, value in _ERASE_ITEMS:
            self.eraseCombo.addItem(text, userData=value)
        # 默认 1 产品模式（全部擦除）
        self.eraseCombo.setCurrentIndex(self.eraseCombo.findData(1))
        optRow.addWidget(self.eraseCombo, 1)
        self.rebootCheck = CheckBox("烧录后重启", card)
        self.rebootCheck.setChecked(True)
        optRow.addWidget(self.rebootCheck)
        optRow.addWidget(BodyLabel("超时", card))
        self.timeoutSpin = SpinBox(card)
        self.timeoutSpin.setRange(30, 3600)
        self.timeoutSpin.setValue(300)
        self.timeoutSpin.setSuffix(" s")
        optRow.addWidget(self.timeoutSpin)
        optRow.addStretch(1)
        grid.addLayout(optRow, 3, 1, 1, 3)

        grid.setColumnStretch(1, 1)
        return card

    def _build_action_card(self) -> CardWidget:
        card = CardWidget(self)
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)

        self.startBtn = PrimaryPushButton(FluentIcon.PLAY, "开始烧录", card)
        self.startBtn.clicked.connect(self._start_burn)
        row.addWidget(self.startBtn)

        self.stopBtn = PushButton(FluentIcon.CANCEL, "停止", card)
        self.stopBtn.clicked.connect(self._stop_burn)
        row.addWidget(self.stopBtn)

        self.driverBtn = PushButton(FluentIcon.ALBUM, "安装 AW 驱动", card)
        self.driverBtn.clicked.connect(self._install_driver)
        row.addWidget(self.driverBtn)

        self.clearBtn = PushButton(FluentIcon.DELETE, "清空输出", card)
        self.clearBtn.clicked.connect(lambda: self.outView.clear())
        row.addWidget(self.clearBtn)

        row.addStretch(1)
        return card

    # ── 工具路径 / 版本探测 ──────────────────────────────────

    def _browse_exe(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 PhoenixConsole.exe", "",
            "PhoenixConsole (PhoenixConsole.exe);;All Files (*)")
        if not path:
            return
        qconfig.set(cfg.phoenixPath, path)
        self.exeEdit.setText(path)
        self._probe_version()

    def _browse_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择固件镜像", "", "固件镜像 (*.img);;All Files (*)")
        if path:
            self.imageEdit.setText(path)

    def _current_exe(self):
        """把输入框当前路径同步回配置，再解析 exe 路径。返回 (path, err)。"""
        qconfig.set(cfg.phoenixPath, self.exeEdit.text().strip())
        return find_phoenix(qconfig.get(cfg.phoenixPath))

    def _probe_version(self):
        if self._runner.is_running() or self._mode == "burn":
            return
        path, err = self._current_exe()
        if not path:
            self.versionLabel.setText("未检测到工具")
            return
        self._mode = "probe"
        self._probe_buf.clear()
        self.versionLabel.setText("检测中…")
        self._runner.start(path, ["-v"])

    # ── 设备枚举（复用 AdbProbe 异步探测，不阻塞 UI）──────────

    def _refresh_devices(self):
        adb, err = find_adb(qconfig.get(cfg.adbPath))
        if not adb:
            InfoBar.error(title="刷新设备失败", content=err, duration=5000,
                          parent=self)
            return
        self.refreshDevBtn.setEnabled(False)
        self._dev_probe = AdbProbe(self)
        self._dev_probe.finished.connect(self._on_devices)
        self._dev_probe.start(adb, ["devices", "-l"], timeout_ms=6000)

    def _on_devices(self, data: bytes, _code: int, error: str):
        self.refreshDevBtn.setEnabled(True)
        if error:
            InfoBar.error(title="刷新设备失败", content=error, duration=5000,
                          parent=self)
            return
        text = bytes(data).decode("utf-8", "replace")
        devs = _parse_devices_text(text)   # adb_runner 内部解析器
        current = self.devCombo.currentData()
        self.devCombo.clear()
        self.devCombo.addItem(_AUTO_SERIAL)
        for d in devs:
            if d.get("state") == "device":
                self.devCombo.addItem(d["serial"], userData=d["serial"])
        idx = self.devCombo.findData(current)
        if idx >= 0:
            self.devCombo.setCurrentIndex(idx)
        self._dev_probe = None

    # ── 烧录流程 ─────────────────────────────────────────────

    def _start_burn(self):
        path, err = self._current_exe()
        if not path:
            InfoBar.error(title="无法开始烧录", content=err, duration=6000,
                          parent=self)
            return
        image = self.imageEdit.text().strip()
        if not image:
            InfoBar.error(title="无法开始烧录", content="请先选择固件镜像",
                          duration=5000, parent=self)
            return
        from pathlib import Path
        if not Path(image).is_file():
            InfoBar.error(title="无法开始烧录",
                          content=f"固件文件不存在：{image}", duration=6000,
                          parent=self)
            return
        if not try_acquire_burn():
            InfoBar.error(title="已有烧录任务进行中",
                          content="GUI 或 AI（MCP）调用正在烧录，请等待完成",
                          duration=6000, parent=self)
            return
        serial = self.devCombo.currentData() or ""
        try:
            args = build_command(
                path, image, self.countSpin.value(), serial=serial,
                erase=self.eraseCombo.currentData(),
                reboot=self.rebootCheck.isChecked(),
                timeout_s=self.timeoutSpin.value())
        except ValueError as e:
            release_burn()
            InfoBar.error(title="参数错误", content=str(e), duration=6000,
                          parent=self)
            return

        self._mode = "burn"
        self._holding_slot = True
        self._manual_stop = False
        self.outView.clear()
        self._log(f"PhoenixConsole {' '.join(args[1:])}")
        self._log("开始烧录…")
        self._set_running_ui(True)
        self._runner.start(path, args)

    def _stop_burn(self):
        if self._mode == "burn" and self._runner.is_running():
            self._manual_stop = True
            self._log("请求停止，正在终止进程…")
            self.stopBtn.setEnabled(False)
            self._runner.stop()

    def _install_driver(self):
        path, err = self._current_exe()
        if not path:
            InfoBar.error(title="安装驱动失败", content=err, duration=6000,
                          parent=self)
            return
        box = MessageBox(
            "安装 AW 驱动",
            "将以管理员身份运行 PhoenixConsole.exe -d 安装 AW USB 量产驱动，"
            "期间会弹出 UAC 授权窗口，请确认。",
            self)
        box.yesButton.setText("安装")
        box.cancelButton.setText("取消")
        if not box.exec():   # QDialog.DialogCode.Rejected
            return
        if sys.platform != "win32":
            InfoBar.error(title="安装驱动失败",
                          content="仅在 Windows 支持自动提权安装",
                          duration=5000, parent=self)
            return
        import os
        res = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", path, "-d",
            str(os.path.dirname(path)), 1)
        if res <= 32 and res != 1223:   # 1223 = 用户取消 UAC
            InfoBar.error(title="安装驱动失败",
                          content=f"提权执行失败（错误码 {res}）",
                          duration=6000, parent=self)

    # ── 进程回调 ─────────────────────────────────────────────

    def _on_proc_started(self):
        if self._mode == "burn":
            self._log(f"[{_stamp()}] 进程已启动")

    def _on_data(self, data: bytes):
        if self._mode == "probe":
            self._probe_buf.extend(bytes(data))
            return
        text = bytes(data).decode("utf-8", "replace")
        for line in text.replace("\r\n", "\n").split("\n"):
            if line.strip():
                self._log(line.rstrip())

    def _on_stopped(self, code: int, err: str):
        if self._mode == "probe":
            self._mode = ""
            text = bytes(self._probe_buf).decode("utf-8", "replace")
            ver = parse_version(text)
            if err:
                self.versionLabel.setText("探测失败")
            else:
                self.versionLabel.setText(
                    f"版本 {ver}" if ver else "未能解析版本")
            return
        self._mode = ""
        if err:
            self._log(f"[{_stamp()}] 进程错误：{err}")
        self._log(f"[{_stamp()}] 进程结束，退出码 {code}")
        ok = (not self._manual_stop) and (not err) and code == 0
        self._release_slot()
        self._set_running_ui(False)
        if self._manual_stop:
            InfoBar.error(title="已手动停止", content="烧录进程已终止",
                          duration=5000, parent=self)
        elif ok:
            InfoBar.success(title="烧录完成",
                            content="PhoenixConsole 已成功返回，详见输出",
                            duration=6000, parent=self)
        else:
            InfoBar.error(title="烧录失败",
                          content=err or f"退出码 {code}，详见输出",
                          duration=8000, parent=self)

    def _release_slot(self):
        if self._holding_slot:
            self._holding_slot = False
            release_burn()

    def _set_running_ui(self, running: bool):
        self.startBtn.setEnabled(not running)
        self.stopBtn.setEnabled(running)
        self.refreshDevBtn.setEnabled(True)

    def _log(self, text: str):
        self.outView.appendPlainText(f"[{_stamp()}] {text}")

    # ── 生命周期 ─────────────────────────────────────────────

    def shutdown(self):
        """终止在途进程并释放烧录槽（窗口关闭/主程序退出时调用）。"""
        if self._runner.is_running():
            self._runner.shutdown()
        self._release_slot()
        self._mode = ""

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

# coding: utf-8
"""主窗口：SplitFluentWindow + 三个页面 + 端口轮询 + 优雅停机。"""
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QCloseEvent

from qfluentwidgets import FluentIcon, NavigationItemPosition, SplitFluentWindow

from app.config import cfg, qconfig
from app.serial_worker import SerialThread
from app.ui.adb_page import AdbPage
from app.ui.console_page import ConsolePage
from app.ui.preset_page import PresetPage
from app.ui.setting_page import SettingPage


class MainWindow(SplitFluentWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("串口调试工具")
        self.resize(1220, 780)

        # 串口工作线程（唯一持有 serial.Serial）
        self.st = SerialThread(self)
        self.st.start()

        self.consolePage = ConsolePage(self.st)
        self.presetPage = PresetPage()
        self.adbPage = AdbPage()
        self.settingPage = SettingPage()
        self.consolePage.setObjectName("consoleInterface")
        self.presetPage.setObjectName("presetInterface")
        self.adbPage.setObjectName("adbInterface")
        # settingInterface 的 objectName 已在 SettingPage 内设置

        self.addSubInterface(self.consolePage, FluentIcon.IOT, "串口调试")
        self.addSubInterface(self.presetPage, FluentIcon.LIBRARY, "预设命令")
        self.addSubInterface(self.adbPage, FluentIcon.COMMAND_PROMPT, "ADB 调试")
        self.addSubInterface(
            self.settingPage, FluentIcon.SETTING, "设置",
            position=NavigationItemPosition.BOTTOM)

        # 设置里改默认型号 -> ADB 页同步重载（lambda 屏蔽信号参数）
        self.settingPage.modelCard.modelChanged.connect(
            lambda _stem: self.adbPage.reload_models())

        # 跨页接线
        self.presetPage.sendRequested.connect(self.st.sigWrite.emit)
        self.settingPage.maxCharsChanged.connect(
            self.consolePage.receivePanel.setMaxChars)
        self.consolePage.receivePanel.setMaxChars(qconfig.get(cfg.maxChars))

        # 端口热插拔轮询（UI 线程轻量操作）
        self._portTimer = QTimer(self)
        self._portTimer.setInterval(2000)
        self._portTimer.timeout.connect(self.consolePage.refreshPorts)
        self._portTimer.start()

        self.switchTo(self.consolePage)

    def closeEvent(self, event: QCloseEvent):
        self._portTimer.stop()
        self.consolePage.shutdown()
        self.presetPage.shutdown()
        self.adbPage.shutdown()
        self.st.stop()
        super().closeEvent(event)

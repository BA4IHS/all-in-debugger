# coding: utf-8
"""主窗口：SplitFluentWindow + 三个页面 + 端口轮询 + 优雅停机。"""
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QCloseEvent

from qfluentwidgets import (
    FluentIcon, FluentIconBase, NavigationItemPosition, SplitFluentWindow,
    Theme, getIconColor,
)

from app.config import cfg, qconfig
from app.dap_worker import DapThread
from app.hid_worker import HidThread
from app.modbus_core import ModbusThread
from app.serial_worker import SerialThread
from app.ssh_worker import SshThread
from app.ui.adb_page import AdbPage
from app.ui.console_page import ConsolePage
from app.ui.dap_page import DapPage
from app.ui.hid_page import HidPage
from app.ui.modbus_page import ModbusPage
from app.ui.preset_page import PresetPage
from app.ui.setting_page import SettingPage
from app.ui.ssh_page import SshPage
from app.ui.window_utils import center_window

ANDROID_ICON_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "android.svg"
)
ANDROID_ICON_DARK_PATH = ANDROID_ICON_PATH.with_name("android_white.svg")


class AndroidIcon(FluentIconBase):
    """与 Fluent 侧栏图标保持相同尺寸并自动适配明暗主题。"""

    def path(self, theme=Theme.AUTO):
        if getIconColor(theme) == "white":
            return str(ANDROID_ICON_DARK_PATH)
        return str(ANDROID_ICON_PATH)


ANDROID_ICON = AndroidIcon()


class MainWindow(SplitFluentWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("all-in-debugger")
        self.resize(1220, 780)

        # 串口工作线程（唯一持有 serial.Serial）
        self.st = SerialThread(self)
        self.st.start()

        # 新增调试通道工作线程（各自唯一持有原生句柄）
        self.ht = HidThread(self)
        self.ht.start()
        self.dt = DapThread(self)
        self.dt.start()
        self.mt = ModbusThread(self)
        self.mt.start()
        self.sht = SshThread(self)
        self.sht.start()

        # 内嵌 MCP 服务（可选）：mcp 依赖缺失时仅禁用该功能，不影响 GUI
        self._mcpService = None
        if qconfig.get(cfg.mcpEnabled):
            try:
                from app.mcp_bridge import WorkerBridge
                from app.mcp_server import McpService
                bridge = WorkerBridge(self.st, self.ht, self.dt, self.mt,
                                      self.sht)
                self._mcpService = McpService(
                    bridge, qconfig.get(cfg.mcpPort),
                    qconfig.get(cfg.mcpToken))
                self._mcpService.start()
            except Exception:
                self._mcpService = None

        self.consolePage = ConsolePage(self.st)
        self.presetPage = PresetPage()
        self.adbPage = AdbPage()
        self.hidPage = HidPage(self.ht)
        self.dapPage = DapPage(self.dt)
        self.modbusPage = ModbusPage(self.mt)
        self.sshPage = SshPage(self.sht)
        self.settingPage = SettingPage()
        self.consolePage.setObjectName("consoleInterface")
        self.presetPage.setObjectName("presetInterface")
        self.adbPage.setObjectName("adbInterface")
        self.hidPage.setObjectName("hidInterface")
        self.dapPage.setObjectName("dapInterface")
        self.modbusPage.setObjectName("modbusInterface")
        self.sshPage.setObjectName("sshInterface")
        # settingInterface 的 objectName 已在 SettingPage 内设置

        self.addSubInterface(self.consolePage, FluentIcon.IOT, "串口调试")
        self.addSubInterface(self.presetPage, FluentIcon.LIBRARY, "预设命令")
        self.addSubInterface(self.adbPage, ANDROID_ICON, "ADB 调试")
        self.addSubInterface(self.hidPage, FluentIcon.CONNECT, "HID 调试")
        self.addSubInterface(self.dapPage, FluentIcon.DEVELOPER_TOOLS,
                             "DAP RTT")
        self.addSubInterface(self.modbusPage, FluentIcon.LINK, "Modbus")
        self.addSubInterface(self.sshPage, FluentIcon.GLOBE, "SSH")
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
        center_window(self)

    def closeEvent(self, event: QCloseEvent):
        self._portTimer.stop()
        if self._mcpService is not None:
            self._mcpService.stop()
        self.consolePage.shutdown()
        self.presetPage.shutdown()
        self.adbPage.shutdown()
        self.hidPage.shutdown()
        self.dapPage.shutdown()
        self.modbusPage.shutdown()
        self.sshPage.shutdown()
        self.settingPage.shutdown()
        self.st.stop()
        self.ht.stop()
        self.dt.stop()
        self.mt.stop()
        self.sht.stop()
        super().closeEvent(event)

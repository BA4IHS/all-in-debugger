# coding: utf-8
"""主窗口：SplitFluentWindow + 三个页面 + 端口轮询 + 优雅停机。"""
from pathlib import Path

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QCloseEvent, QColor

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
from app.tcpip_worker import TcpipThread
from app.ui.adb_page import AdbPage
from app.ui.console_page import ConsolePage
from app.ui.dap_page import DapPage
from app.ui.hid_page import HidPage
from app.ui.modbus_page import ModbusPage
from app.ui.preset_page import PresetPage
from app.ui.setting_page import SettingPage
from app.ui.ssh_page import SshPage
from app.ui.tcpip_page import TcpipPage
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
        self.tp = TcpipThread(self)
        self.tp.start()

        # 内嵌 MCP 服务（可选）：mcp 依赖缺失时仅禁用该功能，不影响 GUI
        self._mcpService = None
        if qconfig.get(cfg.mcpEnabled):
            try:
                from app.mcp_bridge import WorkerBridge
                from app.mcp_server import McpService
                bridge = WorkerBridge(self.st, self.ht, self.dt, self.mt,
                                      self.sht, self.tp)
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
        self.tcpipPage = TcpipPage(self.tp)
        self.settingPage = SettingPage()
        self.consolePage.setObjectName("consoleInterface")
        self.presetPage.setObjectName("presetInterface")
        self.adbPage.setObjectName("adbInterface")
        self.hidPage.setObjectName("hidInterface")
        self.dapPage.setObjectName("dapInterface")
        self.modbusPage.setObjectName("modbusInterface")
        self.sshPage.setObjectName("sshInterface")
        self.tcpipPage.setObjectName("tcpipInterface")
        # settingInterface 的 objectName 已在 SettingPage 内设置

        self.addSubInterface(self.consolePage, FluentIcon.IOT, "串口调试")
        self.addSubInterface(self.presetPage, FluentIcon.LIBRARY, "预设命令")
        self.addSubInterface(self.adbPage, ANDROID_ICON, "ADB 调试")
        self.addSubInterface(self.hidPage, FluentIcon.CONNECT, "HID 调试")
        self.addSubInterface(self.dapPage, FluentIcon.DEVELOPER_TOOLS,"DAP RTT")
        self.addSubInterface(self.modbusPage, FluentIcon.LINK, "Modbus")
        self.addSubInterface(self.sshPage, FluentIcon.GLOBE, "SSH")
        self.addSubInterface(self.tcpipPage, FluentIcon.WIFI, "网络调试")
        self.addSubInterface(
            self.settingPage, FluentIcon.SETTING, "设置",
            position=NavigationItemPosition.BOTTOM)

        # 侧边栏展开宽度（按需调整，默认约 330）
        self.navigationInterface.panel.setExpandWidth(160)

        # 设置里改默认型号 -> ADB 页立即重载并选中新型号（无需重启）
        self.settingPage.modelCard.modelChanged.connect(
            lambda _stem: self.adbPage.reload_models(preselect_default=True))

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

        # 1) 标题跟随侧边栏（标题文字与内容区左缘对齐）：
        #    - SplitFluentWindow 的侧边栏是浮层（titleBar 需 raise_ 才不被盖住），
        #      展开侧边栏后标题若不右移，会被侧边栏遮挡且横跨两种背景色。
        #    - 监听侧边栏宽度动画实时跟随：展开 160 / 折叠 48。
        #    - 文字实际起点 = 侧边栏宽 + 标题栏内部 12 间距 + 18 图标。
        #      TITLE_LEFT_PAD 用来抵消这 30px：
        #      * -30：文字左缘与内容区左缘完全对齐（图标被侧边栏盖住）
        #      * -12：图标紧贴侧边栏右缘可见，文字略靠右 18px
        #      *   0：初始 78px 偏右（默认位置不调整时）
        #    - 旧做法（固定右移、不跟随）：
        #      self.titleBar.hBoxLayout.setContentsMargins(128, 0, 0, 0)
        TITLE_LEFT_PAD = -30
        self.navigationInterface.panel.expandAni.valueChanged.connect(
            lambda r: self.titleBar.hBoxLayout.setContentsMargins(
                r.width() + TITLE_LEFT_PAD, 0, 0, 0))
        self.navigationInterface.panel.displayModeChanged.connect(
            lambda _m: self.titleBar.hBoxLayout.setContentsMargins(
                self.navigationInterface.panel.width() + TITLE_LEFT_PAD,
                0, 0, 0))
        # 启动时同步一次（默认折叠 48px，动画连接不触发）
        self.titleBar.hBoxLayout.setContentsMargins(
            self.navigationInterface.panel.width() + TITLE_LEFT_PAD, 0, 0, 0)
        #    - 图标与文字间距
        # self.titleBar.hBoxLayout.setSpacing(8)
        #    - 文字上下微调（正数下移）
        # self.titleBar.titleLabel.setContentsMargins(0, 2, 0, 0)

        # 2) 整窗透明度（0~1，含标题栏；1 为不透明）
        # self.setWindowOpacity(1.0)

        # 3) Win11 云母半透明（项目默认已开启；Win10 无效并自动回退纯色）
        # self.setMicaEffectEnabled(True)

        # 4) 自定义明/暗主题窗口背景色（light, dark）
        # self.setCustomBackgroundColor(QColor(240, 244, 249), QColor(32, 32, 32))

        # 5) 仅标题栏底色半透明（页面内容不穿透，仅标题栏变色）
        #    必须「追加」到已有样式表：直接 setStyleSheet 会整个替换掉
        #    FLUENT_WINDOW 内置样式（标题文字/按钮图标样式全部丢失）。
        #    类型选择器 TitleBar 只作用标题栏自身背景，不影响子控件按钮。
        #    暗色主题建议 rgba(32,32,32,160)（加深），浅色主题建议
        #    rgba(255,255,255,120)（提亮）。
        # self.titleBar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # self.titleBar.setStyleSheet(
        #     self.titleBar.styleSheet()
        #     + " TitleBar { background-color: rgba(32, 32, 32, 160); }")

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
        self.tcpipPage.shutdown()
        self.settingPage.shutdown()
        self.st.stop()
        self.ht.stop()
        self.dt.stop()
        self.mt.stop()
        self.sht.stop()
        self.tp.stop()
        super().closeEvent(event)

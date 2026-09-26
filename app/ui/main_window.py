# coding: utf-8
"""主窗口：SplitFluentWindow + 三个页面 + 端口轮询 + 优雅停机。"""
import logging
import time
from pathlib import Path

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QColor

from qfluentwidgets import (
    FluentIcon, FluentIconBase, InfoBar, NavigationItemPosition,
    SplitFluentWindow, Theme, getIconColor, isDarkTheme,
)

from app.config import cfg, qconfig
from app.ch347_worker import Ch347Thread
from app.dap_worker import DapThread
from app.hid_worker import HidThread
from app.modbus_core import ModbusThread
from app.serial_worker import SerialThread
from app.ssh_worker import SshThread
from app.tcpip_worker import TcpipThread
from app.ui.console_page import ConsolePage
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
    """非首屏页面延迟构造：

    启动只同步构造 ConsolePage（首屏），其余页面在窗口显示后分批创建
    （每批之间让出事件循环，避免界面卡顿）。页面引用统一从
    `self.pages` / `page()` 获取，未构造完成前为 None。
    """

    # 加载进度（供启动遮罩显示）：已完成数 / 总数 / 页面名
    lazyProgress = pyqtSignal(int, int, str)
    lazyFinished = pyqtSignal()

    # 延迟页面分批表：每批（工厛建元组, [ (页面工厂, 导航图标, 导航标题, objectName) ])
    _LAZY_BATCHES = [
        ("preset", "app.ui.preset_page", "PresetPage", None,
         FluentIcon.LIBRARY, "预设命令"),
        ("adb", "app.ui.adb_page", "AdbPage", None,
         ANDROID_ICON, "ADB 调试"),
        ("hid", "app.ui.hid_page", "HidPage", "ht",
         FluentIcon.CONNECT, "HID 调试"),
        ("dap", "app.ui.dap_page", "DapPage", "dt",
         FluentIcon.DEVELOPER_TOOLS, "DAP RTT"),
        ("modbus", "app.ui.modbus_page", "ModbusPage", "mt",
         FluentIcon.LINK, "Modbus"),
        ("ssh", "app.ui.ssh_page", "SshPage", "sht",
         FluentIcon.GLOBE, "SSH"),
        ("tcpip", "app.ui.tcpip_page", "TcpipPage", "tp",
         FluentIcon.WIFI, "网络调试"),
        ("ch347", "app.ui.ch347_page", "Ch347Page", "cct",
         FluentIcon.TILES, "CH347 调试"),
        ("tools", "app.ui.tools_page", "ToolsPage", None,
         FluentIcon.APPLICATION, "小工具"),
    ]

    def __init__(self):
        t0 = time.monotonic()
        super().__init__()
        self.setWindowTitle("all-in-debugger")
        self.resize(1220, 780)
        # 显式下限覆盖布局自动传播的最小尺寸：Qt 会取所有子页面
        # minimumSizeHint 的最大值作为窗口最小尺寸（实测 932×534），
        # 导致窗口只能调大无法调小；此处显式设小后即可自由缩放。
        self.setMinimumSize(700, 480)

        # 延迟页面注册表：key → 页面实例（构造完成后才存在）
        self.pages: dict = {}

        # 页面切换转场：把默认的位移滑入替换为交叉淡化（须在首个
        # addSubInterface 之前，此时内部 view 为空，直接换新 view）
        self._installCrossfadeView()

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
        self.cct = Ch347Thread(self)
        self.cct.start()

        # 内嵌 MCP 服务（可选）：推迟到全部页面构造完成（lazyFinished）后
        # 再初始化。mcp_bridge/mcp_server 的 import 会拉进 mcp/uvicorn/
        # anyio/httpx/pydantic 等重型依赖（主线程同步 import，约 200ms），
        # 若放在 __init__ 或事件循环头部会与 9 个 lazy 页面的构造竞争
        # 主线程，拖慢首屏可交互时间；放到 lazyFinished 后则完全不影响。
        self._mcpService = None
        if qconfig.get(cfg.mcpEnabled):
            self.lazyFinished.connect(self._initMcpService)

        # 首屏：串口调试页同步构造（启动即显示、即交互）
        self.consolePage = ConsolePage(self.st)
        self.consolePage.setObjectName("consoleInterface")
        self.consolePage.receivePanel.setMaxChars(qconfig.get(cfg.maxChars))
        self.addSubInterface(self.consolePage, FluentIcon.IOT, "串口调试")

        # 侧边栏展开宽度（按需调整，默认约 330）
        self.navigationInterface.panel.setExpandWidth(160)
        # 移除左上角返回键：FluentWindow 基类默认 showReturnButton=True
        # （供堆栈导航后退），本应用单层页面切换无导航历史，该键无用途
        self.navigationInterface.setReturnButtonVisible(False)

        # 端口热插拔轮询（UI 线程轻量操作，仅依赖 consolePage）
        self._portTimer = QTimer(self)
        self._portTimer.setInterval(2000)
        self._portTimer.timeout.connect(self.consolePage.refreshPorts)
        self._portTimer.start()

        self.switchTo(self.consolePage)

        # 其余页面在窗口显示后分批构造（见 showEvent）

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

        center_window(self)
        logging.getLogger(__name__).info(
            "主窗口构造完成：首屏=串口调试 worker线程=7 耗时=%.0fms",
            (time.monotonic() - t0) * 1000)

    def logStartupInfo(self, t0: float):
        """记录窗口与屏幕环境信息（show 后调用，几何/屏幕已就位）。

        另挂一次性 lazyFinished 监听：全部页面构造完成时补记总启动耗时
        （两种 splash 模式下本方法都在 startLazyBuild 触发前调用，
        信号不会错过；重复连接用 UniqueConnection 防护）。
        """
        log = logging.getLogger(__name__)
        g = self.frameGeometry()
        scr = self.screen()
        if scr is not None:
            geo = scr.geometry()
            log.info(
                "屏幕：%s %dx%d 刷新率=%.0fHz DPR=%.2f",
                scr.name(), geo.width(), geo.height(),
                scr.refreshRate(), self.devicePixelRatioF())
        log.info(
            "窗口：frame=%dx%d+%d+%d 客户区=%dx%d 主题=%s Mica材质=开启(Win11)",
            g.width(), g.height(), g.x(), g.y(),
            self.width(), self.height(),
            "深色" if isDarkTheme() else "浅色")

        def _done():
            log.info("启动完成：页面=%d 总耗时=%.0fms",
                     len(self.pages) + 1,
                     (time.monotonic() - t0) * 1000)
        self.lazyFinished.connect(
            _done, Qt.ConnectionType.UniqueConnection)

    # ── 延迟页面构造 ─────────────────────────────────────────────

    def _installCrossfadeView(self):
        """把 FluentWindow 默认的 PopUpAniStackedWidget（旧页瞬切 + 新页
        从下方 76px 位移滑入，观感生硬）替换为 CrossfadeStackedWidget
        （新页淡入 + 旧页垫底的交叉淡化），使切换更顺滑自然。

        必须在任何 addSubInterface 之前调用：此时内部 view 仍为空，
        直接替换即可让后续所有页面（含 lazy 构造的）加入新 view，无需迁移。
        不修改 qfluentwidgets 库源码，仅替换其内部 view 实例，升级安全。
        """
        from app.ui.crossfade_stack import CrossfadeStackedWidget

        sw = self.stackedWidget          # qfluentwidgets StackedWidget (QFrame)
        oldView = sw.view
        newView = CrossfadeStackedWidget(sw)
        # 保险：迁移旧 view 中已有页面（正常此时为空）
        while oldView.count() > 0:
            w = oldView.widget(0)
            oldView.removeWidget(w)
            newView.addWidget(w)
        # 断开旧 view 的 currentChanged 转发，从布局中替换 view
        try:
            oldView.currentChanged.disconnect(sw.currentChanged)
        except TypeError:
            pass
        sw.hBoxLayout.removeWidget(oldView)
        oldView.deleteLater()
        sw.hBoxLayout.addWidget(newView)
        newView.currentChanged.connect(sw.currentChanged)
        sw.view = newView

    def _thread(self, attr: str):
        """延迟页面的 worker 线程参数（构造时已全部就绪）。"""
        return getattr(self, attr)

    def _build_lazy_batch(self, specs: list):
        """构造一批延迟页面：import + 实例化 + 接线 + 注册导航 + 汇报进度。"""
        import importlib
        total = len(self._LAZY_BATCHES) + 1  # 含设置页
        for key, mod_name, cls_name, thread_attr, icon, title in specs:
            if key in self.pages:
                continue
            t0 = time.monotonic()
            try:
                cls = getattr(importlib.import_module(mod_name), cls_name)
                page = cls(self._thread(thread_attr)) if thread_attr else cls()
                self._wire_lazy_page(key, page)
                self.pages[key] = page
                self.addSubInterface(page, icon, title)
                logging.getLogger(__name__).info(
                    "页面 [%s] 构造完成，耗时 %.0fms",
                    title, (time.monotonic() - t0) * 1000)
            except Exception:
                # 单页构造失败不影响其余页面与关机流程；
                # 注意不得在此局部 import logging：会使整个函数内
                # logging 被判为局部变量，成功路径的日志报 UnboundLocalError
                logging.getLogger(__name__).exception("延迟构造页面失败 %s", key)
            finally:
                self.lazyProgress.emit(len(self.pages), total, title)

    def _wire_lazy_page(self, key: str, page):
        """页面专属接线：与页面构造同处一地，防漏接。"""
        if key == "preset":
            page.setObjectName("presetInterface")
            page.sendRequested.connect(self.st.sigWrite.emit)
        elif key == "adb":
            page.setObjectName("adbInterface")
            # 设置里改默认型号 -> ADB 页立即重载并选中新型号（无需重启）；
            # 设置页先于此页构造时，设置页的连接已覆盖；反之在此补接
            setting = self.pages.get("setting")
            if setting is not None:
                setting.modelCard.modelChanged.connect(
                    lambda _stem: page.reload_models(preselect_default=True))
        elif key == "hid":
            page.setObjectName("hidInterface")
        elif key == "dap":
            page.setObjectName("dapInterface")
        elif key == "modbus":
            page.setObjectName("modbusInterface")
        elif key == "ssh":
            page.setObjectName("sshInterface")
        elif key == "tcpip":
            page.setObjectName("tcpipInterface")
        elif key == "tools":
            pass  # objectName 已在页内设置
        elif key == "ch347":
            page.setObjectName("ch347Interface")
        elif key == "setting":
            # objectName 已在页内设置
            # 设置里改默认型号 -> ADB 页立即重载（ADB 未构造时跳过，
            # 其构造时由 adb 分支反向补接）
            page.modelCard.modelChanged.connect(
                lambda _stem: self._reload_adb_models())
            page.maxCharsChanged.connect(
                self.consolePage.receivePanel.setMaxChars)

    def _reload_adb_models(self):
        adb = self.pages.get("adb")
        if adb is not None:
            adb.reload_models(preselect_default=True)

    def page(self, key: str):
        """取延迟构造的页面（未构造完成时为 None）。"""
        return self.pages.get(key)

    def startLazyBuild(self):
        """触发延迟页面构造（幂等）。

        默认由 showEvent 首次显示自动触发；独立动画进程模式下主窗口
        延迟到加载完成才 show，需在构造后显式调用本方法。

        每次只构造 1 页：页面构造会阻塞主线程几十至几百毫秒，
        页间短暂延时让出事件循环。
        """
        if getattr(self, "_lazyStarted", False):
            return
        self._lazyStarted = True
        batches = [[spec] for spec in self._LAZY_BATCHES]
        setting_spec = [("setting", "app.ui.setting_page", "SettingPage",
                         None, FluentIcon.SETTING, "设置")]

        # 逐页构造：页间延时让出事件循环（packaged 内嵌遮罩模式下
        # 供进度条/状态文字重绘）。延时从 30ms 收紧到 8ms：源码模式
        # 下窗口在整个构建期是隐藏的（独立 splash 进程转圈），页间延时
        # 纯属空等；8ms 仍保证每页之间至少一个完整绘制周期，9 个间隙
        # 共省约 200ms。全部完成后构造设置页。
        def _run(batches_left, done):
            if not batches_left:
                done()
                return
            self._build_lazy_batch(batches_left[0])
            QTimer.singleShot(
                8, lambda: _run(batches_left[1:], done))

        def _run_setting():
            key, mod_name, cls_name, _attr, icon, title = setting_spec[0]
            if key not in self.pages:
                t0 = time.monotonic()
                try:
                    import importlib
                    cls = getattr(
                        importlib.import_module(mod_name), cls_name)
                    page = cls()
                    self._wire_lazy_page(key, page)
                    self.pages[key] = page
                    self.addSubInterface(
                        page, icon, title,
                        position=NavigationItemPosition.BOTTOM)
                    logging.getLogger(__name__).info(
                        "页面 [%s] 构造完成，耗时 %.0fms",
                        title, (time.monotonic() - t0) * 1000)
                except Exception:
                    # 同上：不得局部 import logging（UnboundLocalError 坑）
                    logging.getLogger(__name__).exception(
                        "延迟构造页面失败 %s", key)
            self.lazyProgress.emit(len(self.pages),
                                   len(self._LAZY_BATCHES) + 1, title)
            self.lazyFinished.emit()

        QTimer.singleShot(
            0, lambda: _run(batches, _run_setting))

    def showEvent(self, event):
        """首次显示时自动触发延迟构造（若尚未开始）。"""
        super().showEvent(event)
        self.startLazyBuild()

    # 2) 整窗透明度（0~1，含标题栏；1 为不透明）
    # self.setWindowOpacity(1.0)

    # 3) Win11 云母半透明（项目默认已开启；Win10 无效并自动回退纯色）
    # self.setMicaEffectEnabled(True)

    # 4) 自定义明/暗主题窗口背景色（light, dark)
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

    def _initMcpService(self):
        """事件循环里异步初始化 MCP 服务（不阻塞首屏构造）。

        由 __init__ 经 QTimer.singleShot(0, ...) 调度；重复调用幂等。
        """
        if self._mcpService is not None:
            return
        log = logging.getLogger(__name__)
        try:
            from app.mcp_bridge import WorkerBridge
            from app.mcp_server import McpService
            bridge = WorkerBridge(self.st, self.ht, self.dt, self.mt,
                                  self.sht, self.tp, self.cct)
            adb_page = self.pages.get("adb")
            if adb_page is not None:
                bridge.adbOutput.connect(adb_page.on_mcp_output)
                bridge.adbDeviceList.connect(adb_page.on_mcp_devices)
                bridge.adbActivity.connect(adb_page.on_mcp_activity)
            self._mcpService = McpService(
                bridge, qconfig.get(cfg.mcpPort),
                qconfig.get(cfg.mcpToken),
                allow_exec=qconfig.get(cfg.mcpAllowExec),
                allow_file=qconfig.get(cfg.mcpAllowFile),
                allow_ch347=qconfig.get(cfg.mcpCh347))
            if not self._mcpService.start():
                # 无密钥/端口被占用等：服务未启动，弹提示 + 记录原因，
                # GUI 照常运行
                log.warning("MCP 服务未启动：%s", self._mcpService.last_error)
                InfoBar.warning(
                    title="MCP 服务未启动",
                    content=str(self._mcpService.last_error),
                    duration=8000, parent=self)
            else:
                log.info("MCP 服务已启动：%s", self._mcpService.url)
                InfoBar.success(
                    title="MCP 服务已启动",
                    content=f"端口 {self._mcpService.port} 未被占用，"
                            f"监听 {self._mcpService.url}",
                    duration=3000, parent=self)
        except Exception:
            log.exception("MCP 服务初始化失败")
            self._mcpService = None

    def _shutdownPage(self, page):
        if page is not None:
            try:
                page.shutdown()
            except Exception:
                logging.getLogger(__name__).exception(
                    "页面 shutdown 异常：%s", type(page).__name__)

    def _stopWorkersConcurrently(self, workers, per_wait_ms: int = 800):
        """并发停 worker：先对所有 worker requestQuit（Event.set 立即返回），
        再统一 wait。串行调用时 7 个 worker 的 timeout 会累加（最坏
        16s）；并发后总耗时 ≈ 最慢那个（实测 ~100ms）。

        worker.requestQuit() 内部只 set 一个 threading.Event，线程安全，
        可从 UI 线程直接调用（见 SerialThread.stop 内部注释）。
        """
        log = logging.getLogger(__name__)
        for w in workers:
            try:
                w.worker.requestQuit()
            except Exception:
                log.exception("worker requestQuit 异常：%s",
                              type(w).__name__)
        for w in workers:
            name = type(w).__name__
            try:
                if not w.thread.wait(per_wait_ms):
                    log.warning("worker %s 停机超时（>%dms），额外等 200ms",
                                name, per_wait_ms)
                    w.thread.wait(200)
            except Exception:
                log.exception("worker %s wait 异常", name)

    def closeEvent(self, event: QCloseEvent):
        log = logging.getLogger(__name__)
        t0 = time.monotonic()
        log.info("主窗口关闭中…")

        self._portTimer.stop()

        # MCP 服务：uvicorn 后台线程；join 超时从 3s 收紧到 1.5s，
        # should_exit=True 后 uvicorn 通常几百 ms 内退出
        if self._mcpService is not None:
            try:
                self._mcpService.stop(timeout=1.5)
            except Exception:
                log.exception("MCP 服务停止失败")

        # 页面 shutdown：轻量、非阻塞（stopPeriodic / 关子窗 / kill adb probe）
        self._shutdownPage(self.consolePage)
        for key in ("preset", "adb", "hid", "dap", "modbus", "ssh",
                    "tcpip", "tools", "ch347", "setting"):
            self._shutdownPage(self.pages.get(key))

        # worker 线程：并发停机（旧实现串行调 7 次 stop，timeout 累加导致卡顿）
        self._stopWorkersConcurrently(
            (self.st, self.ht, self.dt, self.mt, self.sht, self.tp, self.cct))

        log.info("主窗口关闭完成，耗时 %.0f ms",
                 (time.monotonic() - t0) * 1000)
        super().closeEvent(event)

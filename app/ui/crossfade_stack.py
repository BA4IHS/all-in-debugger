# coding: utf-8
"""页面切换转场：新页单页淡入（fade-in）堆叠控件。

qfluentwidgets 的 FluentWindow 默认用 PopUpAniStackedWidget 做页面切换：
旧页瞬间消失、新页从下方 76px 处纯位移滑入，全程无透明度变化，观感
生硬（像被"甩"进来）。本模块提供 CrossfadeStackedWidget 替代它：
旧页按 stacked 语义立即隐藏，新页以 opacity 0->1 活体淡入，过渡柔和。

历轮实测排除的方案（均因本窗口架构不可行或观感不合格）：
- 交叉淡化（新旧两页半透明叠加）→ 残影/重影；
- 快照 veil / 过背景淡化 → 本窗口 Win11 下 qfluentwidgets 默认开启 Mica
  材质，屏幕底色由 DWM 合成、Qt 窗口自身透明，任何 QWidget.grab() 离屏
  快照都是透明/白图，叠层即白罩闪屏，快照路线根本不可行；
- 双活页 effect 交叉淡化 → 每帧离屏重渲染两页，重页面掉帧。

本方案的取舍：
- 无残影：任何时刻画面只有一页内容（新页）+ 系统背景；
- 无白罩/闪屏：不 grab、不叠纯色层，颜色全部来自活体渲染；
- 开销可控：每帧仅重渲染新页一页（与库默认位移滑入同量级，后者长期
  使用无帧率投诉）；节拍按当前屏幕刷新率（QScreen.refreshRate()）走；
- QGraphicsOpacityEffect 仅动画期间挂在新页上，结束即移除。

接口完全兼容 qfluentwidgets StackedWidget 的委托调用
（setCurrentWidget 的多参签名、isAnimationEnabled 属性、setAnimationEnabled、
addWidget/removeWidget），可无缝替换 FluentWindow 内部的 view。

由 MainWindow 在首个 addSubInterface 之前替换 self.stackedWidget.view 接入，
这样所有页面（含 lazy 构造的）都直接加入本控件，无需迁移。
"""
from math import pi, sin

from PyQt6.QtCore import QElapsedTimer, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QStackedWidget


class CrossfadeStackedWidget(QStackedWidget):
    """新页单页淡入切换的堆叠控件（替代 PopUpAniStackedWidget）。"""

    # 与 PopUpAniStackedWidget 对齐的信号，兼容潜在监听方
    aniStart = pyqtSignal()
    aniFinished = pyqtSignal()

    DURATION = 240   # 淡入时长 ms：柔和且不拖沓

    def __init__(self, parent=None):
        super().__init__(parent)
        # StackedWidget.isAnimationEnabled() 读取该属性（注意是属性不是方法）
        self.isAnimationEnabled = True
        self._ani = None          # 动画期间为驱动 QTimer（None = 空闲）
        self._aniWidget = None    # 当前正在淡入的新页（挂着 effect）
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        # PreciseTimer：高刷新率下间隔仅几 ms，粗定时器会丢拍
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)

    # ── 兼容 qfluentwidgets StackedWidget 委托的接口 ──────────────

    def setAnimationEnabled(self, enabled: bool):
        self.isAnimationEnabled = bool(enabled)

    def addWidget(self, widget, deltaX=0, deltaY=0):
        # 兼容 PopUp.addWidget(widget, deltaX, deltaY) 签名；delta 在此无意义
        super().addWidget(widget)

    def removeWidget(self, widget):
        super().removeWidget(widget)

    def setCurrentWidget(self, widget, needPopOut=False, showNextWidgetDirectly=True,
                         duration=250, easingCurve=None):
        # StackedWidget 以 setCurrentWidget(w, duration=300) 或
        # setCurrentWidget(w, True, False, 300, InQuad) 调用，动画参数统一忽略
        self.setCurrentIndex(self.indexOf(widget))

    def setCurrentIndex(self, index, needPopOut=False, showNextWidgetDirectly=True,
                        duration=250, easingCurve=None):
        if index < 0 or index >= self.count() or index == self.currentIndex():
            return
        if not self.isAnimationEnabled:
            super().setCurrentIndex(index)
            return
        self._startFade(index)

    def minimumSizeHint(self):
        """不向主窗口传播页面最小尺寸（根治“窗口拖不小/移动后变大锁定”）。

        QStackedWidget 默认取所有页 minimumSizeHint 的最大值：某个宽页
        （实测 Modbus 表格 ~1010px）会把主窗口 minimumSizeHint 顶到
        ~1059×600，远大于 MainWindow 显式设的 setMinimumSize(700,480)。
        一旦布局最小尺寸 > 显式最小，移动/拖动触发重布局时窗口会被
        snap 回 1059×600 并锁死（“只能调大不能调小”），与当前在哪个
        页无关。各页内部已用 SingleDirectionScrollArea 处理纵向溢出，
        这里返回 0 让主窗口最小尺寸由 setMinimumSize(700,480) 唯一决定；
        视图在布局中有拉伸，不会因此塔缩。
        """
        return QSize(0, 0)

    # ── 单页淡入实现 ────────────────────────────────────────────

    def _refreshIntervalMs(self) -> int:
        """与当前屏幕刷新率同步的驱动间隔（垂直同步节奏）。

        如 60Hz→17ms、144Hz→7ms、240Hz→4ms；读不到刷新率时回退 60Hz。
        每次切换重新读取，适配换显示器 / 动态刷新率场景。
        """
        rate = 0.0
        screen = self.screen()
        if screen is not None:
            rate = float(screen.refreshRate())
        if rate <= 1.0:
            from PyQt6.QtGui import QGuiApplication
            primary = QGuiApplication.primaryScreen()
            if primary is not None:
                rate = float(primary.refreshRate())
        if rate <= 1.0:
            rate = 60.0
        return max(1, int(round(1000.0 / rate)))

    def _startFade(self, index: int):
        # 先中断进行中的动画并清理，避免连续切换时 effect 残留
        self._cleanup()

        nextWidget = self.widget(index)

        # 立即切换 current：旧页按 stacked 语义隐藏（无叠影），
        # 并触发 currentChanged（路由 / 导航状态同步）
        super().setCurrentIndex(index)

        # 新页若已自带 graphicsEffect，跳过动画（不覆盖其原有 effect）
        if nextWidget.graphicsEffect() is not None:
            self.aniFinished.emit()
            return

        effect = QGraphicsOpacityEffect(nextWidget)
        effect.setOpacity(0.0)
        nextWidget.setGraphicsEffect(effect)
        self._aniWidget = nextWidget

        # 按显示器刷新率节拍驱动淡入（每帧仅重渲染新页一页）
        self._clock.restart()
        self._timer.setInterval(self._refreshIntervalMs())
        self._ani = self._timer
        self.aniStart.emit()
        self._timer.start()

    def _tick(self):
        t = self._clock.elapsed() / self.DURATION
        if t >= 1.0:
            self._onFadeFinished()
            return
        # OutSine：起步响应快、收尾柔和，淡入过程不晃眼
        e = sin(t * pi / 2.0)
        if self._aniWidget is not None:
            effect = self._aniWidget.graphicsEffect()
            if effect is not None:
                effect.setOpacity(e)

    def _onFadeFinished(self):
        self._cleanup()
        self.aniFinished.emit()

    def _cleanup(self):
        """停定时器、移除新页 effect（恢复不透明正常渲染）。幂等。"""
        self._ani = None
        if self._timer.isActive():
            self._timer.stop()
        if self._aniWidget is not None:
            # setGraphicsEffect(None) 删除 effect；新页恢复不透明
            self._aniWidget.setGraphicsEffect(None)
            self._aniWidget = None

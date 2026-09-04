# coding: utf-8
"""启动加载遮罩：qfluentwidgets SplashScreen + 自绘转圈 + 阶段状态文字。

盖住主窗口延迟构造非首屏页面的过程（侧栏导航逐批出现的视觉跳动），
全部页面就绪后由 lazyFinished 触发 finish() 撤除。
"""
import math
import time

from PyQt6.QtCore import QSize, Qt, QRectF, QTimer
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from qfluentwidgets import BodyLabel, SplashScreen, isDarkTheme, themeColor


class _SpinRing(QWidget):
    """单调时钟驱动的自绘转圈。

    QPropertyAnimation（qfluentwidgets 的 IndeterminateProgressRing）在
    主线程被页面构造阻塞时冻结，恢复后才能推进，观感为「卡住-跳一下」。
    本组件旋转角度直接取自 time.monotonic()：任何时机获得重绘
    （文字更新 / resize / timer tick）都会画到当前时刻应有的角度，
    主线程空闲时 16ms timer 驱动 ~60fps 平滑旋转。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(36, 36)
        self._timer = QTimer(self)
        self._timer.setInterval(16)      # ~60 fps
        self._timer.timeout.connect(self.update)
        self._t0 = time.monotonic()
        self._period = 1200              # 旋转一周耗时 ms
        self._breath = 700               # 弧长呼吸周期 ms

    def start(self):
        self._t0 = time.monotonic()
        self._timer.start()
        self.update()

    def stop(self):
        self._timer.stop()
        self.update()

    def paintEvent(self, e):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        cw = 4
        w = min(self.width(), self.height()) - cw
        rc = QRectF(cw / 2, self.height() / 2 - w / 2, w, w)

        # 轨迹底环（低透明度）
        track = (QColor(255, 255, 255, 30) if isDarkTheme()
                 else QColor(0, 0, 0, 30))
        pen = QPen(track, cw, cap=Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rc, 0, 360 * 16)

        # 前景弧：角度由流逝时间决定（主线程阻塞恢复后立即跳到正确角度）
        now = (time.monotonic() - self._t0) * 1000
        base = now % self._period / self._period * 360
        arc = 45 + 135 * (0.5 + 0.5 * math.sin(now * 2 * math.pi / self._breath))
        pen.setColor(themeColor())
        painter.setPen(pen)
        painter.drawArc(rc, int(-base * 16), int(-arc * 16))


class LoadingSplash(SplashScreen):
    """图标居中偏上，其下为转圈进度环与状态文字。"""

    def __init__(self, icon, parent=None):
        super().__init__(icon, parent)
        self.ring = _SpinRing(self)
        self.statusLabel = BodyLabel("正在启动…", self)
        # 文字水平居中，不裁切
        self.statusLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.statusLabel.setSizePolicy(QSizePolicy.Policy.Fixed,
                                       QSizePolicy.Policy.Fixed)
        # 背景为纯白/纯黑（随主题），文字用半透明对比色
        c = "255, 255, 255" if isDarkTheme() else "96, 96, 96"
        self.statusLabel.setStyleSheet(f"color: rgba({c}, 160);"
                                       " background: transparent;")
        # SplashScreen 只监听父窗口 Resize 事件，而主窗口在遮罩创建前
        # 已 resize 完成 → 遮罩会停在默认小尺寸，文字被左右裁切。
        # 创建时显式同步一次父窗口尺寸。
        if parent is not None:
            self.resize(parent.size())
        self._relayout()

    def showEvent(self, e):
        super().showEvent(e)
        self.ring.start()
        self._relayout()

    def setStatus(self, text: str):
        self.statusLabel.setText(text)
        self._relayout()

    def finish(self):
        """关闭遮罩并停掉动画 timer。"""
        self.ring.stop()
        super().finish()

    def _relayout(self):
        """按当前尺寸排版：图标中心 38% 高度，环与文字依次靠下。"""
        w, h = self.width(), self.height()
        iw, ih = self.iconSize().width(), self.iconSize().height()
        self.iconWidget.move(w // 2 - iw // 2, int(h * 0.38) - ih // 2)
        self.ring.move(w // 2 - self.ring.width() // 2,
                       int(h * 0.38) + ih // 2 + 36)
        self.statusLabel.adjustSize()
        self.statusLabel.move(w // 2 - self.statusLabel.width() // 2,
                              self.ring.y() + self.ring.height() + 14)

    def setIconSize(self, size: QSize):
        super().setIconSize(size)
        self._relayout()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

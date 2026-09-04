# coding: utf-8
"""启动加载动画子进程：独立进程渲染转圈，不受主进程阻塞影响。

Qt 是单 GUI 线程模型：主线程被页面构造阻塞时，主进程内任何窗口内容
（无论 QPropertyAnimation / QTimer / 自绘）都必然冻结。要获得 Win11
启动那样持续匀速的转圈，唯一方案是独立进程渲染——本进程有自己的
事件循环，主进程再卡也 60fps 匀速旋转。

用法：python -m app.ui.splash_proc <父进程PID>
- 无边框置顶小窗（透明背景 + 圆角卡片），屏幕居中
- 父进程退出后自动退出（每 2s 轮询父进程存活），防孤儿进程

有意不 import qfluentwidgets：子进程越轻越快出现（PyQt6 即够，
主题色用 Win11 强调色 #0078d4）。
"""
import ctypes
import math
import sys
import time

from PyQt6.QtCore import Qt, QRectF, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

ACCENT = QColor("#0078d4")           # Win11 强调色
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def parent_alive(pid: int) -> bool:
    """父进程是否仍在运行（父 PID 退出后 PID 可能被复用，
    2s 轮询窗口内复用概率极低，可接受）。"""
    if pid <= 0:
        return True
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    kernel32.CloseHandle(h)
    return bool(ok) and code.value == STILL_ACTIVE


class SpinWindow(QWidget):
    """置顶透明小窗：圆角卡片 + 时间驱动的旋转弧 + 启动文字。"""

    def __init__(self, parent_pid: int):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)   # Tool：不占任务栏
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._pid = parent_pid
        self._t0 = time.monotonic()
        self.setFixedSize(240, 200)
        geo = QApplication.primaryScreen().availableGeometry()
        self.move(geo.center().x() - self.width() // 2,
                  geo.center().y() - self.height() // 2)

        self._ani = QTimer(self)
        self._ani.setInterval(16)     # ~60 fps
        self._ani.timeout.connect(self.update)
        self._ani.start()
        self._guard = QTimer(self)
        self._guard.setInterval(2000)
        self._guard.timeout.connect(self._check_parent)
        self._guard.start()

    def _check_parent(self):
        if not parent_alive(self._pid):
            QApplication.quit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing)

        # 半透明圆角卡片（任何桌面背景下均可读）
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(249, 249, 249, 235))
        p.drawRoundedRect(self.rect(), 16, 16)

        # 轨迹底环
        cx, cy = self.width() / 2, self.height() / 2 - 26
        r = 26
        rc = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        pen = QPen(QColor(0, 0, 0, 28), 4, cap=Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(rc, 0, 360 * 16)

        # 旋转弧：角度由单调时钟决定，主进程阻塞与本进程无关
        now = (time.monotonic() - self._t0) * 1000
        base = now % 1200 / 1200 * 360
        arc = 45 + 135 * (0.5 + 0.5 * math.sin(now * 2 * math.pi / 700))
        pen.setColor(ACCENT)
        p.setPen(pen)
        p.drawArc(rc, int(-base * 16), int(-arc * 16))

        # 状态文字
        p.setPen(QColor(60, 60, 60))
        p.setFont(QFont("Microsoft YaHei UI", 10))
        p.drawText(QRectF(0, cy + r + 22, self.width(), 30),
                   Qt.AlignmentFlag.AlignCenter, "正在启动 all-in-debugger…")


def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    app = QApplication(sys.argv)
    win = SpinWindow(pid)
    win.show()
    app.exec()


if __name__ == "__main__":
    main()

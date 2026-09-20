# coding: utf-8
"""启动性能基准：逐阶段测量 main.py 的启动耗时，定位剩余瓶颈。

用法（仓库根目录）：
    python _bench_startup.py           # 单次完整测量
    python _bench_startup.py --repeat 3  # 重复 3 次取中位数

不启动加载动画子进程（避免派生额外进程干扰测量），
主窗口 show 后通过 QTimer 立即退出，只测到 lazyFinished。
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 禁用 splash 子进程派生（测量时不需要独立动画进程干扰）
os.environ.setdefault("_BENCH_NO_SPLASH", "1")

_T0 = time.perf_counter()
_MARKS = []


def mark(label: str):
    now = time.perf_counter()
    prev = _MARKS[-1][1] if _MARKS else _T0
    _MARKS.append((label, now, now - prev))


mark("python-interp-start")

# ── 阶段 1：顶部 import ────────────────────────────────────────
from PyQt6.QtCore import Qt, QTimer
mark("import PyQt6.QtCore")

from PyQt6.QtWidgets import QApplication
mark("import PyQt6.QtWidgets")

from qfluentwidgets import FluentIcon, setTheme
mark("import qfluentwidgets")

from app.config import cfg, loadConfig, qconfig
mark("import app.config")

from app.crash_guard import installCrashGuard
mark("import app.crash_guard")

from app.logging_setup import setupLogging
mark("import app.logging_setup")

from app.ui.main_window import MainWindow
mark("import app.ui.main_window")

from app.ui.scrollbar_style import (apply_white_scrollbars,
                                    install_white_scrollbars)
mark("import app.ui.scrollbar_style")

from app.ui.window_utils import center_window
mark("import app.ui.window_utils")

# ── 阶段 2：初始化 ────────────────────────────────────────────
installCrashGuard()
mark("installCrashGuard()")

setupLogging()
mark("setupLogging()")

QApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
app = QApplication(sys.argv)
mark("QApplication()")

loadConfig()
mark("loadConfig()")

setTheme(qconfig.get(cfg.themeMode))
mark("setTheme()")

install_white_scrollbars(app)
mark("install_white_scrollbars()")

# ── 阶段 3：主窗口构造 ────────────────────────────────────────
window = MainWindow()
mark("MainWindow()")

apply_white_scrollbars(window)
mark("apply_white_scrollbars()")

# ── 阶段 4：show + 延迟构造 ──────────────────────────────────
# 不派生 splash 子进程，直接 show + startLazyBuild
window.resize(1220, 780)
window.show()
mark("window.show()")

center_window(window)
mark("center_window()")

window.startLazyBuild()
mark("startLazyBuild() [scheduled]")

# 断开 MCP 初始化连接：_initMcpService 同步 import mcp/uvicorn 约 200ms，
# 会混入 lazyFinished 测量；本 bench 只测页面构造，MCP 影响单独评估
try:
    window.lazyFinished.disconnect(window._initMcpService)
except (TypeError, RuntimeError):
    pass


def _on_lazy_finished():
    mark("lazyFinished [all 9 pages built]")
    # 立即退出，不进入事件循环空转
    QTimer.singleShot(0, app.quit)


window.lazyFinished.connect(_on_lazy_finished)

# 兜底：最多等 15s，防止 lazyFinished 不发射时挂死
QTimer.singleShot(15000, app.quit)

app.exec()
mark("app.exec() returned")

# ── 输出 ──────────────────────────────────────────────────────
total = _MARKS[-1][1] - _T0
print("\n=== 启动阶段耗时（源码模式）===")
print(f"{'阶段':<45} {'耗时(ms)':>10} {'累计(ms)':>10}")
print("-" * 70)
for label, ts, delta in _MARKS:
    cum = (ts - _T0) * 1000
    print(f"{label:<45} {delta*1000:>10.1f} {cum:>10.1f}")
print("-" * 70)
print(f"{'总计':<45} {total*1000:>10.1f}")

# 额外：检查哪些重型依赖在启动链上被加载
heavy = ["paramiko", "pymodbus", "invoke", "numpy", "PIL",
         "mcp", "uvicorn", "fastmcp", "anyio", "httpx"]
loaded = [m for m in heavy if m in sys.modules]
print(f"\n启动链已加载的重型依赖：{loaded or '（无）'}")
print(f"sys.modules 总数：{len(sys.modules)}")

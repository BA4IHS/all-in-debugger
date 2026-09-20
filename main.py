# coding: utf-8
"""all-in-debugger 入口。

Copyright (C) 2026 BA4IHS
本程序为自由软件，可按 GNU GPLv3 条款再分发和/或修改（详见 LICENSE）。
"""
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from qfluentwidgets import FluentIcon, setTheme

from app.config import cfg, loadConfig, qconfig
from app.crash_guard import installCrashGuard
from app.logging_setup import setupLogging, setLogLevel
from app.ui.main_window import MainWindow
from app.ui.splash import LoadingSplash
from app.ui.scrollbar_style import apply_white_scrollbars, install_white_scrollbars
from app.ui.window_utils import center_window

CREATE_NO_WINDOW = 0x08000000

# 打包检测：Nuitka 不设置 sys.frozen（PyInstaller 才设），它在每个编译模块的
# globals 里注入 __compiled__ 标记；两者任一命中即视为打包运行。
# 误判代价严重：走错分支会用 `-m` 方式 spawn 自身 exe，被 Nuitka 的
# 自我调用防护立即终止（退出码 2），加载动画静默消失。
_FROZEN = getattr(sys, "frozen", False) or "__compiled__" in globals()


def _spawn_splash_proc():
    """启动独立加载动画进程。

    Qt 单 GUI 线程模型下，主线程被页面构造阻塞时进程内任何窗口都会
    冻结；独立进程渲染的转圈不受影响，等同 Win11 启动动画体验。

    - 源码运行：python -m app.ui.splash_proc <父PID>
    - 打包运行：派生自身 exe（--splash-proc <父PID>，main() 入口分流）。
      注意两个 Nuitka 陷阱（均已实测踩中）：
      1) sys.executable 被伪装成 <dist>/python.exe 且该文件不存在，
         直接 Popen 会 FileNotFoundError → 必须用 sys.argv[0] 取真实 exe；
      2) 用 `-m` 方式 spawn 自身会被 Nuitka 自我调用防护立即终止
         （退出码 2）→ 必须走 --splash-proc 入口分流。
    spawn 失败返回 None，由 main() 回退为主窗口内嵌遮罩方案。
    """
    if _FROZEN:
        exe = os.path.abspath(sys.argv[0])
        if not os.path.isfile(exe):
            return None
        args = [exe, "--splash-proc", str(os.getpid())]
        cwd = os.path.dirname(exe)   # exe 目录（终端用户机器上源码路径不存在）
    else:
        args = [sys.executable, "-m", "app.ui.splash_proc", str(os.getpid())]
        cwd = str(ROOT)              # -m 模块解析需要 cwd 在包根目录
    try:
        return subprocess.Popen(
            args,
            cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW)
    except OSError:
        return None


def _stop_splash_proc(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(2)
    except subprocess.TimeoutExpired:
        proc.kill()


def main():
    t0 = time.monotonic()
    # 全局异常兜底：槽内逃逸的异常不再让进程静默退出（详见 crash_guard）
    installCrashGuard()
    # 应用日志落盘到 logs/app.log（源码运行时并挂 stderr）；
    # 未装 handler 前所有 logging.getLogger().xxx() 都是静默的
    setupLogging()

    import logging
    log = logging.getLogger("app.main")
    # 启动环境：系统/架构/解释器/打包形态/入口，排查现场问题的基础信息
    log.info(
        "all-in-debugger 启动：os=%s(%s) arch=%s python=%s frozen=%s "
        "exe=%s cwd=%s argv=%s",
        platform.system(), platform.release(), platform.machine(),
        sys.version.split()[0], _FROZEN, sys.executable,
        os.getcwd(), sys.argv[1:])

    # 本 exe 以 --splash-proc 参数启动时，仅作为加载动画子进程运行
    # （打包版主进程派生自身 exe 实现独立转圈，见 _spawn_splash_proc）
    if "--splash-proc" in sys.argv[1:]:
        from app.ui import splash_proc
        splash_proc.main()
        return

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    tQt = time.monotonic()
    loadConfig()
    # 日志等级跟随配置：setupLogging 先以默认 INFO 挂 handler，
    # 配置加载后再按 cfg.logLevel 调整 root level
    setLogLevel(qconfig.get(cfg.logLevel))
    setTheme(qconfig.get(cfg.themeMode))
    install_white_scrollbars(app)
    tCfg = time.monotonic()

    splashProc = _spawn_splash_proc()

    window = MainWindow()
    apply_white_scrollbars(window)
    tWin = time.monotonic()
    log.info(
        "启动阶段耗时：QApplication=%.0fms 配置/主题=%.0fms 主窗口构造=%.0fms "
        "splash=%s",
        (tQt - t0) * 1000, (tCfg - tQt) * 1000, (tWin - tCfg) * 1000,
        "独立进程" if splashProc is not None else "内嵌遮罩")

    if splashProc is not None:
        # 源码运行：独立动画进程在屏幕中央转圈（不受主进程阻塞影响），
        # 主窗口后台静默加载，就绪后动画消失、完整界面一次性呈现
        def reveal():
            # show 前9页已全部加入布局，首次显示时窗口会按当前 sizeHint
            # 重新调整尺寸（实测被挤成 500×500）；重新应用默认尺寸并居中
            window.resize(1220, 780)
            window.show()
            center_window(window)
            window.logStartupInfo(t0)
            _stop_splash_proc(splashProc)

        window.lazyFinished.connect(reveal)
        window.startLazyBuild()
    else:
        # 打包版：主窗口内嵌遮罩盖住分批构造过程，就绪后自动撤除
        splash = LoadingSplash(FluentIcon.DEVELOPER_TOOLS, window)
        splash.setStatus(
            f"正在加载模块 (0/{len(MainWindow._LAZY_BATCHES) + 1})…")
        window.lazyProgress.connect(
            lambda done, total, name: splash.setStatus(
                f"正在加载{name} ({done}/{total})…"))
        window.lazyFinished.connect(splash.finish)
        window.show()
        splash.show()
        window.logStartupInfo(t0)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

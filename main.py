# coding: utf-8
"""all-in-debugger 入口。

Copyright (C) 2026 BA4IHS
本程序为自由软件，可按 GNU GPLv3 条款再分发和/或修改（详见 LICENSE）。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from qfluentwidgets import FluentIcon, setTheme

from app.config import cfg, loadConfig, qconfig
from app.ui.main_window import MainWindow
from app.ui.splash import LoadingSplash
from app.ui.scrollbar_style import apply_white_scrollbars, install_white_scrollbars
from app.ui.window_utils import center_window

CREATE_NO_WINDOW = 0x08000000


def _spawn_splash_proc():
    """启动独立加载动画进程（源码运行时）。

    Qt 单 GUI 线程模型下，主线程被页面构造阻塞时进程内任何窗口都会
    冻结；独立进程渲染的转圈不受影响，等同 Win11 启动动画体验。
    打包版（Nuitka frozen，无可用 python 解释器）返回 None，
    由 main() 回退为主窗口内嵌遮罩方案。
    """
    if getattr(sys, "frozen", False):
        return None
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "app.ui.splash_proc", str(os.getpid())],
            cwd=str(ROOT),
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
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    loadConfig()
    setTheme(qconfig.get(cfg.themeMode))
    install_white_scrollbars(app)

    splashProc = _spawn_splash_proc()

    window = MainWindow()
    apply_white_scrollbars(window)

    if splashProc is not None:
        # 源码运行：独立动画进程在屏幕中央转圈（不受主进程阻塞影响），
        # 主窗口后台静默加载，就绪后动画消失、完整界面一次性呈现
        def reveal():
            # show 前9页已全部加入布局，首次显示时窗口会按当前 sizeHint
            # 重新调整尺寸（实测被挤成 500×500）；重新应用默认尺寸并居中
            window.resize(1220, 780)
            window.show()
            center_window(window)
            _stop_splash_proc(splashProc)

        window.lazyFinished.connect(reveal)
        window.startLazyBuild()
    else:
        # 打包版：主窗口内嵌遮罩盖住分批构造过程，就绪后自动撤除
        splash = LoadingSplash(FluentIcon.DEVELOPER_TOOLS, window)
        splash.setStatus("正在加载模块 (0/9)…")
        window.lazyProgress.connect(
            lambda done, total, name: splash.setStatus(
                f"正在加载{name} ({done}/{total})…"))
        window.lazyFinished.connect(splash.finish)
        window.show()
        splash.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

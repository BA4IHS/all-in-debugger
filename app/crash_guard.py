# coding: utf-8
"""全局崩溃兜底：把逃逸异常转成可见提示 + 崩溃日志，程序继续运行。

PyQt 的默认行为是「槽函数内未捕获异常 → sys.excepthook → qFatal() →
进程立即终止」。打包版还关掉了控制台（--windows-console-mode=disable），
于是用户看到的只是「点一下按钮程序就消失了」，既没有提示也没有日志。

本模块接管两个钩子：

- ``sys.excepthook``：主线程异常（Qt 事件循环里的槽、定时器回调等）；
- ``threading.excepthook``：worker 线程 ``run()`` 里逃逸的异常。

处理策略：追加写入 ``logs/crash.log``（超过上限自动重置，避免无限增长），
并在 GUI 线程弹出非模态提示框（上限 ``MAX_BOXES`` 个，防止异常风暴刷屏）。
``KeyboardInterrupt`` / ``SystemExit`` 透传给原钩子，保证 Ctrl+C 仍能退出。
"""
import sys
import threading
import traceback
from datetime import datetime

# 提示框上限：超过后只记日志，避免连续异常把屏幕糊满
MAX_BOXES = 3
# crash.log 体积上限（字节），超过则清空重写
LOG_LIMIT = 256 * 1024

_LOG_LOCK = threading.Lock()
_BUSY = threading.local()      # 重入保护：钩子内部再抛异常时不递归
_BOXES = []                    # 强引用，防止非模态提示框被 GC 提前销毁
_notifier = None
_installed = False
_prevSysHook = None
_prevThreadHook = None


def logPath():
    """崩溃日志路径：<程序目录>/logs/crash.log（logs/ 已在 gitignore）。"""
    from app.config import APP_DIR
    return APP_DIR / "logs" / "crash.log"


def _writeLog(kind: str, text: str) -> None:
    try:
        path = logPath()
        with _LOG_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > LOG_LIMIT:
                path.write_text("", encoding="utf-8")
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"===== {stamp} [{kind}] =====\n{text}\n\n")
    except OSError:
        pass


def _dumpStderr(etype, value, tb) -> None:
    """源码运行时同步打到终端；打包版 stderr 可能为 None，忽略即可。"""
    try:
        if sys.stderr is not None:
            traceback.print_exception(etype, value, tb, file=sys.stderr)
    except Exception:  # noqa: BLE001 - 兜底路径绝不能再抛
        pass


def _forget(box) -> None:
    try:
        if box in _BOXES:
            _BOXES.remove(box)
    except Exception:  # noqa: BLE001
        pass


def _showBox(kind: str, text: str) -> None:
    """在 GUI 线程弹非模态提示（阻塞式对话框会把事件循环一起卡死）。"""
    if len(_BOXES) >= MAX_BOXES:
        return
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance()
        if app is None:
            return
        lines = [s for s in text.strip().splitlines() if s.strip()]
        box = QMessageBox(app.activeWindow())
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("程序内部错误")
        box.setText("该操作未能完成，程序已捕获错误并继续运行。")
        box.setInformativeText(f"{kind}：{lines[-1] if lines else '未知错误'}")
        box.setDetailedText(text)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.finished.connect(lambda *_: _forget(box))
        _BOXES.append(box)
        box.show()
    except Exception:  # noqa: BLE001 - 兜底路径绝不能再抛
        pass


def _makeNotifier():
    """GUI 线程里的中转对象：worker 线程 emit 后由 Qt 排队到主循环执行。

    QMessageBox 只能在 GUI 线程创建，直接从 worker 线程调用会触发
    "QObject: Cannot create children for a parent that is in a different
    thread" 之类的未定义行为。
    """
    from PyQt6.QtCore import QObject, pyqtSignal

    class _Notifier(QObject):
        crashed = pyqtSignal(str, str)

    notifier = _Notifier()
    notifier.crashed.connect(_showBox)
    return notifier


def _sysHook(etype, value, tb):
    if etype is None:
        return
    if issubclass(etype, (KeyboardInterrupt, SystemExit)):
        _prevSysHook(etype, value, tb)
        return
    if getattr(_BUSY, "active", False):
        return
    _BUSY.active = True
    try:
        text = "".join(traceback.format_exception(etype, value, tb))
        _dumpStderr(etype, value, tb)
        _writeLog("main", text)
        if _notifier is not None:
            _notifier.crashed.emit("主线程", text)
        else:
            _showBox("主线程", text)
    except Exception:  # noqa: BLE001 - 兜底自身出错也不得再抛回解释器
        pass
    finally:
        _BUSY.active = False


def _threadHook(args):
    etype = getattr(args, "exc_type", None)
    if etype is None:
        return
    if issubclass(etype, (KeyboardInterrupt, SystemExit)):
        _prevThreadHook(args)
        return
    if getattr(_BUSY, "active", False):
        return
    _BUSY.active = True
    try:
        value = getattr(args, "exc_value", None)
        tb = getattr(args, "exc_traceback", None)
        text = "".join(traceback.format_exception(etype, value, tb))
        _dumpStderr(etype, value, tb)
        thread = getattr(args, "thread", None)
        name = getattr(thread, "name", None) or "thread"
        _writeLog(f"thread:{name}", text)
        if _notifier is not None:
            _notifier.crashed.emit(f"线程 {name}", text)
    except Exception:  # noqa: BLE001 - 兜底自身出错也不得再抛回解释器
        pass
    finally:
        _BUSY.active = False


def installCrashGuard() -> bool:
    """安装全局异常兜底；须在 QApplication 构造之后、主循环之前调用。

    重复调用无副作用。返回是否成功安装（Qt 不可用时仍会装日志钩子）。
    """
    global _installed, _notifier, _prevSysHook, _prevThreadHook
    if _installed:
        return True
    _prevSysHook = sys.excepthook
    _prevThreadHook = threading.excepthook
    try:
        _notifier = _makeNotifier()
    except Exception:  # noqa: BLE001 - 无 Qt（如纯测试环境）时退化为只记日志
        _notifier = None
    sys.excepthook = _sysHook
    threading.excepthook = _threadHook
    _installed = True
    return True


def uninstallCrashGuard() -> None:
    """还原原始钩子（测试收尾用）。"""
    global _installed, _notifier
    if not _installed:
        return
    sys.excepthook = _prevSysHook
    threading.excepthook = _prevThreadHook
    _notifier = None
    _installed = False

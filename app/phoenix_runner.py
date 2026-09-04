# coding: utf-8
"""PhoenixConsole（全志）命令行烧录执行器。

PhoenixConsole 是纯命令行量产烧录工具：
  PhoenixConsole.exe [-options] -o count -p image_path
（-e 擦除 / -r 重启 / -s serial / -t 单台超时 / -u usb 端口，详见 build_command）

- find_phoenix：本地路径解析（同 adb_runner.find_adb 思路）
- parse_version / build_command / run_burn_sync：同步纯逻辑，供 MCP 桥与测试
- PhoenixProcess：基于 QProcess 的异步烧录进程，供 UI 使用

烧录经子进程完成、无原生句柄，架构同 ADB 模块：UI 走 QProcess，
MCP 桥走同步 subprocess，不新增 worker 线程。
"""
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional, Tuple

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, pyqtSignal

# erase 模式合法取值：-1 = 不传 -e（不擦除）
ERASE_MODES = (-1, 0, 1, 10, 11, 12)
EXE_NAME = "PhoenixConsole.exe"

# 全局烧录互斥锁：GUI（QProcess）与 MCP（subprocess）同一时刻只允许一个烧录
_burn_lock = threading.Lock()

_VERSION_RE = re.compile(
    r"(?:PhoenixConsole version|current version)\s*:\s*(V?[\d.]+)",
    re.IGNORECASE)


def _bundled_phoenix() -> Optional[str]:
    """程序自带的 PhoenixConsole 工具包（app/libs/phoenix/，
    整套拷贝自 PhoenixConsole_V2.0.8：exe + 运行 DLL + AW_Driver）。"""
    from app.native import LIBS_DIR
    cand = LIBS_DIR / "phoenix" / EXE_NAME
    return str(cand) if cand.is_file() else None


def find_phoenix(configured: str) -> Tuple[Optional[str], str]:
    """解析 PhoenixConsole.exe 路径。返回 (path 或 None, 说明/错误)。

    优先级：配置值 > 程序自带 app/libs/phoenix > PATH。
    配置值可为 exe 绝对路径或安装目录（自动补 PhoenixConsole.exe）；
    PATH 兜底仅覆盖个别把工具目录加进系统路径的环境。
    """
    cand = (configured or "").strip()
    if cand:
        p = Path(cand)
        if p.is_dir():
            p = p / EXE_NAME
        if p.is_file():
            return str(p), ""
    resolved = _bundled_phoenix()
    if not resolved:
        resolved = shutil.which(cand or EXE_NAME)
    if not resolved:
        return None, (f"未找到 {EXE_NAME}（{cand or EXE_NAME}）："
                      "请在小工具「Phoenix 烧录」窗口浏览选择")
    return resolved, ""


def parse_version(out: str) -> str:
    """提取版本号；兼容 -h 头部 `PhoenixConsole version:V2.0.8` 与
    -v 输出 `current version: V2.0.8`；找不到返回空串。"""
    match = _VERSION_RE.search(out or "")
    return match.group(1) if match else ""


def build_command(exe: str, image: str, count: int, serial: str = "",
                  erase: int = -1, reboot: bool = False,
                  timeout_s: int = 0, usb_port: int = -1) -> list:
    """拼装烧录命令行（返回 [exe, ...args] 可直接 subprocess 执行）。

    - erase：-1 不传 -e；0/1 产品模式；10/11/12 升级模式
    - timeout_s > 0 才传 -t；usb_port >= 0 才传 -u
    """
    if erase not in ERASE_MODES:
        raise ValueError(f"erase 取值应为 {'/'.join(map(str, ERASE_MODES))} 之一")
    count = int(count)
    if count < 1:
        raise ValueError("count 必须 >= 1")
    args = []
    if erase >= 0:
        args += ["-e", str(int(erase))]
    if reboot:
        args.append("-r")
    args += ["-o", str(count), "-p", str(image)]
    if serial:
        args += ["-s", str(serial)]
    if int(timeout_s) > 0:
        args += ["-t", str(int(timeout_s))]
    if usb_port is not None and int(usb_port) >= 0:
        args += ["-u", str(int(usb_port))]
    return [str(exe), *args]


def _run_env(exe_dir: str) -> dict:
    """子进程环境：工作目录 = exe 目录且 PATH 前置该目录。

    PhoenixConsole 会 CreateProcess 同目录自带的 adb.exe（引导设备进
    烧录模式），把 exe 目录加进 PATH 保证能找到。
    """
    env = dict(os.environ)
    env["PATH"] = exe_dir + os.pathsep + env.get("PATH", "")
    return env


def run_burn_sync(exe: str, args, timeout_s: float) -> subprocess.CompletedProcess:
    """同步执行烧录/探测命令（MCP 桥与测试用）。

    超时抛 subprocess.TimeoutExpired，启动失败抛 OSError，由调用方转错误。
    """
    exe = str(exe)
    exe_dir = str(Path(exe).resolve().parent)
    kwargs = dict(capture_output=True, text=True, timeout=float(timeout_s),
                  encoding="utf-8", errors="replace", cwd=exe_dir,
                  env=_run_env(exe_dir))
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.run([exe, *args], **kwargs)


def try_acquire_burn() -> bool:
    """抢占全局烧录槽（非阻塞）。GUI 与 MCP 任一占用期间拒绝新烧录。"""
    return _burn_lock.acquire(blocking=False)


def release_burn() -> None:
    """释放烧录槽（与 try_acquire_burn 配对）。"""
    _burn_lock.release()


# ---------------------------------------------------------------------------
# UI 异步烧录进程
# ---------------------------------------------------------------------------

class PhoenixProcess(QObject):
    """QProcess 异步烧录进程（UI 线程安全，不阻塞界面）。

    start 一次跑一条命令（烧录或 -v 探测）；同一实例再次 start 前
    需等上一次 stopped。stop 异步终止，shutdown 立即杀并屏蔽迟到信号。
    """

    dataReceived = pyqtSignal(bytes)
    started = pyqtSignal()
    stopped = pyqtSignal(int, str)   # 退出码, 错误说明（空串=正常结束）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: Optional[QProcess] = None
        self._retired = set()

    def is_running(self) -> bool:
        return self._proc is not None and \
            self._proc.state() != QProcess.ProcessState.NotRunning

    def start(self, exe: str, args: list):
        if self.is_running():
            return
        exe = str(exe)
        p = QProcess(self)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        p.readyReadStandardOutput.connect(lambda p=p: self._on_read(p))
        p.started.connect(self.started.emit)
        p.finished.connect(
            lambda code, status, p=p: self._on_finished(p, code, status))
        p.errorOccurred.connect(lambda error, p=p: self._on_error(p, error))
        self._proc = p
        exe_dir = str(Path(exe).resolve().parent)
        p.setWorkingDirectory(exe_dir)
        pe = QProcessEnvironment.systemEnvironment()
        pe.insert("PATH", exe_dir + os.pathsep + pe.value("PATH", ""))
        p.setProcessEnvironment(pe)
        p.start(exe, [str(a) for a in args])

    def stop(self):
        """异步终止（先 terminate，超时未退再 kill）。"""
        p = self._proc
        if p is None:
            return
        p.terminate()
        QProcess.singleShot(1500, lambda p=p: self._kill_if_current(p))

    def shutdown(self):
        """窗口/应用关闭专用：立即杀进程并屏蔽迟到信号。"""
        p = self._proc
        if p is None:
            self._reap_retired()
            return
        self._proc = None
        p.blockSignals(True)
        if p.state() != QProcess.ProcessState.NotRunning:
            p.kill()
            p.waitForFinished(1000)
        p.deleteLater()
        self._reap_retired()

    def _kill_if_current(self, p: QProcess):
        if p is self._proc and \
                p.state() != QProcess.ProcessState.NotRunning:
            p.kill()

    def _on_read(self, p: QProcess):
        if p is not self._proc:
            return
        d = p.readAllStandardOutput()
        if d:
            self.dataReceived.emit(bytes(d))

    def _on_finished(self, p: QProcess, code, _status):
        if p is not self._proc:
            return
        self._on_read(p)
        self._proc = None
        p.deleteLater()
        self.stopped.emit(int(code), "")

    def _on_error(self, p: QProcess, err):
        if p is not self._proc:
            return
        self._proc = None
        self._dispose_async(p)
        self.stopped.emit(-1, f"进程错误({getattr(err, 'value', err)})")

    def _dispose_async(self, p: QProcess):
        """终止进程并在 finished 后销毁，不阻塞 UI。"""
        if p.state() == QProcess.ProcessState.NotRunning:
            p.deleteLater()
            return
        self._retired.add(p)

        def cleanup(_code, _status, p=p):
            self._retired.discard(p)
            p.deleteLater()

        p.finished.connect(cleanup)
        p.kill()

    def _reap_retired(self):
        for p in list(self._retired):
            p.blockSignals(True)
            if p.state() != QProcess.ProcessState.NotRunning:
                p.kill()
                p.waitForFinished(1000)
            p.deleteLater()
        self._retired.clear()

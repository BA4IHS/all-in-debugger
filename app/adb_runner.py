# coding: utf-8
"""ADB 执行与配置解析核心。

- find_adb：仅做本地路径解析
- adb_version / list_adb_devices：同步辅助，仅供测试或后台线程使用
- AdbProbe：基于 QProcess 的异步探测，供 UI 刷新版本和设备列表
- list_profiles / load_profile：从目录读取型号 profile（每型号一个 JSON）
- AdbShellProcess：交互式 `adb -s <serial> shell -t`（QProcess，异步，UI 线程）
- AdbCommandRunner：顺序执行 profile 命令（`adb -s <serial> shell <cmd>`），
  输出流式投喂，命令间用 ANSI 分隔标题（终端会渲染颜色）

所有 UI 触发的 adb 调用都应走 QProcess，不能在主线程调用 subprocess.run。
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal

# ---------------------------------------------------------------------------
# adb 定位 / 版本 / 设备列表（同步）
# ---------------------------------------------------------------------------

def _bundled_adb() -> Optional[str]:
    """程序自带的 adb 三件套（app/libs/adb/，含官方 AdbWinApi/AdbWinUsbApi）。"""
    from app.native import LIBS_DIR
    cand = LIBS_DIR / "adb" / "adb.exe"
    return str(cand) if cand.is_file() else None


def find_adb(configured: str) -> Tuple[Optional[str], str]:
    """解析 adb 可执行文件路径。返回 (path 或 None, 说明/错误)。

    优先级：配置项 > 程序自带三件套 > PATH。
    （自带版为官方最新 platform-tools；PATH 里常有刷机工具捆绑的
    1.0.39 旧客户端，交互终端会有数秒回显延迟，故排在自带版之后。）
    """
    cand = (configured or "").strip()
    if cand and os.path.isabs(cand) and os.path.isfile(cand):
        return cand, ""
    resolved = _bundled_adb()
    if not resolved:
        resolved = shutil.which(cand or "adb")
    if not resolved:
        return None, f"未找到 adb：{cand or 'adb'}（请在设置里配置 adb 路径）"
    return resolved, ""


def adb_version(adb_path: str) -> Tuple[Optional[str], str]:
    try:
        r = subprocess.run([adb_path, "version"], capture_output=True,
                           text=True, timeout=6, creationflags=_hide_console())
    except Exception as e:
        return None, str(e)
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    version = adb_version_line(out)
    lines = out.splitlines()
    return (version or (lines[0] if lines else None)), ""


_ADB_VERSION_RE = re.compile(
    r"Android Debug Bridge version\s+(\d+)\.(\d+)\.(\d+)",
    re.IGNORECASE,
)


def parse_adb_version(out: str):
    """从混合 stdout/stderr 中提取 (major, minor, patch)；找不到返回 None。"""
    match = _ADB_VERSION_RE.search(out or "")
    return tuple(int(v) for v in match.groups()) if match else None


def adb_version_line(out: str) -> str:
    """返回真实版本行，忽略 server mismatch/daemon 启动等前置输出。"""
    for line in (out or "").splitlines():
        if _ADB_VERSION_RE.search(line):
            return line.strip()
    return ""


def is_legacy_adb_version(version) -> bool:
    """1.0.39 等旧客户端在部分设备的交互 PTY 上会出现数秒级延迟。"""
    return version is not None and tuple(version) < (1, 0, 40)


def list_adb_devices(adb_path: str) -> Tuple[List[dict], str]:
    """返回 ([{serial,state,info}, ...], 错误)。state 如 device/offline/unauthorized。"""
    try:
        r = subprocess.run([adb_path, "devices", "-l"], capture_output=True,
                           text=True, timeout=6, creationflags=_hide_console())
    except Exception as e:
        return [], str(e)
    out = (r.stdout or "") + (r.stderr or "")
    return _parse_devices_text(out), ""


def _parse_devices_text(out: str) -> List[dict]:
    devs = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        info = " ".join(parts[2:])
        devs.append({"serial": serial, "state": state, "info": info})
    return devs


def _hide_console() -> int:
    """Windows 下隐藏子进程控制台窗口；其它平台返回 0。"""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _dispose_process_async(p: QProcess, retired=None) -> None:
    """终止进程并在 finished 后销毁，不阻塞 UI。"""
    if p.state() == QProcess.ProcessState.NotRunning:
        p.deleteLater()
        return
    if retired is not None:
        retired.add(p)

    def cleanup(_code, _status, p=p):
        if retired is not None:
            retired.discard(p)
        p.deleteLater()

    p.finished.connect(cleanup)
    p.kill()


def _reap_process_on_shutdown(p: QProcess, timeout_ms=1000) -> None:
    """仅用于应用退出：确保父 QObject 销毁前子进程已经退出。"""
    p.blockSignals(True)
    if p.state() != QProcess.ProcessState.NotRunning:
        p.kill()
        p.waitForFinished(max(1, int(timeout_ms)))
    p.deleteLater()


def _reap_retired_processes(retired) -> None:
    for p in list(retired):
        _reap_process_on_shutdown(p)
    retired.clear()


# ---------------------------------------------------------------------------
# UI 异步探测
# ---------------------------------------------------------------------------

class AdbProbe(QObject):
    """带超时、可取消的异步子进程探测器。

    finished(data, exit_code, error)：
    - 正常结束时 error 为空，data 为合并后的 stdout/stderr
    - 启动失败、超时或进程崩溃时 error 非空
    """

    finished = pyqtSignal(bytes, int, str)
    busyChanged = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: Optional[QProcess] = None
        self._retired = set()
        self._buffer = bytearray()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)

    def is_running(self) -> bool:
        return self._proc is not None

    def start(self, program: str, arguments, timeout_ms: int = 6000):
        """立即返回；同一探测器上一次未完成的任务会被安全取消。"""
        self._cancel(emit_busy=False)
        self._buffer.clear()

        p = QProcess(self)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        p.readyReadStandardOutput.connect(lambda p=p: self._on_read(p))
        p.finished.connect(
            lambda code, status, p=p: self._on_finished(p, code, status))
        p.errorOccurred.connect(lambda error, p=p: self._on_error(p, error))
        self._proc = p
        self.busyChanged.emit(True)
        self._timer.start(max(1, int(timeout_ms)))
        p.start(program, [str(a) for a in arguments])

    def cancel(self):
        self._cancel(emit_busy=True)

    def shutdown(self):
        p = self._proc
        if p is None:
            _reap_retired_processes(self._retired)
            return
        self._proc = None
        self._timer.stop()
        _reap_process_on_shutdown(p)
        _reap_retired_processes(self._retired)

    def _cancel(self, emit_busy: bool):
        p = self._proc
        if p is None:
            return
        self._proc = None
        self._timer.stop()
        _dispose_process_async(p, self._retired)
        if emit_busy:
            self.busyChanged.emit(False)

    def _on_read(self, p: QProcess):
        if p is not self._proc:
            return
        data = p.readAllStandardOutput()
        if data:
            self._buffer.extend(bytes(data))

    def _on_finished(self, p: QProcess, code, _status):
        self._finish(p, int(code), "")

    def _on_error(self, p: QProcess, _error):
        if p is not self._proc:
            return
        self._finish(
            p, -1, p.errorString() or "ADB 进程启动失败",
            kill=p.state() != QProcess.ProcessState.NotRunning)

    def _on_timeout(self):
        p = self._proc
        if p is None:
            return
        self._finish(p, -1, "ADB 响应超时（已终止本次操作）", kill=True)

    def _finish(self, p: QProcess, code: int, error: str, kill=False):
        if p is not self._proc:
            return
        self._on_read(p)
        data = bytes(self._buffer)
        self._buffer.clear()
        self._proc = None                 # 先摘除，忽略 kill 后的迟到信号
        self._timer.stop()
        if kill:
            _dispose_process_async(p, self._retired)
        else:
            p.deleteLater()
        self.busyChanged.emit(False)
        self.finished.emit(data, int(code), error)


# ---------------------------------------------------------------------------
# profile（型号 -> 命令集）
# ---------------------------------------------------------------------------

def profile_dir() -> Path:
    return Path(__file__).resolve().parent / "adb_profiles"


def load_profile(path) -> dict:
    """读取并校验一个 profile 文件；失败抛 ValueError。"""
    p = Path(path)
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("profile 顶层应为对象")
    cmds = data.get("commands")
    if not isinstance(cmds, list) or not cmds:
        raise ValueError("profile 缺少非空 commands 数组")
    norm = []
    for i, c in enumerate(cmds):
        if not isinstance(c, dict):
            raise ValueError(f"commands[{i}] 应为对象")
        name = str(c.get("name") or f"命令{i + 1}")
        cmd = str(c.get("cmd") or "").strip()
        if not cmd:
            raise ValueError(f"commands[{i}] 缺少 cmd")
        norm.append({"name": name, "cmd": cmd})
    data["commands"] = norm
    return data


def list_profiles(directory=None) -> List[Tuple[str, str, dict]]:
    """扫描目录，返回 [(文件名stem, 显示型号名, profile数据), ...]，按型号名排序。

    解析失败的文件被跳过（不中断整体）。
    """
    d = Path(directory) if directory else profile_dir()
    result = []
    if not d.is_dir():
        return result
    for fp in sorted(d.glob("*.json")):
        try:
            data = load_profile(fp)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        model = str(data.get("model") or fp.stem)
        result.append((fp.stem, model, data))
    result.sort(key=lambda t: t[1])
    return result


# ---------------------------------------------------------------------------
# 交互式 shell
# ---------------------------------------------------------------------------

class AdbShellProcess(QObject):
    dataReceived = pyqtSignal(bytes)
    started = pyqtSignal()
    stopped = pyqtSignal(int, str)   # 退出码, 错误说明

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: Optional[QProcess] = None
        self._retired = set()

    def is_running(self) -> bool:
        return self._proc is not None and \
            self._proc.state() != QProcess.ProcessState.NotRunning

    def start(self, adb: str, serial: str):
        if self.is_running():
            return
        p = QProcess(self)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        p.readyReadStandardOutput.connect(lambda p=p: self._on_read(p))
        p.started.connect(self.started.emit)
        p.finished.connect(
            lambda code, status, p=p: self._on_finished(p, code, status))
        p.errorOccurred.connect(lambda error, p=p: self._on_error(p, error))
        self._proc = p
        # -t 强制 PTY：远程 /bin/sh 才交互、有提示符、能出 ANSI 颜色
        p.start(adb, ["-s", serial, "shell", "-t"])

    def write(self, data: bytes):
        if self.is_running():
            self._proc.write(bytes(data))

    def stop(self):
        p = self._proc
        if p is None:
            return
        # terminate/kill 都是异步的，不能在 UI 线程 waitForFinished。
        p.terminate()
        QTimer.singleShot(800, lambda p=p: self._kill_if_current(p))

    def shutdown(self):
        """窗口关闭专用：立即杀进程并屏蔽迟到信号。"""
        p = self._proc
        if p is None:
            _reap_retired_processes(self._retired)
            return
        self._proc = None
        _reap_process_on_shutdown(p)
        _reap_retired_processes(self._retired)

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
        _dispose_process_async(p, self._retired)
        self.stopped.emit(-1, f"adb 进程错误({getattr(err, 'value', err)})")


# ---------------------------------------------------------------------------
# 顺序命令执行器（信息采集）
# ---------------------------------------------------------------------------

_SEP = "\x1b[36m── [{name}] $ {cmd} ──\x1b[0m\r\n"
_FOOT = "\x1b[90m── exit {code} ──\x1b[0m\r\n"


class AdbCommandRunner(QObject):
    dataReceived = pyqtSignal(bytes)
    commandStarted = pyqtSignal(str)
    commandFinished = pyqtSignal(str, int)
    allFinished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: List[Tuple[str, str]] = []   # (name, cmd)
        self._proc: Optional[QProcess] = None
        self._retired = set()
        self._cur_name = ""
        self._adb = ""
        self._serial = ""
        self._codec = "UTF-8"

    def set_codec(self, codec: str):
        self._codec = codec

    def is_running(self) -> bool:
        return bool(self._queue) or self._proc is not None

    def run_one(self, adb: str, serial: str, name: str, cmd: str):
        self._adb, self._serial = adb, serial
        self._queue.append((name, cmd))
        if self._proc is None:
            self._start_next()

    def run_all(self, adb: str, serial: str, commands: List[dict]):
        self._adb, self._serial = adb, serial
        for c in commands:
            self._queue.append((c["name"], c["cmd"]))
        if self._proc is None:
            self._start_next()

    def cancel(self):
        self._queue.clear()
        p = self._proc
        if p is None:
            return
        self._proc = None
        _dispose_process_async(p, self._retired)

    def shutdown(self):
        self._queue.clear()
        p = self._proc
        if p is None:
            _reap_retired_processes(self._retired)
            return
        self._proc = None
        _reap_process_on_shutdown(p)
        _reap_retired_processes(self._retired)

    def _enc(self, s: str) -> bytes:
        return s.encode(self._codec, "replace")

    def _start_next(self):
        if not self._queue:
            self.allFinished.emit()
            return
        name, cmd = self._queue.pop(0)
        self._cur_name = name
        self.dataReceived.emit(self._enc(_SEP.format(name=name, cmd=cmd)))
        self.commandStarted.emit(name)

        p = QProcess(self)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        p.readyReadStandardOutput.connect(lambda p=p: self._on_read(p))
        p.finished.connect(
            lambda code, status, p=p: self._on_finished(code, status, p))
        p.errorOccurred.connect(
            lambda error, p=p: self._on_error(error, p))
        self._proc = p
        p.start(self._adb, ["-s", self._serial, "shell", cmd])

    def _on_read(self, p=None):
        p = p or self._proc
        if p is None or p is not self._proc:
            return
        d = p.readAllStandardOutput()
        if d:
            self.dataReceived.emit(bytes(d))

    def _on_finished(self, code, _status, p=None):
        p = p or self._proc
        if p is None or p is not self._proc:
            return
        self._on_read(p)
        self._proc = None
        p.deleteLater()
        self.dataReceived.emit(self._enc(_FOOT.format(code=int(code))))
        self.commandFinished.emit(self._cur_name, int(code))
        self._start_next()

    def _on_error(self, err, p=None):
        # 保留 p=None，便于纯逻辑测试直接验证异常枚举格式。
        if p is not None and p is not self._proc:
            return
        if p is not None:
            self._proc = None
            _dispose_process_async(p, self._retired)
        else:
            self._proc = None
        self.dataReceived.emit(
            self._enc(f"\x1b[31m[adb 错误 {getattr(err, 'value', err)}]\x1b[0m\r\n"))
        self.commandFinished.emit(self._cur_name, -1)
        self._start_next()

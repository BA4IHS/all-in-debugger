# coding: utf-8
"""ADB 执行与配置解析核心。

- find_adb / adb_version / list_adb_devices：同步 subprocess，用于手动刷新（快）
- list_profiles / load_profile：从目录读取型号 profile（每型号一个 JSON）
- AdbShellProcess：交互式 `adb -s <serial> shell -t`（QProcess，异步，UI 线程）
- AdbCommandRunner：顺序执行 profile 命令（`adb -s <serial> shell <cmd>`），
  输出流式投喂，命令间用 ANSI 分隔标题（终端会渲染颜色）

QProcess 非阻塞，无需工作线程；pyserial 才需要线程。
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import QObject, QProcess, pyqtSignal

# ---------------------------------------------------------------------------
# adb 定位 / 版本 / 设备列表（同步）
# ---------------------------------------------------------------------------

def find_adb(configured: str) -> Tuple[Optional[str], str]:
    """解析 adb 可执行文件路径。返回 (path 或 None, 说明/错误)。"""
    cand = (configured or "").strip() or "adb"
    resolved = shutil.which(cand)
    if not resolved and os.path.isabs(cand) and os.path.isfile(cand):
        resolved = cand
    if not resolved:
        return None, f"未找到 adb：{cand}（请在设置里配置 adb 路径）"
    return resolved, ""


def adb_version(adb_path: str) -> Tuple[Optional[str], str]:
    try:
        r = subprocess.run([adb_path, "version"], capture_output=True,
                           text=True, timeout=6, creationflags=_hide_console())
    except Exception as e:
        return None, str(e)
    line = (r.stdout or r.stderr or "").strip().splitlines()
    return (line[0] if line else None), ""


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

    def is_running(self) -> bool:
        return self._proc is not None and \
            self._proc.state() != QProcess.ProcessState.NotRunning

    def start(self, adb: str, serial: str):
        if self.is_running():
            return
        p = QProcess(self)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        p.readyReadStandardOutput.connect(self._on_read)
        p.started.connect(self.started.emit)
        p.finished.connect(self._on_finished)
        p.errorOccurred.connect(self._on_error)
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
        try:
            p.terminate()
            if not p.waitForFinished(800):
                p.kill()
                p.waitForFinished(300)
        except Exception:
            pass

    def _on_read(self):
        d = self._proc.readAllStandardOutput()
        if d:
            self.dataReceived.emit(bytes(d))

    def _on_finished(self, code, _status):
        self._proc = None
        self.stopped.emit(int(code), "")

    def _on_error(self, err):
        self._proc = None
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
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass

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
        p.readyReadStandardOutput.connect(self._on_read)
        p.finished.connect(self._on_finished)
        p.errorOccurred.connect(self._on_error)
        self._proc = p
        p.start(self._adb, ["-s", self._serial, "shell", cmd])

    def _on_read(self):
        d = self._proc.readAllStandardOutput()
        if d:
            self.dataReceived.emit(bytes(d))

    def _on_finished(self, code, _status):
        self._proc = None
        self.dataReceived.emit(self._enc(_FOOT.format(code=int(code))))
        self.commandFinished.emit(self._cur_name, int(code))
        self._start_next()

    def _on_error(self, err):
        self._proc = None
        self.dataReceived.emit(
            self._enc(f"\x1b[31m[adb 错误 {getattr(err, 'value', err)}]\x1b[0m\r\n"))
        self.commandFinished.emit(self._cur_name, -1)
        self._start_next()

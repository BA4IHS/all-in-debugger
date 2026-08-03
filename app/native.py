# coding: utf-8
"""原生 DLL 加载器（HID / ADB USB / CMSIS-DAP 共用的本地库接入层）。

约定：
- DLL 统一放在 程序目录/app/libs/ 下（支持 x86/x64 子目录自动选择）
- 也允许通过环境变量覆盖：HIDAPI_DLL / ADBWINAPI_DLL / CMSIS_DAP_DLL
- 所有加载失败均优雅降级：返回 None，由调用方给出"未找到 DLL"提示
"""
import ctypes
import os
import platform
import sys
from pathlib import Path
from typing import Optional

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).resolve().parent
    LIBS_DIR = _BASE / "app" / "libs"
else:
    _BASE = Path(__file__).resolve().parent.parent
    LIBS_DIR = _BASE / "app" / "libs"


class NativeError(RuntimeError):
    """原生库调用错误。"""


def _candidate_dirs():
    dirs = [LIBS_DIR]
    arch = "x64" if platform.architecture()[0] == "64bit" else "x86"
    dirs.append(LIBS_DIR / arch)
    dirs.append(LIBS_DIR / "adb")   # adb 三件套子目录
    dirs.append(_BASE)  # 兼容放在程序根目录
    return dirs


def _find_file(name: str) -> Optional[Path]:
    for d in _candidate_dirs():
        p = d / name
        if p.is_file():
            return p
    return None


_loaded = {}
_load_errors = {}


def load_dll(name: str, env_var: str = "") -> Optional[ctypes.WinDLL]:
    """按名称加载 DLL，带缓存；失败返回 None 并记录原因。"""
    key = name.lower()
    if key in _loaded:
        return _loaded[key]
    path = None
    if env_var:
        env = os.environ.get(env_var, "").strip()
        if env and Path(env).is_file():
            path = Path(env)
    if path is None:
        path = _find_file(name)
    if path is None:
        _load_errors[key] = f"未找到 {name}（请放入 {LIBS_DIR}）"
        return None
    try:
        dll = ctypes.WinDLL(str(path))
    except OSError as e:
        _load_errors[key] = f"加载 {name} 失败：{e}"
        return None
    _loaded[key] = dll
    return dll


def load_error(name: str) -> str:
    return _load_errors.get(name.lower(), "")


def libs_dir() -> Path:
    return LIBS_DIR

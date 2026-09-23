# coding: utf-8
"""原生 DLL 加载器（HID / ADB USB / CMSIS-DAP / CH347 共用的本地库接入层）。

约定：
- DLL 统一放在 程序目录/app/libs/ 下（支持 x86/x64 子目录自动选择）
- 也允许通过环境变量覆盖：HIDAPI_DLL / ADBWINAPI_DLL / CMSIS_DAP_DLL / CH347DLL
- 所有加载失败均优雅降级：返回 None，由调用方给出"未找到 DLL"提示
"""
import ctypes
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

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
    dirs.append(LIBS_DIR / "ch347")  # CH347DLL.dll 子目录
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
# 已记录过告警的 DLL：load_dll 会被 UI 快照/MCP/页面构造反复调用，
# 失败时每次都打日志会刷屏（实测 CH347DLL 缺失时每 1.5s 刷一条）。
_logged_failures = set()
# 路径解析结果（name.lower() -> Path 或 None）。只在首次解析时扫描
# 候选目录，之后一律走本缓存，运行期不再访问文件系统。
_resolved = {}
# 启动期 preload() 完成后的封口标记：之后不再查找任何未探测的 DLL，
# 保证“只在启动的一刻寻找原生库”（详见 preload）。
_SEALED = False

# 启动期需要探测的原生库分组：(候选名元组, 环境变量, 显示名, 系统回退名)
# - 同一组内多个候选名是同一逻辑库的不同文件名，只在全部落空时告警一次；
# - 系统回退名用于 Windows 自带/驱动安装后位于 System32 的库
#   （winusb.dll 是系统组件；CH347DLLA64.dll 随官方驱动安装）。
#   命中系统回退时不告警——库确实可用，只是不由本程序分发。
_DLL_GROUPS = (
    (("hidapi.dll", "hidapi-hidapi.dll", "libhidapi.dll"),
     "HIDAPI_DLL", "hidapi.dll", ()),
    (("winusb.dll",), "", "winusb.dll", ("winusb.dll",)),
    (("CH347DLL.dll",), "CH347DLL", "CH347DLL.dll",
     ("CH347DLL.dll", "CH347DLLA64.dll")),
)


def _resolve(name: str, env_var: str = "") -> Optional[Path]:
    """解析 DLL 路径（环境变量 > 候选目录），结果缓存，只探测一次。"""
    key = name.lower()
    if key in _resolved:
        return _resolved[key]
    path = None
    if env_var:
        env = os.environ.get(env_var, "").strip()
        if env and Path(env).is_file():
            path = Path(env)
            log.info("原生库 %s：使用环境变量 %s 覆盖路径 %s",
                     name, env_var, path)
    if path is None:
        path = _find_file(name)
    _resolved[key] = path
    return path


def _log_failure(key: str) -> None:
    """同一 DLL 的失败原因只记一次（避免被轮询反复刷屏）。"""
    if key not in _logged_failures:
        _logged_failures.add(key)
        log.warning("原生库加载失败：%s", _load_errors[key])


def _system_loadable(names) -> Optional[str]:
    """按名尝试从系统搜索路径加载（System32 等），成功返回该名。"""
    for nm in names:
        try:
            ctypes.WinDLL(nm)
        except OSError:
            continue
        return nm
    return None


def preload() -> dict:
    """启动时一次性探测全部内置原生库，把“找得到/找不到”定死。

    由 main() 在日志就绪后调用。之后运行期的 load_dll 全是缓存命中，
    既不会再访问文件系统，也不会出现启动之后才冒出来的“未找到 DLL”
    告警（此前由 UI 快照/MCP 轮询触发，看起来像运行期故障）。

    （中途把 DLL 放进目录不会被识别——本程序只在启动时查找。）
    """
    global _SEALED
    for names, env_var, label, sys_names in _DLL_GROUPS:
        hit = None
        for nm in names:
            if _resolve(nm, env_var) is not None:
                hit = nm
                break
        if hit is not None:
            load_dll(hit, env_var)
            continue
        # 随包目录没有：确认系统路径是否可用（系统组件/官方驱动）。
        # 命中则视为可用——库确实存在，只是不由本程序分发。
        if sys_names and _system_loadable(sys_names) is not None:
            log.info("原生库 %s：随包目录无副本，使用系统目录版本", label)
            continue
        key = names[0].lower()
        _load_errors[key] = f"未找到 {label}（可放入 {LIBS_DIR}）"
        _log_failure(key)
    _SEALED = True
    return dict(_load_errors)


def load_dll(name: str, env_var: str = "",
             quiet: bool = False) -> Optional[ctypes.WinDLL]:
    """按名称加载 DLL；失败返回 None 并记录原因（同因只告警一次）。

    路径解析由 _resolve 缓存：每个 DLL 只在首次被解析时扫描候选目录。
    preload() 之后进入封口状态，未在启动期探测过的名字直接判为不可用，
    不再触碰文件系统。

    quiet=True：调用方自带系统目录回退（winusb/CH347），随包目录没有
    属正常情况，只记原因不告警，避免日志里出现“功能其实正常”的
    误导性警告。
    """
    key = name.lower()
    if key in _loaded:
        return _loaded[key]
    if key in _load_errors:      # 已判定失败：仅启动时寻找，不再重试
        return None
    if _SEALED and key not in _resolved:
        _load_errors[key] = (f"{name} 未在启动时探测到"
                             f"（本程序仅在启动时查找原生库，请重启后生效）")
        if not quiet:
            _log_failure(key)
        return None
    path = _resolve(name, env_var)
    if path is None:
        _load_errors[key] = f"未找到 {name}（可放入 {LIBS_DIR}）"
        if not quiet:
            _log_failure(key)
        return None
    try:
        dll = ctypes.WinDLL(str(path))
    except OSError as e:
        _load_errors[key] = f"加载 {name} 失败：{e}"
        if not quiet:
            _log_failure(key)
        return None
    _loaded[key] = dll
    _logged_failures.discard(key)
    log.info("原生库已加载：%s <- %s", name, path)
    return dll


def hexPreview(data: bytes, limit: int = 64) -> str:
    """DEBUG 级详细数据日志用的十六进制预览（截断到 limit 字节）。

    各 worker 的收/发数据日志共用；limit 控制单条日志长度，
    超长数据只截前段并标注总长，避免大流量刷屏。
    """
    b = bytes(data or b"")
    head = b[:limit].hex(" ")
    if len(b) > limit:
        return f"{head} …（共 {len(b)} 字节）"
    return f"{head}（{len(b)} 字节）"


def load_error(name: str) -> str:
    return _load_errors.get(name.lower(), "")


def libs_dir() -> Path:
    return LIBS_DIR

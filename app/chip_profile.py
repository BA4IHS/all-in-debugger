# coding: utf-8
"""芯片包（chip profile）：RTT 目标的预置配置，支持像 ADB 型号包一样自定义添加。

每个芯片包一个 JSON 文件，放在 app/chip_profiles/*.json，格式：

{
  "name": "STM32F103 (Cortex-M3)",       # 必填：显示名
  "kernel": "m3",                        # 必填：内核 key（app.dap_rtt.KERNELS）
  "ram_regions": [[0x20000000, 0x20005000]],  # 可选：RTT 扫描区间 [[start,end],...]
  "swd_speed_khz": 4000,                 # 可选：默认 SWD 时钟（kHz）
  "cb_addr": 0,                          # 可选：固定 RTT 控制块地址（0=自动）
  "desc": "20KB SRAM @ 0x20000000"       # 可选：说明
}

解析失败的文件被 list_profiles 跳过，不中断整体（与 adb_profiles 一致）。
"""
import json
from pathlib import Path
from typing import List, Optional, Tuple

from app import dap_rtt


def chip_dir() -> Path:
    return Path(__file__).resolve().parent / "chip_profiles"


def load_profile(path) -> dict:
    """读取并校验一个芯片包文件；失败抛 ValueError。"""
    p = Path(path)
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("芯片包顶层应为对象")
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("芯片包缺少 name（显示名）")
    kernel = str(data.get("kernel") or "").strip().lower()
    if not kernel:
        raise ValueError("芯片包缺少 kernel（内核 key）")
    keys = {k["key"] for k in dap_rtt.KERNELS}
    if kernel not in keys:
        raise ValueError(f"未知内核 key：{kernel}（可用："
                         + "/".join(sorted(keys)) + "）")
    regions = data.get("ram_regions")
    if regions is not None:
        if not isinstance(regions, list) or not regions:
            raise ValueError("ram_regions 应为非空 [[start,end],...] 列表")
        norm = []
        for r in regions:
            if (not isinstance(r, (list, tuple)) or len(r) != 2
                    or not all(isinstance(v, int) and v >= 0 for v in r)):
                raise ValueError(f"ram_regions 项应为 [start,end]：{r!r}")
            start, end = int(r[0]), int(r[1])
            if start >= end:
                raise ValueError(f"ram_regions 区间 start>=end：{r!r}")
            norm.append([start, end])
        data["ram_regions"] = norm
    speed = data.get("swd_speed_khz")
    if speed is not None:
        speed = int(speed)
        if not 1 <= speed <= 50_000:
            raise ValueError(f"swd_speed_khz 超出范围：{speed}")
        data["swd_speed_khz"] = speed
    cb = data.get("cb_addr")
    if cb is not None:
        cb = int(cb)
        if cb < 0:
            raise ValueError(f"cb_addr 不能为负：{cb}")
        data["cb_addr"] = cb
    data["name"] = name
    data["kernel"] = kernel
    return data


def list_profiles() -> List[Tuple[str, str, dict]]:
    """扫描目录，返回 [(文件名stem, 显示名, 数据), ...]，按显示名排序。

    解析失败的文件被跳过（不中断整体），与 adb_profiles 一致。
    """
    d = chip_dir()
    result = []
    if not d.is_dir():
        return result
    for fp in sorted(d.glob("*.json")):
        try:
            data = load_profile(fp)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        result.append((fp.stem, data["name"], data))
    result.sort(key=lambda t: t[1])
    return result


def find_profile(key: str) -> Optional[dict]:
    """按文件 stem 或显示名查找芯片包；找不到返回 None。"""
    key = (key or "").strip()
    if not key:
        return None
    for stem, name, data in list_profiles():
        if key in (stem, name):
            return data
    return None

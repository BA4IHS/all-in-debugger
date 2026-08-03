# coding: utf-8
"""SEGGER RTT 控制块定位与通道数据收发（基于 SwdTarget 内存访问）。

控制块结构（SEGGER RTT.h，SEGGER_RTT_CB）：
    char  acID[16]            "SEGGER RTT\0"
    u32   MaxNumUpBuffers
    u32   MaxNumDownBuffers
    之后依次是 UP 缓冲区描述符数组、DOWN 缓冲区描述符数组，每个 24 字节：
        char* pName; char* pBuffer; u32 Size; u32 WrOff; u32 RdOff; u32 Flags;
"""
from typing import List, Optional, Tuple

from app.dap_core import DapError, SwdTarget

RTT_SIGNATURE = b"SEGGER RTT\x00\x00\x00\x00\x00\x00"
_DESC_SIZE = 24

# 常见 Cortex-M RAM 起始扫描区（可被用户覆盖）
DEFAULT_RAM_REGIONS = [
    (0x20000000, 0x20040000),
    (0x10000000, 0x10010000),
    (0x30000000, 0x30020000),
]


def find_control_block(target: SwdTarget,
                       regions: Optional[List[Tuple[int, int]]] = None,
                       ap_sel: int = 0) -> Optional[int]:
    """在 RAM 区间内扫描 RTT 控制块签名，返回地址；找不到返回 None。"""
    regions = regions or DEFAULT_RAM_REGIONS
    for start, end in regions:
        addr = start
        while addr + 16 <= end:
            try:
                chunk = target.read_mem_block(addr, min(0x400, end - addr), ap_sel)
            except DapError:
                break  # 该区间不可读（无 RAM），换下一区
            idx = chunk.find(RTT_SIGNATURE)
            if idx >= 0:
                return addr + idx
            # 签名可能跨块边界，回退 16 字节
            addr += len(chunk) - 16
    return None


def parse_control_block(target: SwdTarget, cb_addr: int,
                        ap_sel: int = 0) -> dict:
    """读控制块头，返回通道描述列表。"""
    head = target.read_mem_block(cb_addr, 24, ap_sel)
    if not head.startswith(RTT_SIGNATURE):
        raise DapError(f"{cb_addr:#x} 处不是有效的 RTT 控制块")
    max_up = int.from_bytes(head[16:20], "little")
    max_down = int.from_bytes(head[20:24], "little")
    if max_up > 32 or max_down > 32:
        raise DapError(f"RTT 通道数异常（up={max_up} down={max_down}），"
                       "可能扫描到了错误地址")
    channels = []
    desc_addr = cb_addr + 24
    for i in range(max_up):
        channels.append(_parse_desc(target, desc_addr, "UP", i, ap_sel))
        desc_addr += _DESC_SIZE
    for i in range(max_down):
        channels.append(_parse_desc(target, desc_addr, "DOWN", i, ap_sel))
        desc_addr += _DESC_SIZE
    return {
        "addr": cb_addr,
        "max_up": max_up,
        "max_down": max_down,
        "channels": channels,
    }


def _parse_desc(target: SwdTarget, addr: int, direction: str, index: int,
                ap_sel: int) -> dict:
    raw = target.read_mem_block(addr, _DESC_SIZE, ap_sel)
    name_ptr = int.from_bytes(raw[0:4], "little")
    buf_ptr = int.from_bytes(raw[4:8], "little")
    size = int.from_bytes(raw[8:12], "little")
    name = ""
    if name_ptr:
        try:
            nb = target.read_mem_block(name_ptr, 32, ap_sel)
            name = nb.split(b"\x00", 1)[0].decode("utf-8", "replace")
        except DapError:
            pass
    return {
        "direction": direction,
        "index": index,
        "name": name or f"{direction}{index}",
        "buffer": buf_ptr,
        "size": size,
        "wr_off_addr": addr + 12,
        "rd_off_addr": addr + 16,
    }


def read_channel(target: SwdTarget, ch: dict, ap_sel: int = 0) -> bytes:
    """读一个 UP 通道的待读数据（环形缓冲）。"""
    size = ch["size"]
    if size <= 0 or ch["buffer"] == 0:
        return b""
    offs = target.read_mem_block(ch["wr_off_addr"], 8, ap_sel)
    wr = int.from_bytes(offs[0:4], "little")
    rd = int.from_bytes(offs[4:8], "little")
    if wr >= size or rd >= size or wr == rd:
        return b""
    if rd < wr:
        data = target.read_mem_block(ch["buffer"] + rd, wr - rd, ap_sel)
    else:
        part1 = target.read_mem_block(ch["buffer"] + rd, size - rd, ap_sel)
        part2 = target.read_mem_block(ch["buffer"], wr, ap_sel) if wr else b""
        data = part1 + part2
    target.write_mem32(ch["rd_off_addr"], wr, ap_sel)  # 消费完毕
    return data


def write_channel(target: SwdTarget, ch: dict, data: bytes,
                  ap_sel: int = 0) -> int:
    """向 DOWN 通道写数据（阻塞语义：空间不足截断）。返回写入字节数。"""
    size = ch["size"]
    if size <= 0 or ch["buffer"] == 0 or not data:
        return 0
    offs = target.read_mem_block(ch["wr_off_addr"], 8, ap_sel)
    wr = int.from_bytes(offs[0:4], "little")
    rd = int.from_bytes(offs[4:8], "little")
    if wr >= size or rd >= size:
        return 0
    free = (rd - wr - 1) % size
    n = min(len(data), free)
    if n <= 0:
        return 0
    payload = data[:n]
    if wr + n <= size:
        target.write_mem_block(ch["buffer"] + wr, payload, ap_sel)
    else:
        first = size - wr
        target.write_mem_block(ch["buffer"] + wr, payload[:first], ap_sel)
        target.write_mem_block(ch["buffer"], payload[first:], ap_sel)
    target.write_mem32(ch["wr_off_addr"], (wr + n) % size, ap_sel)
    return n

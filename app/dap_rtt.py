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

# SEGGER RTT 通道槽位固定 0-15 共 16 个（对标 J-Link RTT Viewer）。
# 固件控制块实际只定义 max_up/max_down 个描述符，其余槽位未配置；
# UI/MCP 一律按编号索引，未配置通道读返回空、写拒绝（防止写控制块
# 外内存破坏目标程序）。
MAX_CHANNELS = 16

# 常见 Cortex-M RAM 起始扫描区（可被用户覆盖）
DEFAULT_RAM_REGIONS = [
    (0x20000000, 0x20040000),
    (0x10000000, 0x10010000),
    (0x30000000, 0x30020000),
]

# 异常向量表偏移 0x20 处存 RTT 控制块地址（+2 标记有效）——SEGGER 自动检测惯例
_VTOR_CB_OFFSET = 0x20

# ── 内核预设 ────────────────────────────────────────────────────
# family：m=Cortex-M（有固定向量表惯例 + 通用 SRAM 布局，支持自动检测）；
#         a=Cortex-A（应用处理器，无通用 RAM 布局，须手动指定地址/区间，
#         依据 SEGGER 官方 RTT 文档 "Cortex-A specifics"）。
# ram_regions：None 表示回退内置默认区间；Cortex-A 一律 None（不自动扫描）。
# desc：依据 SEGGER 官方 RTT 文档（kb.segger.com/RTT）撰写。
KERNELS = [
    {"key": "auto", "name": "自动（默认）", "family": "m",
     "ram_regions": None,
     "desc": "先按 SEGGER 惯例读向量表 0x20，再扫描内置常见 Cortex-M RAM 区间"},
    {"key": "m0", "name": "Cortex-M0 / M0+", "family": "m",
     "ram_regions": DEFAULT_RAM_REGIONS,
     "desc": "ARMv6-M 入门内核，SRAM 惯例基址 0x20000000"},
    {"key": "m3", "name": "Cortex-M3", "family": "m",
     "ram_regions": DEFAULT_RAM_REGIONS,
     "desc": "ARMv7-M（项目实测 STM32F103 IDCODE=0x1BA01477）"},
    {"key": "m4", "name": "Cortex-M4", "family": "m",
     "ram_regions": DEFAULT_RAM_REGIONS,
     "desc": "ARMv7-M + DSP/FPU，SRAM 惯例基址 0x20000000"},
    {"key": "m7", "name": "Cortex-M7", "family": "m",
     "ram_regions": DEFAULT_RAM_REGIONS,
     "desc": "ARMv7-M + 缓存：RTT 控制块/缓冲需 cache line 对齐"},
    {"key": "m23", "name": "Cortex-M23", "family": "m",
     "ram_regions": DEFAULT_RAM_REGIONS,
     "desc": "ARMv8-M Baseline，注意 TrustZone 非安全区域可能不可访问"},
    {"key": "m33", "name": "Cortex-M33", "family": "m",
     "ram_regions": DEFAULT_RAM_REGIONS,
     "desc": "ARMv8-M Mainline，SRAM 惯例基址 0x20000000"},
    {"key": "a7", "name": "Cortex-A7", "family": "a",
     "ram_regions": None,
     "desc": "ARMv7-A 应用处理器：无通用 RAM 布局，须手动指定控制块地址/RAM 区间"},
    {"key": "a53", "name": "Cortex-A53", "family": "a",
     "ram_regions": None,
     "desc": "ARMv8-A 应用处理器：无通用 RAM 布局，须手动指定控制块地址/RAM 区间"},
]


def get_kernel(key: Optional[str]) -> dict:
    """按 key 取内核预设；未知 key 回退 auto。"""
    key = str(key or "auto").lower()
    return next((k for k in KERNELS if k["key"] == key), KERNELS[0])


def cb_from_vector_table(target: SwdTarget, ap_sel: int = 0) -> Optional[int]:
    """按 SEGGER 惯例从向量表偏移 0x20 读取 RTT 控制块地址。

    有效格式：向量表项 8 存放 (控制块地址 + 2) 以标记有效性（bit1=1）；
    读回后需校验该地址处确为 RTT 签名才认定有效。
    仅 Cortex-M 适用；读不到/格式无效返回 None（调用方回退 RAM 扫描）。
    """
    try:
        val = target.read_mem32(_VTOR_CB_OFFSET, ap_sel)
    except DapError:
        return None
    if not val or not (val & 2):        # bit1=1 才认为已标记
        return None
    cb = (val & 0xFFFFFFFE) - 2
    if not (0x20000000 <= cb < 0x60000000):
        return None                     # 明显不在常见 RAM 映射，拒绝
    try:
        head = target.read_mem_block(cb, 16, ap_sel)
    except DapError:
        return None
    return cb if head == RTT_SIGNATURE else None


def find_control_block(target: SwdTarget,
                       regions: Optional[List[Tuple[int, int]]] = None,
                       ap_sel: int = 0,
                       try_vtor: bool = True) -> Optional[int]:
    """定位 RTT 控制块，返回地址；找不到返回 None。

    try_vtor=True 时先按 SEGGER 惯例尝试向量表 0x20 快速定位，
    失败再扫描 regions（找不到即认为未初始化）。
    """
    if try_vtor:
        cb = cb_from_vector_table(target, ap_sel)
        if cb:
            return cb
    regions = regions or DEFAULT_RAM_REGIONS
    for start, end in regions:
        addr = start
        while addr + 16 <= end:
            try:
                # 块长对齐到 4 字节，避免 read_mem_block 尾部越界取整
                count = min(0x400, end - addr) & ~3
                chunk = target.read_mem_block(addr, count, ap_sel)
            except DapError:
                break  # 该区间不可读（无 RAM），换下一区
            idx = chunk.find(RTT_SIGNATURE)
            if idx >= 0:
                return addr + idx
            # 签名可能跨块边界，回退 16 字节重叠；剩余不足 16 字节时
            # 该尾部已在上一次读取的最后 16 字节内覆盖，直接结束本区间
            # （原实现 addr += len(chunk) - 16 在 len==16 时恒不前进，死循环）
            step = len(chunk) - 16
            if step <= 0:
                break
            addr += step
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


def channel_by_index(rtt: dict, direction: str,
                     index: int) -> Optional[dict]:
    """按通道号取通道描述；未配置（超出固件 max_up/max_down）返回 None。

    通道号即描述符数组索引：UP 0..max_up-1、DOWN 0..max_down-1。
    读取未配置通道返回 None，调用方应返回空数据或拒绝写入。
    """
    if index < 0 or index >= MAX_CHANNELS:
        return None
    return next((c for c in rtt.get("channels", [])
                 if c["direction"] == direction and c["index"] == index),
                None)


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

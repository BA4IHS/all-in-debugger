# coding: utf-8
"""串口相关纯函数与常量表：HEX 解析/格式化、增量解码、换行符、端口枚举。

全部为无副作用函数（list_ports 除外），可独立单测。
"""
import codecs
import re
import time
from typing import List, Optional, Tuple

import serial
from serial.tools import list_ports as _list_ports

# ---------------------------------------------------------------------------
# 常量表
# ---------------------------------------------------------------------------

BAUDRATES = [
    "1200", "2400", "4800", "9600", "19200", "38400",
    "57600", "115200", "230400", "460800", "921600",
]

DATABITS = ["5", "6", "7", "8"]

STOPBIT_MAP = {
    "1": serial.STOPBITS_ONE,
    "1.5": serial.STOPBITS_ONE_POINT_FIVE,
    "2": serial.STOPBITS_TWO,
}

PARITY_MAP = {
    "None": serial.PARITY_NONE,
    "Even": serial.PARITY_EVEN,
    "Odd": serial.PARITY_ODD,
    "Mark": serial.PARITY_MARK,
    "Space": serial.PARITY_SPACE,
}

# 流控选项 -> (xonxoff, rtscts)
FLOWCONTROL_MAP = {
    "None": (False, False),
    "XON/XOFF": (True, False),
    "RTS/CTS": (False, True),
}

NEWLINE_MAP = {
    "None": b"",
    "CR": b"\r",
    "LF": b"\n",
    "CR+LF": b"\r\n",
}

CODECS = ["UTF-8", "GBK", "ASCII"]

_HEX_CHARS = set("0123456789abcdef")


# ---------------------------------------------------------------------------
# HEX 解析 / 格式化
# ---------------------------------------------------------------------------

def parse_hex_input(text: str) -> Tuple[Optional[bytes], str]:
    """把用户输入的十六进制字符串解析为 bytes。

    容忍空格、逗号（中英文）、0x 前缀、大小写混用；
    支持分组（'AA BB 0F'）与连续（'AABB0F'）两种写法。

    Returns:
        (bytes, '')  成功
        (None, msg)  失败，msg 为带位置的错误描述
    """
    if text is None:
        return None, "内容为空"
    raw = text.strip()
    if not raw:
        return None, "内容为空"

    normalized = raw.replace(",", " ").replace("，", " ").replace("0x", " ").replace("0X", " ")
    groups = [g for g in normalized.split() if g]
    if not groups:
        return None, "没有有效的十六进制内容"

    hex_chars = []
    for g in groups:
        low = g.lower()
        if len(low) % 2 != 0:
            return None, f"分组 '{g}' 长度为奇数，无法解析"
        if not all(c in _HEX_CHARS for c in low):
            return None, f"分组 '{g}' 含有非十六进制字符"
        hex_chars.append(low)

    try:
        return bytes.fromhex("".join(hex_chars)), ""
    except ValueError as e:
        return None, f"解析失败：{e}"


def format_hex(data: bytes, sep: str = " ") -> str:
    """b'\\xaa\\xbb' -> 'AA BB'"""
    return sep.join(f"{b:02X}" for b in data)


def is_valid_hex_input(text: str) -> bool:
    data, _ = parse_hex_input(text)
    return data is not None


# ---------------------------------------------------------------------------
# 文本编解码（增量，跨包半个多字节字符不出错）
# ---------------------------------------------------------------------------

def make_decoder(codec: str):
    """创建增量解码器。errors='replace'：非法字节显示为替换符而非抛异常。"""
    try:
        return codecs.getincrementaldecoder(codec)(errors="replace")
    except LookupError:
        return codecs.getincrementaldecoder("utf-8")(errors="replace")


def decode_chunk(decoder, data: bytes) -> str:
    """增量解码一块字节，final=False 保留不完整的多字节尾部到下一块。"""
    return decoder.decode(data, False)


def encode_text(text: str, codec: str) -> bytes:
    try:
        return text.encode(codec, errors="replace")
    except LookupError:
        return text.encode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 换行符 / 时间戳 / 计数
# ---------------------------------------------------------------------------

def append_newline(payload: bytes, mode: str) -> bytes:
    return payload + NEWLINE_MAP.get(mode, b"")


def timestamp_str(ts: Optional[float] = None) -> str:
    """[HH:MM:SS.zzz] 格式时间戳。"""
    if ts is None:
        ts = time.time()
    return time.strftime("[%H:%M:%S", time.localtime(ts)) + f".{int(ts % 1 * 1000):03d}]"


def fmt_bytes(n: int) -> str:
    """字节数人性化显示：1234 -> '1.21 KB'"""
    n = max(0, int(n))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.2f} GB"


# ---------------------------------------------------------------------------
# 端口枚举
# ---------------------------------------------------------------------------

def _is_null_com_port(device: str) -> bool:
    """识别 ELTIMA 等虚拟串口工具生成的 NULL_COM* 控制端口。"""
    normalized = (device or "").strip().upper().replace(" ", "_")
    return normalized.startswith("NULL_COM")


def list_serial_ports() -> List[Tuple[str, str]]:
    """返回 [(端口名, 描述), ...]，过滤 NULL_COM* 后按端口名排序。"""
    try:
        ports = [
            p for p in _list_ports.comports()
            if not _is_null_com_port(p.device)
        ]
        ports.sort(key=lambda p: p.device)
    except Exception:
        return []
    return [(p.device, p.description or p.device) for p in ports]


def format_port_label(device: str, description: str) -> str:
    """组合下拉显示文本：'COM13 - USB-SERIAL CH340'。

    去掉描述尾部与端口名重复的 '(COMxx)'，避免 'COM13 - ... (COM13)' 冗余；
    描述为空或与端口名相同时只显示端口名。
    """
    desc = (description or "").strip()
    if desc:
        desc = re.sub(r"\(\s*" + re.escape(device) + r"\s*\)\s*$", "",
                      desc, flags=re.I).strip()
    if not desc or desc.lower() == device.lower():
        return device
    return f"{device} - {desc}"


def build_open_config(port: str, baudrate: str, databits: str, stopbits: str,
                      parity: str, flowcontrol: str, dtr: bool, rts: bool) -> dict:
    """把 UI 选项组装成 serial.Serial / serial_for_url 参数。

    Raises:
        ValueError: 参数非法（波特率非数字等）
    """
    try:
        baud = int(baudrate)
        if baud <= 0:
            raise ValueError
    except ValueError:
        raise ValueError(f"波特率无效：{baudrate!r}")

    if stopbits not in STOPBIT_MAP:
        raise ValueError(f"停止位无效：{stopbits!r}")
    if parity not in PARITY_MAP:
        raise ValueError(f"校验位无效：{parity!r}")
    if flowcontrol not in FLOWCONTROL_MAP:
        raise ValueError(f"流控无效：{flowcontrol!r}")

    xonxoff, rtscts = FLOWCONTROL_MAP[flowcontrol]
    return {
        "port": port,
        "baudrate": baud,
        "bytesize": int(databits),
        "stopbits": STOPBIT_MAP[stopbits],
        "parity": PARITY_MAP[parity],
        "xonxoff": xonxoff,
        "rtscts": rtscts,
        "dsrdtr": dtr,
        "timeout": 0.05,
        "write_timeout": 1,
        "dtr": dtr,
        "rts": rts,
    }

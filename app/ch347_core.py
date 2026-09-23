# coding: utf-8
"""CH347DLL.dll 的 ctypes 封装与纯逻辑（移植自 WCH 官方 CH347Demo）。

约定（与 hid/dap 模块一致）：
- 设备句柄（DLL 按索引 0~15 管理）只允许在 worker 线程内通过
  Ch347Device 构造/销毁，UI 线程仅调用本模块的纯函数；
- DLL 缺失时优雅降级：load_ch347_dll() 返回 None，由页面显示安装指引；
- CH347 全部调用为同步 USB 传输，单次流上限 4096 字节，长数据自动分块。

DLL 来源：安装 WCH 官方 CH347 驱动后，CH347DLLA64.DLL 已位于系统目录
（System32），ctypes 按名即可解析，无需随软件分发；可选地用环境变量
CH347DLL 或 app/libs/ch347/ 内的副本覆盖（便于测试新版 DLL）。
x64 Python 只能加载 64 位 DLL。
"""
import ctypes
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from app.native import LIBS_DIR, load_dll, load_error

log = logging.getLogger(__name__)

DLL_NAME = "CH347DLL.dll"
DLL_ENV = "CH347DLL"
# 驱动安装到系统目录后的候选导出名（按优先级尝试，不随包分发）
_SYSTEM_DLL_NAMES = (DLL_NAME, "CH347DLLA64.dll")
MAX_STREAM = 4096          # 单次 SPI/I2C 流最大字节数（mMAX_BUFFER_LENGTH）
_INVALID_HANDLES = (None, 0, -1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)


class Ch347Error(RuntimeError):
    """CH347 设备层错误（DLL 缺失/调用失败），消息可直接上 UI。"""


# ── ctypes 结构（对应 CH347DLL.H，#pragma pack(1)）──────────────────

class SPI_CONFIG(ctypes.Structure):
    """mSpiCfgS：SPI 接口配置，pack=1 共 20 字节。"""
    _pack_ = 1
    _fields_ = [
        ("iMode", ctypes.c_ubyte),                 # 0-3: SPI Mode0/1/2/3
        ("iClock", ctypes.c_ubyte),               # 0=60MHz .. 7=468.75KHz
        ("iByteOrder", ctypes.c_ubyte),           # 0=LSB 先行, 1=MSB 先行
        ("iSpiWriteReadInterval", ctypes.c_ushort),  # 读写间隔 us
        ("iSpiOutDefaultData", ctypes.c_ubyte),   # 读取时默认输出字节
        ("iChipSelect", ctypes.c_uint32),         # bit7=1 使能片选, bit1:0=CS1/CS2
        ("CS1Polarity", ctypes.c_ubyte),          # 0=低有效 1=高有效
        ("CS2Polarity", ctypes.c_ubyte),
        ("iIsAutoDeativeCS", ctypes.c_ushort),    # 传完自动撤销片选
        ("iActiveDelay", ctypes.c_ushort),        # 片选建立延时 us
        ("iDelayDeactive", ctypes.c_uint32),      # 片选释放延时 us
    ]


class DEVICE_INFOR(ctypes.Structure):
    """mDeviceInforS：设备信息（仅用于解析，字段序与头文件一致）。"""
    _pack_ = 1
    _fields_ = [
        ("iIndex", ctypes.c_ubyte),
        ("DevicePath", ctypes.c_ubyte * 260),
        ("UsbClass", ctypes.c_ubyte),
        ("FuncType", ctypes.c_ubyte),
        ("DeviceID", ctypes.c_char * 64),
        ("ChipMode", ctypes.c_ubyte),
        ("DevHandle", ctypes.c_void_p),
        ("BulkOutEndpMaxSize", ctypes.c_ushort),
        ("BulkInEndpMaxSize", ctypes.c_ushort),
        ("UsbSpeedType", ctypes.c_ubyte),
        ("CH347IfNum", ctypes.c_ubyte),
        ("DataUpEndp", ctypes.c_ubyte),
        ("DataDnEndp", ctypes.c_ubyte),
        ("ProductString", ctypes.c_char * 64),
        ("ManufacturerString", ctypes.c_char * 64),
        ("WriteTimeout", ctypes.c_uint32),
        ("ReadTimeout", ctypes.c_uint32),
        ("FuncDescStr", ctypes.c_char * 64),
        ("FirewareVer", ctypes.c_ubyte),
    ]


# ── 常量表 ────────────────────────────────────────────────────────────

CHIP_TYPE_NAMES = {0: "CH341", 1: "CH347T", 2: "CH347F", 3: "CH339W"}

# iClock 档位（CH347SPI_Init），60MHz÷2^n；SetFrequency 支持任意值时优先用它
SPI_CLK_NAMES = ["60MHz", "30MHz", "15MHz", "7.5MHz",
                 "3.75MHz", "1.875MHz", "937.5KHz", "468.75KHz"]

# I2C 速率位域（CH347I2C_Set 低 3 位）
I2C_SPEEDS = [("20KHz", 0), ("100KHz", 1), ("400KHz", 2), ("750KHz", 3),
              ("50KHz", 4), ("200KHz", 5), ("1MHz", 6)]

# SPI Flash 指令集（源自 SPI_FLASH.h）
CMD_FLASH_READ = 0x03
CMD_FLASH_H_READ = 0x0B
CMD_FLASH_BYTE_PROG = 0x02
CMD_FLASH_SECTOR_ERASE = 0x20      # 4KB
CMD_FLASH_BLOCK_ERASE_32K = 0x52
CMD_FLASH_BLOCK_ERASE_64K = 0xD8
CMD_FLASH_CHIP_ERASE = 0xC7
CMD_FLASH_RDSR = 0x05
CMD_FLASH_WRSR = 0x01
CMD_FLASH_EWSR = 0x50
CMD_FLASH_WREN = 0x06
CMD_FLASH_WRDI = 0x04
CMD_FLASH_JEDEC_ID = 0x9F

FLASH_SECTOR_SIZE = 4096
FLASH_PAGE_SIZE = 256

ERASE_GRANULARITY = {"sector4k": (4096, CMD_FLASH_SECTOR_ERASE),
                    "block32k": (32768, CMD_FLASH_BLOCK_ERASE_32K),
                    "block64k": (65536, CMD_FLASH_BLOCK_ERASE_64K)}

# 常见厂商（JEDEC ID 首字节），未收录显示 Vendor 0xNN
FLASH_VENDORS = {
    0xEF: "Winbond", 0xC8: "GigaDevice", 0xC2: "Macronix", 0x20: "ST",
    0xD9: "Kontron", 0x62: "ESMT", 0xB3: "ISSI", 0x1C: "EON",
    0x52: "Oryx", 0x7F: "冷闪/杂牌", 0x85: "Puya", 0x68: "BOYA",
}

# JEDEC 24bit ID → (型号, 容量字节)（移植自 SPI_FLASH.h，含主要系列）
JEDEC_TABLE = {
    0xEF3011: ("W25X10", 128 * 1024),
    0xEF3012: ("W25X20", 256 * 1024),
    0xEF3013: ("W25X40", 512 * 1024),
    0xEF4014: ("W25X80", 1024 * 1024),
    0xEF3015: ("W25Q16", 2 * 1024 * 1024),
    0xEF4015: ("W25Q16", 2 * 1024 * 1024),
    0xEF4016: ("W25Q32", 4 * 1024 * 1024),
    0xEF6016: ("W25Q32", 4 * 1024 * 1024),
    0xEF4017: ("W25Q64", 8 * 1024 * 1024),
    0xEF6017: ("W25Q64", 8 * 1024 * 1024),
    0xEF4018: ("W25Q128", 16 * 1024 * 1024),
    0xEF6018: ("W25Q128", 16 * 1024 * 1024),
    0xEF4019: ("W25Q256", 32 * 1024 * 1024),
    0xEF6019: ("W25Q256", 32 * 1024 * 1024),
    0xBF258D: ("SST25VF040", 512 * 1024),
    0xBF258E: ("SST25VF080", 1024 * 1024),
    0xBF2541: ("SST25VF016", 2 * 1024 * 1024),
    0xBF254A: ("SST25VF032", 4 * 1024 * 1024),
    0xBF254B: ("SST25VF064", 8 * 1024 * 1024),
    0x202013: ("M25P40", 512 * 1024),
    0x202014: ("M25P80", 1024 * 1024),
    0x202015: ("M25P16", 2 * 1024 * 1024),
    0x202016: ("M25P32", 4 * 1024 * 1024),
    0x202017: ("M25P64", 8 * 1024 * 1024),
    0xC22013: ("MX25L40", 512 * 1024),
    0xC22014: ("MX25L80", 1024 * 1024),
    0xC22015: ("MX25L16", 2 * 1024 * 1024),
    0xC22016: ("MX25L32", 4 * 1024 * 1024),
    0xC22017: ("MX25L64", 8 * 1024 * 1024),
    0x20BA17: ("MT25Q64", 8 * 1024 * 1024),
    0x20BA18: ("MT25Q128", 16 * 1024 * 1024),
    0x20BA19: ("MT25Q256", 32 * 1024 * 1024),
    0x20BA20: ("MT25Q512", 64 * 1024 * 1024),
    0x20BA21: ("MT25Q1024", 128 * 1024 * 1024),
    0x20BA22: ("MT25Q2048", 256 * 1024 * 1024),
}

# EEPROM 型号（24Cxx）：枚举值即 CH347DLL.H 的 EEPROM_TYPE 顺序
EEPROM_MODELS = [
    {"name": "24C01", "enum": 0, "size": 128},
    {"name": "24C02", "enum": 1, "size": 256},
    {"name": "24C04", "enum": 2, "size": 512},
    {"name": "24C08", "enum": 3, "size": 1024},
    {"name": "24C16", "enum": 4, "size": 2048},
    {"name": "24C32", "enum": 5, "size": 4096},
    {"name": "24C64", "enum": 6, "size": 8192},
    {"name": "24C128", "enum": 7, "size": 16384},
    {"name": "24C256", "enum": 8, "size": 32768},
    {"name": "24C512", "enum": 9, "size": 65536},
    {"name": "24C1024", "enum": 10, "size": 131072},
    {"name": "24C2048", "enum": 11, "size": 262144},
    {"name": "24C4096", "enum": 12, "size": 524288},
]

# LCD 公共命令（ST7789/ILI9341 兼容子集）
LCD_CMD_SWRESET = 0x01
LCD_CMD_SLPLOT = 0x11
LCD_CMD_DISPON = 0x29
LCD_CMD_CASET = 0x2A
LCD_CMD_RASET = 0x2B
LCD_CMD_RAMWR = 0x2C
LCD_CMD_MADCTL = 0x36
LCD_CMD_COLMOD = 0x3A

FLASH_CAP_LIMIT = 32 * 1024 * 1024   # Flash 区域操作/文件上限 32MB


# ── 纯函数：HEX / 地址拆分 / JEDEC ───────────────────────────────────

def parse_hex(text: str) -> bytes:
    """解析 HEX 字符串：空格/逗号/换行分隔、0x 前缀、连续十六进制串。"""
    t = str(text or "").replace(",", " ").replace("0x", " ").replace("0X", " ")
    toks = [x for x in t.split() if x]
    if not toks:
        raise ValueError("HEX 数据为空")
    if len(toks) == 1 and len(toks[0]) > 2 and len(toks[0]) % 2 == 0:
        try:
            return bytes.fromhex(toks[0])
        except ValueError:
            pass
    try:
        vals = [int(x, 16) for x in toks]
    except ValueError:
        raise ValueError(f"无法解析 HEX 数据：{text[:60]}…") from None
    if any(v < 0 or v > 0xFF for v in vals):
        raise ValueError("存在超出 0x00~0xFF 的字节")
    return bytes(vals)


def format_hex(data, per_line: int = 16) -> str:
    """字节 → 每行 16 个大写 HEX。"""
    bs = bytes(data or b"")
    lines = []
    for i in range(0, len(bs), per_line):
        lines.append(" ".join(f"{b:02X}" for b in bs[i:i + per_line]))
    return "\n".join(lines)


def parse_addr(text) -> int:
    """地址输入解析（接受 int 或 '0x..'/'FF A0' 等 HEX 文本）。"""
    if isinstance(text, int):
        return text
    t = str(text or "").strip()
    if not t:
        raise ValueError("地址为空")
    try:
        return int(t, 16) if not t.isdigit() else int(t, 16)
    except ValueError:
        raise ValueError(f"非法地址：{text}") from None


def flash_page_split(addr: int, data) -> List[tuple]:
    """按 256B 页边界拆分 → [(页起始地址, 页内数据), ...]。"""
    data = bytes(data)
    if not data:
        return []
    out = []
    cur = addr
    pos = 0
    while pos < len(data):
        room = FLASH_PAGE_SIZE - (cur % FLASH_PAGE_SIZE)
        n = min(room, len(data) - pos)
        out.append((cur, data[pos:pos + n]))
        cur += n
        pos += n
    return out


def flash_erase_addrs(addr: int, length: int, granularity: int) -> List[int]:
    """覆盖 [addr, addr+length) 所需的擦除块地址列表。

    granularity ∈ {4096, 32768, 65536}；首尾按粒度向下对齐（首块/尾块
    整块擦除，块内非本次范围的数据同样被抹掉——与官方 Demo 行为一致）。
    """
    if granularity not in (4096, 32768, 65536):
        raise ValueError(f"不支持的擦除粒度 {granularity}")
    length = int(length)
    if length <= 0:
        raise ValueError("擦除长度必须大于 0")
    start = int(addr) & ~(granularity - 1)
    end = (int(addr) + length + granularity - 1) & ~(granularity - 1)
    return list(range(start, end, granularity))


def jedec_to_info(id_bytes) -> dict:
    """JEDEC ID → {id_hex, vendor, model, capacity}；未收录按容量字节回退。"""
    b = bytes(id_bytes)
    if len(b) < 3:
        raise ValueError("JEDEC ID 需要 3 字节")
    id24 = (b[0] << 16) | (b[1] << 8) | b[2]
    vendor = FLASH_VENDORS.get(b[0], f"Vendor 0x{b[0]:02X}")
    model, capacity = "", None
    if id24 in JEDEC_TABLE:
        model, capacity = JEDEC_TABLE[id24]
    elif 0x10 <= b[2] <= 0x19:
        # 通用编码：容量字节 = log2(字节数)（0x14 → 1MB）
        model = f"{vendor} 兼容片"
        capacity = 1 << b[2]
    return {"id_hex": f"{id24:06X}", "id_bytes": b.hex().upper(),
            "vendor": vendor, "model": model,
            "capacity": capacity if capacity and capacity <= FLASH_CAP_LIMIT
            else None,
            "capacity_raw": capacity}


# ── 用户脚本结构（data.json ch347 字段持久化 + MCP 传参共用）──────────

@dataclass
class GpioMacroStep:
    """GPIO 序列宏一步：把 pin 置为 level 后延时 delay_ms。"""
    pin: int = 0
    level: int = 0
    delay_ms: int = 0

    def validate(self) -> "GpioMacroStep":
        if not 0 <= int(self.pin) <= 7:
            raise ValueError(f"GPIO 引脚须为 0~7：{self.pin}")
        if int(self.level) not in (0, 1):
            raise ValueError(f"电平须为 0/1：{self.level}")
        if not 0 <= int(self.delay_ms) <= 5000:
            raise ValueError(f"延时须为 0~5000ms：{self.delay_ms}")
        return self

    @classmethod
    def from_dict(cls, d: dict) -> "GpioMacroStep":
        d = d or {}
        return cls(pin=int(d.get("pin", 0)), level=int(d.get("level", 0)),
                   delay_ms=int(d.get("delay_ms", d.get("ms", 0)))).validate()

    def to_dict(self) -> dict:
        return {"pin": int(self.pin), "level": int(self.level),
                "delay_ms": int(self.delay_ms)}


@dataclass
class I2cScriptStep:
    """I2C 脚本一步：向 addr7 写 write_hex（首字节可含寄存器），读回 read_len。"""
    addr7: int = 0x50
    write_hex: str = ""
    read_len: int = 0
    delay_ms: int = 0

    def validate(self) -> "I2cScriptStep":
        if not 0x03 <= int(self.addr7) <= 0x77:
            raise ValueError(f"7bit 设备地址须为 0x03~0x77：{self.addr7}")
        if not 0 <= int(self.read_len) <= 256:
            raise ValueError(f"读取长度须为 0~256：{self.read_len}")
        if not 0 <= int(self.delay_ms) <= 10000:
            raise ValueError(f"延时须为 0~10000ms：{self.delay_ms}")
        data = parse_hex(self.write_hex) if str(self.write_hex).strip() \
            else b""
        if len(data) > 256:
            raise ValueError("单步写入数据不能超过 256 字节")
        return self

    @property
    def write_bytes(self) -> bytes:
        s = str(self.write_hex).strip()
        return parse_hex(s) if s else b""

    @classmethod
    def from_dict(cls, d: dict) -> "I2cScriptStep":
        d = d or {}
        return cls(addr7=int(d.get("addr7", d.get("addr", 0x50))),
                   write_hex=str(d.get("write_hex", d.get("write", ""))),
                   read_len=int(d.get("read_len", d.get("read", 0))),
                   delay_ms=int(d.get("delay_ms", d.get("ms", 0)))).validate()

    def to_dict(self) -> dict:
        return {"addr7": int(self.addr7), "write_hex": self.write_hex,
                "read_len": int(self.read_len),
                "delay_ms": int(self.delay_ms)}


@dataclass
class LcdInitStep:
    """LCD 初始化一步：cmd=发命令 / data=发数据 / delay=延时。"""
    kind: str = "cmd"
    hex: str = ""
    ms: int = 0

    def validate(self) -> "LcdInitStep":
        if self.kind not in ("cmd", "data", "delay"):
            raise ValueError(f"步类型须为 cmd/data/delay：{self.kind}")
        if not 0 <= int(self.ms) <= 10000:
            raise ValueError(f"延时须为 0~10000ms：{self.ms}")
        data = self.payload
        if self.kind == "cmd" and len(data) != 1:
            raise ValueError(f"cmd 步须恰好 1 字节命令：{self.hex}")
        if self.kind == "data" and not 1 <= len(data) <= 256:
            raise ValueError("data 步须为 1~256 字节")
        return self

    @property
    def payload(self) -> bytes:
        s = str(self.hex).strip()
        return parse_hex(s) if s else b""

    @classmethod
    def from_dict(cls, d: dict) -> "LcdInitStep":
        d = d or {}
        return cls(kind=str(d.get("kind", "cmd")),
                   hex=str(d.get("hex", "")),
                   ms=int(d.get("ms", 0))).validate()

    def to_dict(self) -> dict:
        return {"kind": self.kind, "hex": self.hex, "ms": int(self.ms)}


@dataclass
class LcdProfile:
    """SPI 屏幕配置：几何/色序/引脚映射/SPI 参数/初始化序列。"""
    name: str = "自定义"
    controller: str = "custom"        # st7789 | ili9341 | custom
    width: int = 240
    height: int = 320
    rgb_order: str = "RGB"            # RGB | BGR
    mirror_x: bool = False
    mirror_y: bool = False
    dc_pin: int = 0
    res_pin: int = 1                  # -1 = 不用硬复位
    blk_pin: int = -1                 # -1 = 不用背光控制
    spi_mode: int = 0
    spi_clk: int = 0                  # CH347 时钟档 0~7
    steps: List[LcdInitStep] = field(default_factory=list)

    def validate(self) -> "LcdProfile":
        if not 8 <= int(self.width) <= 480 or not 8 <= int(self.height) <= 480:
            raise ValueError(f"分辨率须为 8~480：{self.width}x{self.height}")
        if int(self.width) * int(self.height) > 0x40000:
            raise ValueError("像素总数不能超过 262144（约 512x512）")
        if not 0 <= int(self.dc_pin) <= 7:
            raise ValueError(f"DC 引脚须为 0~7：{self.dc_pin}")
        for p in (self.res_pin, self.blk_pin):
            if int(p) != -1 and not 0 <= int(p) <= 7:
                raise ValueError(f"复位/背光引脚须为 0~7 或 -1：{p}")
        if not 0 <= int(self.spi_mode) <= 3:
            raise ValueError(f"SPI Mode 须为 0~3：{self.spi_mode}")
        if not 0 <= int(self.spi_clk) <= 7:
            raise ValueError(f"SPI 时钟档须为 0~7：{self.spi_clk}")
        if self.rgb_order not in ("RGB", "BGR"):
            raise ValueError(f"色序须为 RGB/BGR：{self.rgb_order}")
        if len(self.steps) > 2048:
            raise ValueError("初始化序列过长")
        self.steps = [s if isinstance(s, LcdInitStep)
                      else LcdInitStep.from_dict(s) for s in self.steps]
        return self

    @property
    def madctl(self) -> int:
        """0x36 MADCTL：bit3=1 RGB；MX/MY 由 mirror 开关决定。"""
        v = 0x08 if self.rgb_order == "RGB" else 0x00
        if self.mirror_x:
            v |= 0x40
        if self.mirror_y:
            v |= 0x80
        return v

    @classmethod
    def from_dict(cls, d: dict) -> "LcdProfile":
        d = d or {}
        return cls(
            name=str(d.get("name", "自定义")),
            controller=str(d.get("controller", "custom")),
            width=int(d.get("width", 240)), height=int(d.get("height", 320)),
            rgb_order=str(d.get("rgb_order", "RGB")),
            mirror_x=bool(d.get("mirror_x", False)),
            mirror_y=bool(d.get("mirror_y", False)),
            dc_pin=int(d.get("dc_pin", 0)), res_pin=int(d.get("res_pin", 1)),
            blk_pin=int(d.get("blk_pin", -1)),
            spi_mode=int(d.get("spi_mode", 0)),
            spi_clk=int(d.get("spi_clk", 0)),
            steps=[LcdInitStep.from_dict(s) for s in d.get("steps", [])],
        ).validate()

    def to_dict(self) -> dict:
        return {"name": self.name, "controller": self.controller,
                "width": int(self.width), "height": int(self.height),
                "rgb_order": self.rgb_order,
                "mirror_x": bool(self.mirror_x),
                "mirror_y": bool(self.mirror_y),
                "dc_pin": int(self.dc_pin), "res_pin": int(self.res_pin),
                "blk_pin": int(self.blk_pin),
                "spi_mode": int(self.spi_mode), "spi_clk": int(self.spi_clk),
                "steps": [s.to_dict() for s in self.steps]}


def _steps(specs) -> List[LcdInitStep]:
    return [LcdInitStep(kind, hx, ms).validate()
            for kind, hx, ms in specs]


# 常用控制器初始化序列模板（cmd 单字节 / data 载荷 / delay 毫秒）
LCD_TEMPLATES = {
    "st7789": [
        ("cmd", "01", 0), ("delay", "", 120),
        ("cmd", "2A", 0), ("data", "00 00 00 EF", 0),
        ("cmd", "2B", 0), ("data", "00 00 01 3F", 0),
        ("cmd", "11", 0), ("delay", "", 120),
        ("cmd", "3A", 0), ("data", "05", 0),
        ("cmd", "B2", 0), ("data", "0C 0C 00 33 33", 0),
        ("cmd", "B7", 0), ("data", "35", 0),
        ("cmd", "BB", 0), ("data", "19", 0),
        ("cmd", "C0", 0), ("data", "2C", 0),
        ("cmd", "C2", 0), ("data", "01 FF", 0),
        ("cmd", "C3", 0), ("data", "12 2C", 0),
        ("cmd", "C4", 0), ("data", "12 2C", 0),
        ("cmd", "C6", 0), ("data", "2C", 0),
        ("cmd", "D0", 0), ("data", "A4 A1", 0),
        ("cmd", "21", 0), ("cmd", "29", 0), ("delay", "", 100),
    ],
    "ili9341": [
        ("cmd", "01", 0), ("delay", "", 100),
        ("cmd", "EF", 0), ("data", "03 80 02", 0),
        ("cmd", "CF", 0), ("data", "00 C1 30", 0),
        ("cmd", "ED", 0), ("data", "64 03 12 81", 0),
        ("cmd", "DB", 0), ("data", "93", 0),
        ("cmd", "DC", 0), ("data", "32 98", 0),
        ("cmd", "CD", 0), ("data", "15", 0),
        ("cmd", "E8", 0), ("data", "85 00 78", 0),
        ("cmd", "20", 0),
        ("cmd", "B7", 0), ("data", "02", 0),
        ("cmd", "B6", 0), ("data", "0A 82 27 00", 0),
        ("cmd", "F7", 0), ("data", "81 00 06", 0),
        ("cmd", "C0", 0), ("data", "21 3C", 0),
        ("cmd", "C1", 0), ("data", "11", 0),
        ("cmd", "C5", 0), ("data", "3A 25", 0),
        ("cmd", "36", 0), ("data", "48", 0),
        ("cmd", "3A", 0), ("data", "55", 0),
        ("cmd", "B1", 0), ("data", "00 18", 0),
        ("cmd", "F2", 0), ("data", "00", 0),
        ("cmd", "3F", 0), ("data", "00", 0),
        ("cmd", "2E", 0), ("data", "33", 0),
        ("cmd", "E0", 0), ("data", "02 04 00 07 07 38 32 29 2B 29 25 26 00", 0),
        ("cmd", "E1", 0), ("data", "00 04 00 07 05 10 0F 12 27 2B 29 26 25", 0),
        ("cmd", "11", 0), ("delay", "", 120),
        ("cmd", "29", 0), ("delay", "", 50),
    ],
}


def lcd_template_steps(controller: str) -> List[LcdInitStep]:
    key = str(controller).lower()
    if key not in LCD_TEMPLATES:
        raise ValueError(f"无内置模板：{controller}")
    return _steps(LCD_TEMPLATES[key])


def rgb888_to_rgb565(pixels: bytes, swap: bool = False) -> bytes:
    """RGB888 字节流 → RGB565 大端（2 字节/像素）。pixels 长度须为 3 的倍数。"""
    px = bytes(pixels)
    if len(px) % 3:
        raise ValueError("RGB888 数据长度须为 3 的倍数")
    out = bytearray(len(px) // 3 * 2)
    o = 0
    for i in range(0, len(px), 3):
        r, g, b = px[i], px[i + 1], px[i + 2]
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        hi, lo = v >> 8, v & 0xFF
        if swap:
            out[o], out[o + 1] = lo, hi
        else:
            out[o], out[o + 1] = hi, lo
        o += 2
    return bytes(out)


# ── DLL 绑定 ──────────────────────────────────────────────────────────

_U = ctypes.c_uint32
_H = ctypes.c_uint16
_B = ctypes.c_ubyte
_PV = ctypes.c_void_p
_WP = ctypes.c_void_p          # PVOID：直接传 create_string_buffer 的地址


# 驱动解析结果只记一次（dll_info 被 UI 快照/MCP 反复调用，避免刷屏）
_dll_logged = False


def load_ch347_dll():
    """加载 CH347DLL；缺失返回 None（不抛异常）。

    优先顺序：环境变量 CH347DLL 指定路径 → app/libs/ch347/ 副本 →
    系统目录（安装官方驱动后 CH347DLLA64.DLL 已在 System32）。
    """
    global _dll_logged
    dll = load_dll(DLL_NAME, DLL_ENV)
    if dll is not None:
        # load_dll 内部已记录来源路径，这里不重复
        _dll_logged = True
        return dll
    for name in _SYSTEM_DLL_NAMES:
        try:
            dll = ctypes.WinDLL(name)
        except OSError:
            continue
        if not _dll_logged:
            _dll_logged = True
            log.info("CH347 驱动：从系统目录加载 %s（官方驱动）", name)
        return dll
    if not _dll_logged:
        _dll_logged = True
        log.warning(
            "CH347 驱动未找到：环境变量 %s / %s / 系统目录均无 %s，"
            "CH347 功能不可用（需安装 WCH 官方驱动）",
            DLL_ENV, LIBS_DIR / "ch347", " 或 ".join(_SYSTEM_DLL_NAMES))
    return None


def dll_info() -> str:
    """DLL 状态描述（供 UI/MCP 快照展示）。"""
    dll = load_ch347_dll()
    if dll is None:
        return ("未检测到 CH347DLL：请先安装 WCH 官方 CH347 驱动"
                "（安装后 CH347DLLA64.DLL 自动位于系统目录，无需放入本软件）；"
                f"也可用环境变量 {DLL_ENV} 或 {LIBS_DIR / 'ch347'} 目录"
                f"内的 {DLL_NAME} 指定/覆盖")
    return f"{DLL_NAME} 已加载"


class _Api:
    """按需绑定导出函数；32 位 stdcall 修饰名自动回退。"""

    def __init__(self, dll):
        self._dll = dll
        self._fns = {}

    def __getattr__(self, name):
        try:
            return self._fns[name]
        except KeyError:
            pass
        spec = _API_SPECS[name]
        restype, argtypes = spec
        fn = None
        try:
            fn = getattr(self._dll, name)
        except AttributeError:
            total = sum(ctypes.sizeof(t) for t in argtypes)
            try:
                fn = getattr(self._dll, f"{name}@{total}")
            except AttributeError:
                raise Ch347Error(f"DLL 缺少导出函数 {name}") from None
        fn.restype = restype
        fn.argtypes = argtypes
        self._fns[name] = fn
        return fn


_BOOL = ctypes.c_int          # Win32 BOOL

_API_SPECS = {
    "CH347OpenDevice": (_WP, [_U]),
    "CH347CloseDevice": (_BOOL, [_U]),
    "CH347GetDeviceInfor": (_BOOL, [_U, ctypes.POINTER(DEVICE_INFOR)]),
    "CH347GetChipType": (_B, [_U]),
    "CH347SetTimeout": (_BOOL, [_U, _U, _U]),
    "CH347GetSerialNumber": (_BOOL, [_U, ctypes.c_char_p]),
    "CH347SPI_Init": (_BOOL, [_U, ctypes.POINTER(SPI_CONFIG)]),
    "CH347SPI_GetCfg": (_BOOL, [_U, ctypes.POINTER(SPI_CONFIG)]),
    "CH347SPI_SetFrequency": (_BOOL, [_U, _U]),
    "CH347SPI_SetDataBits": (_BOOL, [_U, _B]),
    "CH347SPI_SetChipSelect": (_BOOL, [_U, _H, _H, _U, _U, _U]),
    "CH347SPI_Write": (_BOOL, [_U, _U, _U, _U, _PV]),
    "CH347SPI_Read": (_BOOL, [_U, _U, _U, ctypes.POINTER(_U), _PV]),
    "CH347SPI_WriteRead": (_BOOL, [_U, _U, _U, _PV]),
    "CH347StreamSPI4": (_BOOL, [_U, _U, _U, _PV]),
    "CH347I2C_Set": (_BOOL, [_U, _U]),
    "CH347I2C_SetStretch": (_BOOL, [_U, ctypes.c_int]),
    "CH347I2C_SetDelaymS": (_BOOL, [_U, _U]),
    "CH347StreamI2C": (_BOOL, [_U, _U, _PV, _U, _PV]),
    "CH347StreamI2C_RetACK": (_BOOL, [_U, _U, _PV, _U, _PV,
                                      ctypes.POINTER(_U)]),
    "CH347GPIO_Get": (_BOOL, [_U, ctypes.POINTER(_B), ctypes.POINTER(_B)]),
    "CH347GPIO_Set": (_BOOL, [_U, _B, _B, _B]),
    "CH347ReadEEPROM": (_BOOL, [_U, ctypes.c_int, _U, _U,
                                ctypes.POINTER(_B)]),
    "CH347WriteEEPROM": (_BOOL, [_U, ctypes.c_int, _U, _U,
                                 ctypes.POINTER(_B)]),
}

_api_obj = None
_api_dll_id = None


def _api() -> _Api:
    """返回已绑定 API 表；DLL 缺失抛 Ch347Error。"""
    global _api_obj, _api_dll_id
    dll = load_ch347_dll()
    if dll is None:
        raise Ch347Error(dll_info())
    if _api_obj is None or _api_dll_id != id(dll):
        _api_obj = _Api(dll)
        _api_dll_id = id(dll)
    return _api_obj


def _buf(data=b"", extra: int = 0):
    """create_string_buffer 包装：len(data)+extra。"""
    size = len(bytes(data)) + extra
    if size == 0:
        size = 1
    return ctypes.create_string_buffer(bytes(data), size)


def _check(ok, what: str):
    if not ok:
        raise Ch347Error(f"{what} 失败（请检查设备连接/模式与驱动）")


# ── 设备枚举与句柄封装（仅 worker 线程使用）───────────────────────────

# 本进程已通过 Ch347Device 打开的索引集合；scan_devices 会跳过它们，
# 避免对同一设备重复 Open/Close 破坏驱动状态（严重时可蓝屏）。
_open_indexes = set()


def _infor_to_dict(inf: DEVICE_INFOR) -> dict:
    def s(raw):
        return bytes(raw or b"").split(b"\x00", 1)[0].decode(
            "gbk", "replace")
    return {
        "index": int(inf.iIndex),
        "chip_mode": int(inf.ChipMode),
        "usb_class": int(inf.UsbClass),
        "func_type": int(inf.FuncType),
        "device_id": s(inf.DeviceID),
        "product": s(inf.ProductString),
        "manufacturer": s(inf.ManufacturerString),
        "func_desc": s(inf.FuncDescStr),
        "fw_ver": int(inf.FirewareVer),
        "hs": bool(inf.UsbSpeedType),
    }


def scan_devices() -> List[dict]:
    """索引 0~15 逐个试探：Open→GetDeviceInfor→Close。

    过滤 ChipMode==3（Mode3 接口为 JTAG+I2C，无 SPI/GPIO）的设备。

    已被本进程 Ch347Device 占用的索引会被跳过：对同一句柄重复
    Open/Close 会破坏 CH347 驱动内部状态（底层为内核态驱动，
    异常时可导致蓝屏），因此枚举绝不触碰正在使用的设备。
    """
    api = _api()
    out = []
    inf = DEVICE_INFOR()
    for i in range(16):
        if i in _open_indexes:
            continue
        h = api.CH347OpenDevice(i)
        if h in _INVALID_HANDLES:
            continue
        try:
            # argtypes 为 POINTER(DEVICE_INFOR)，直接传结构体实例即可，
            # 也便于测试注入 fake api
            if api.CH347GetDeviceInfor(i, inf):
                d = _infor_to_dict(inf)
                if d["chip_mode"] == 3:
                    continue
                d["chip_type"] = int(api.CH347GetChipType(i))
                d["chip_type_name"] = CHIP_TYPE_NAMES.get(
                    d["chip_type"], f"0x{d['chip_type']:02X}")
                out.append(d)
        finally:
            api.CH347CloseDevice(i)
    return out


class Ch347Device:
    """按索引独占一台 CH347；所有方法只允许在 worker 线程内调用。"""

    def __init__(self, index: int, fast_timeout_ms: int = 500):
        self.index = int(index)
        if self.index in _open_indexes:
            raise Ch347Error(
                f"设备 {self.index}# 已在本程序内打开，请勿重复打开")
        self._api = _api()
        h = self._api.CH347OpenDevice(self.index)
        if h in _INVALID_HANDLES:
            raise Ch347Error(f"打开设备 {self.index}# 失败："
                             "请确认已插入 CH347 且未被其他程序占用")
        self._opened = True
        _open_indexes.add(self.index)
        try:
            self._api.CH347SetTimeout(self.index, fast_timeout_ms,
                                      fast_timeout_ms)
        except Ch347Error:
            pass
        # GPIO 影子状态：enable/dir/data 位掩码，供 gpio_write_pin 保持其余脚
        self._gpio_enable = 0
        self._gpio_dir = 0
        self._gpio_data = 0
        self._cs = 0x80        # SPI 片选参数（bit7 使能 + CS1）

    def close(self):
        if self._opened:
            self._opened = False
            _open_indexes.discard(self.index)
            try:
                self._api.CH347CloseDevice(self.index)
            except Exception:      # noqa: BLE001 - 关闭失败无补救
                pass

    def __del__(self):                    # 兜底：防止异常路径泄漏设备
        try:
            self.close()
        except Exception:                 # noqa: BLE001
            pass

    # ── 信息 ─────────────────────────────────────────────────────

    def info(self) -> dict:
        inf = DEVICE_INFOR()
        _check(self._api.CH347GetDeviceInfor(self.index, inf),
               "读取设备信息")
        d = _infor_to_dict(inf)
        d["chip_type"] = int(self._api.CH347GetChipType(self.index))
        d["chip_type_name"] = CHIP_TYPE_NAMES.get(
            d["chip_type"], f"0x{d['chip_type']:02X}")
        return d

    # ── SPI ──────────────────────────────────────────────────────

    def spi_init(self, mode: int = 0, clk: int = 0, msb_first: bool = True,
                 cs_index: int = 0, cs1_polarity: int = 0,
                 cs2_polarity: int = 0, cs_enable: bool = True,
                 auto_deactive_cs: bool = True, active_delay_us: int = 0,
                 delay_deactive_us: int = 0, interval_us: int = 0,
                 out_default: int = 0xFF, data_bits: int = 0,
                 frequency_hz: int = 0) -> None:
        """初始化 SPI 接口；frequency_hz>0 时用精确频率替代 clk 档。"""
        cfg = SPI_CONFIG()
        cfg.iMode = mode & 3
        cfg.iClock = clk & 7
        cfg.iByteOrder = 1 if msb_first else 0
        cfg.iSpiWriteReadInterval = interval_us & 0xFFFF
        cfg.iSpiOutDefaultData = out_default & 0xFF
        cs = (cs_index & 3) | (0x80 if cs_enable else 0)
        cfg.iChipSelect = cs
        cfg.CS1Polarity = cs1_polarity & 1
        cfg.CS2Polarity = cs2_polarity & 1
        cfg.iIsAutoDeativeCS = 1 if auto_deactive_cs else 0
        cfg.iActiveDelay = active_delay_us & 0xFFFF
        cfg.iDelayDeactive = delay_deactive_us & 0xFFFFFFFF
        self._cs = cs
        if frequency_hz:
            self._api.CH347SPI_SetFrequency(self.index,
                                            int(frequency_hz) & 0xFFFFFFFF)
        else:
            self._api.CH347SPI_SetFrequency(self.index, 0)
        self._api.CH347SPI_SetDataBits(self.index, data_bits & 1)
        _check(self._api.CH347SPI_Init(self.index, ctypes.byref(cfg)),
               "SPI 初始化")

    def spi_xfer(self, tx, read_len: int = 0) -> bytes:
        """全双工流：发送 tx，总交换长度 len(tx)+read_len，返回尾部 read_len。

        超过 4096 字节自动分块（CS 保持由 Init 的 auto_deactive 决定）。
        """
        tx = bytes(tx or b"")
        total = len(tx) + int(read_len)
        if total <= 0:
            return b""
        out = bytearray()
        pos = 0
        remain_total = total
        while remain_total > 0:
            n = min(MAX_STREAM, remain_total)
            buf = bytearray(n)
            # 段内对应要发送的部分
            take = min(n, max(0, len(tx) - pos))
            buf[:take] = tx[pos:pos + take]
            _check(self._api.CH347StreamSPI4(self.index, self._cs, n,
                                             _buf(buf)),
                   "SPI 流传输")
            out += buf
            pos += take
            remain_total -= n
        if read_len:
            return bytes(out[len(tx):len(tx) + int(read_len)])
        return b""

    # ── I2C ──────────────────────────────────────────────────────

    def i2c_init(self, speed: int = 1, stretch: bool = False,
                 delay_ms: int = 0) -> None:
        _check(self._api.CH347I2C_Set(self.index, speed & 7), "I2C 初始化")
        self._api.CH347I2C_SetStretch(self.index, 1 if stretch else 0)
        if delay_ms:
            self._api.CH347I2C_SetDelaymS(self.index, int(delay_ms))
        self._i2c_ready = True
        self._i2c_speed = speed & 7

    def ensure_i2c_ready(self, speed: int = 2) -> None:
        """确保 I2C 接口已初始化（未初始化或速率变化时按 speed 初始化）。

        CH347 的 I2C 必须先 CH347I2C_Set 才能收发，否则后续
        CH347StreamI2C_RetACK 直接返回失败。此前只有「I2C」标签页的手动
        初始化按钮会调用它，「I2C 器件」页扫描/读写前从不初始化，于是
        每次探测都在未初始化状态立即失败——这会让 117 个地址瞬间“扫完”
        且一个器件都找不到。这里按需自动补初始化，默认 400KHz。
        """
        speed &= 7
        if (not getattr(self, "_i2c_ready", False)
                or getattr(self, "_i2c_speed", None) != speed):
            self.i2c_init(speed=speed)

    def i2c_xfer(self, write, read_len: int = 0):
        """write 首字节 = 8bit 设备地址（addr<<1 | R/W）。返回 (data, ack)。

        ack 为写阶段未收到 ACK 的字节数（0=全部应答）。
        """
        w = bytes(write or b"")
        rl = max(0, int(read_len))
        if len(w) > MAX_STREAM or rl > MAX_STREAM:
            raise Ch347Error(f"I2C 单次长度上限 {MAX_STREAM} 字节")
        iwb = ctypes.create_string_buffer(w, max(len(w), 1))
        orb = ctypes.create_string_buffer(max(rl, 1))
        ack = _U(0)
        _check(self._api.CH347StreamI2C_RetACK(
            self.index, len(w), iwb, rl, orb, ctypes.byref(ack)),
            "I2C 传输")
        return bytes(orb.raw[:rl]), int(ack.value)

    def i2c_scan(self, progress=None) -> List[int]:
        """逐地址扫描 0x03~0x77，返回 ACK 的 7bit 地址列表。

        真正的扫描必须一个一个地址发探测，每次都有 I2C 总线事务开销
        （未应答的地址要等超时），117 个地址不可能瞬间返回。若整轮全部
        探测都失败（不是“没器件”，而是接口/接线/供电异常），抛 Ch347Error
        说明原因，避免把硬件故障显示成“扫描完成但无器件”。

        progress: 可选回调 (done, total)，供 UI 显示进度。
        """
        self.ensure_i2c_ready()
        addrs = list(range(0x03, 0x78))
        total = len(addrs)
        found = []
        errors = 0
        last_err = None
        for i, a in enumerate(addrs):
            try:
                _, ack = self.i2c_xfer(bytes([a << 1]), 0)
                if ack == 0:
                    found.append(a)
            except Ch347Error as e:
                # 单个地址探测失败（无应答等）属正常，继续下一个；
                # 但若整轮全失败，说明是接口级故障，下面统一报错。
                errors += 1
                last_err = e
            if progress:
                progress(i + 1, total)
        if not found and errors == total:
            raise Ch347Error(
                "I2C 扫描失败：全部地址均无响应。请检查器件上电、SDA/SCL 接线"
                f"与上拉电阻（底层错误：{last_err}）")
        return found

    # ── GPIO ─────────────────────────────────────────────────────

    def gpio_get(self):
        d, p = _B(0), _B(0)
        _check(self._api.CH347GPIO_Get(self.index,
                                       ctypes.byref(d), ctypes.byref(p)),
               "GPIO 读取")
        return int(d.value), int(p.value)

    def gpio_set(self, enable: int, set_dir_out: int, data_out: int) -> None:
        self._api.CH347GPIO_Set(self.index, enable & 0xFF,
                                set_dir_out & 0xFF, data_out & 0xFF)
        self._gpio_enable = enable & 0xFF
        self._gpio_dir = set_dir_out & 0xFF
        self._gpio_data = data_out & 0xFF

    def gpio_write_pin(self, pin: int, level: int) -> None:
        """把单个引脚设为输出并输出电平，保持其余引脚状态不变。"""
        pin = int(pin) & 7
        bit = 1 << pin
        self.gpio_set(self._gpio_enable | bit,
                      self._gpio_dir | bit,
                      (self._gpio_data | bit) if level
                      else (self._gpio_data & ~bit))

    # ── SPI Flash ────────────────────────────────────────────────

    def _flash_cmd(self, tx) -> bytes:
        n = len(bytes(tx))
        buf = bytearray(bytes(tx)) + bytearray(1)
        _check(self._api.CH347SPI_WriteRead(self.index, self._cs, n, _buf(buf)),
               "Flash SPI 传输")
        return bytes(buf)

    def flash_status(self) -> int:
        r = self._flash_cmd([CMD_FLASH_RDSR, 0])
        return r[1]

    def flash_wait_busy(self, timeout_ms: int = 60000) -> None:
        import time
        deadline = time.monotonic() + timeout_ms / 1000.0
        while self.flash_status() & 0x01:
            if time.monotonic() > deadline:
                raise Ch347Error("Flash 忙等待超时（芯片未响应或写入未完成）")
            time.sleep(0.005)

    def _flash_wren(self) -> None:
        self._flash_cmd([CMD_FLASH_WREN])

    def flash_identify(self) -> dict:
        r = self._flash_cmd([CMD_FLASH_JEDEC_ID, 0, 0, 0])
        return jedec_to_info(r[1:4])

    def flash_read(self, addr: int, length: int, fast: bool = False,
                   progress=None) -> bytearray:
        """标准读 0x03 / 快读 0x0B（1 字节 dummy），分块 ≤ 4092B/次。"""
        length = int(length)
        if length <= 0:
            raise Ch347Error("读取长度必须大于 0")
        cmd = CMD_FLASH_H_READ if fast else CMD_FLASH_READ
        dummy = 1 if fast else 0
        out = bytearray()
        pos = 0
        while pos < length:
            n = min(MAX_STREAM - 4 - dummy, length - pos)
            addr_i = int(addr) + pos
            frame = bytearray([cmd,
                               (addr_i >> 16) & 0xFF, (addr_i >> 8) & 0xFF,
                               addr_i & 0xFF])
            if fast:
                frame.append(0xFF)
            frame += bytearray(n)
            _check(self._api.CH347SPI_WriteRead(
                self.index, self._cs, len(frame), _buf(frame)),
                "Flash 读取")
            out += frame[len(frame) - n:]
            pos += n
            if progress:
                progress(len(out), length)
        return out

    def flash_erase(self, addr: int, length: int, granularity: int,
                    progress=None) -> int:
        """按粒度擦除覆盖 [addr, addr+length) 的全部块，返回擦除块数。"""
        if granularity == 0:          # 0 = 全片擦
            self._flash_wren()
            self._flash_cmd([CMD_FLASH_CHIP_ERASE])
            self.flash_wait_busy(120000)
            return 1
        cmd = {4096: CMD_FLASH_SECTOR_ERASE,
               32768: CMD_FLASH_BLOCK_ERASE_32K,
               65536: CMD_FLASH_BLOCK_ERASE_64K}.get(granularity)
        if cmd is None:
            raise Ch347Error(f"不支持的擦除粒度 {granularity}")
        addrs = flash_erase_addrs(addr, length, granularity)
        for i, a in enumerate(addrs):
            self._flash_wren()
            self._flash_cmd([cmd, (a >> 16) & 0xFF, (a >> 8) & 0xFF,
                             a & 0xFF])
            self.flash_wait_busy(5000)
            if progress:
                progress(i + 1, len(addrs))
        return len(addrs)

    def flash_write(self, addr: int, data, progress=None) -> int:
        """先擦后写（4K 扇区粒度，与官方 Demo 一致），按 256B 页编程。

        返回写入字节数；进度回调 (done, total=len(data))。
        """
        data = bytes(data)
        if not data:
            raise Ch347Error("写入数据为空")
        self.flash_erase(addr, len(data), 4096)
        total = len(data)
        done = 0
        for page_addr, chunk in flash_page_split(addr, data):
            self._flash_wren()
            frame = bytearray([CMD_FLASH_BYTE_PROG,
                               (page_addr >> 16) & 0xFF,
                               (page_addr >> 8) & 0xFF,
                               page_addr & 0xFF]) + bytearray(chunk)
            _check(self._api.CH347SPI_Write(self.index, self._cs,
                                            len(frame), 256 + 4,
                                            _buf(frame)),
                   "Flash 页编程")
            self.flash_wait_busy(2000)
            done += len(chunk)
            if progress:
                progress(done, total)
        return done

    def flash_blank_check(self, addr: int, length: int,
                          progress=None):
        """空白校验：返回 (True, None) 或 (False, 首个非 0xFF 地址)。"""
        pos = 0
        while pos < length:
            n = min(4096, length - pos)
            chunk = self.flash_read(int(addr) + pos, n)
            idx = next((i for i, b in enumerate(chunk) if b != 0xFF), None)
            if idx is not None:
                return False, int(addr) + pos + idx
            pos += n
            if progress:
                progress(pos, length)
        return True, None

    # ── EEPROM（I2C 24Cxx，DLL 内置驱动）─────────────────────────

    def _eeprom_enum(self, model: str) -> int:
        for m in EEPROM_MODELS:
            if m["name"] == str(model):
                return m["enum"]
        raise Ch347Error(f"未知 EEPROM 型号：{model}")

    def eeprom_read(self, model: str, addr: int, length: int) -> bytes:
        eid = self._eeprom_enum(model)
        length = int(length)
        out = bytearray()
        pos = 0
        while pos < length:
            n = min(256, length - pos)
            buf = (_B * n)()
            _check(self._api.CH347ReadEEPROM(
                self.index, eid, int(addr) + pos, n, buf), "EEPROM 读取")
            out += bytes(buf)
            pos += n
        return bytes(out)

    def eeprom_write(self, model: str, addr: int, data) -> int:
        eid = self._eeprom_enum(model)
        data = bytes(data)
        if not data:
            raise Ch347Error("EEPROM 写入数据为空")
        pos = 0
        while pos < len(data):
            n = min(256, len(data) - pos)
            chunk = data[pos:pos + n]
            buf = (_B * n).from_buffer_copy(chunk)
            _check(self._api.CH347WriteEEPROM(
                self.index, eid, int(addr) + pos, n, buf), "EEPROM 写入")
            pos += n
        return len(data)

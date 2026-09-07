# coding: utf-8
"""应用配置：qconfig 持久化（主题/接收上限/日志目录）+ data.json（发送历史/预设命令）"""
import json
import sys
import threading
import uuid
from pathlib import Path

from qfluentwidgets import QConfig, Theme, qconfig
from qfluentwidgets.common.config import (
    BoolValidator,
    ConfigItem,
    EnumSerializer,
    OptionsConfigItem,
    OptionsValidator,
    RangeConfigItem,
    RangeValidator,
)

if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = APP_DIR / "config.json"
DATA_FILE = APP_DIR / "data.json"

# data.json 的读改写锁：UI 线程（发送历史/预设/SSH 会话）与 worker
# 线程（SSH 主机密钥指纹）会并发写入，不加锁会互相覆盖丢数据。
_DATA_LOCK = threading.Lock()


class Config(QConfig):
    """qconfig 管理的配置项（写入 config.json）"""

    themeMode = OptionsConfigItem(
        "MainWindow",
        "ThemeMode",
        default=Theme.AUTO,
        validator=OptionsValidator([Theme.LIGHT, Theme.DARK, Theme.AUTO]),
        serializer=EnumSerializer(Theme),
        restart=False,
    )
    maxChars = RangeConfigItem(
        "Receive",
        "MaxChars",
        default=200_000,
        validator=RangeValidator(10_000, 1_000_000),
    )
    logDir = ConfigItem("Serial", "LogDir", default="")

    # ADB
    adbPath = ConfigItem("ADB", "AdbPath", default="adb")
    defaultModel = ConfigItem("ADB", "DefaultModel", default="")

    # PhoenixConsole 烧录工具
    phoenixPath = ConfigItem("Phoenix", "ExePath", default="")

    # MCP 服务（内嵌，重启后生效）
    mcpEnabled = OptionsConfigItem(
        "MCP", "Enabled", default=False, validator=BoolValidator())
    mcpPort = RangeConfigItem(
        "MCP", "Port", default=8642, validator=RangeValidator(1024, 65535))
    mcpToken = ConfigItem("MCP", "Token", default="")

    # MCP 安全策略：高危能力默认不开放，关闭时对应工具根本不注册
    # （AI 客户端 tools/list 里看不到），同样重启后生效。
    mcpAllowExec = OptionsConfigItem(
        "MCP", "AllowExec", default=False, validator=BoolValidator())
    mcpAllowFile = OptionsConfigItem(
        "MCP", "AllowFile", default=False, validator=BoolValidator())

    # TCP/IP 网络调试页外观（接收区/发送区字号与颜色）
    rxFontSize = RangeConfigItem(
        "Tcpip", "RxFontSize", default=10, validator=RangeValidator(8, 32))
    rxTextColor = ConfigItem("Tcpip", "RxTextColor", default="#DCDCDC")
    rxBgColor = ConfigItem("Tcpip", "RxBgColor", default="#1E1E1E")
    txFontSize = RangeConfigItem(
        "Tcpip", "TxFontSize", default=10, validator=RangeValidator(8, 32))
    txTextColor = ConfigItem("Tcpip", "TxTextColor", default="#DCDCDC")
    txBgColor = ConfigItem("Tcpip", "TxBgColor", default="#1E1E1E")


cfg = Config()


def loadConfig() -> None:
    qconfig.load(str(CONFIG_FILE), cfg)
    _ensureMcpToken()


def _ensureMcpToken() -> None:
    """首次启动时自动生成 MCP 密钥；已有密钥不覆盖/删除。"""
    if qconfig.get(cfg.mcpToken):
        return
    qconfig.set(cfg.mcpToken, uuid.uuid4().hex[:16])


def saveConfig() -> None:
    qconfig.save()


# ---------------------------------------------------------------------------
# data.json：结构化数据（发送历史、预设命令），不走 qconfig
# ---------------------------------------------------------------------------

def loadData() -> dict:
    with _DATA_LOCK:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {"sendHistory": [], "presets": []}


def saveData(data: dict) -> None:
    with _DATA_LOCK:
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

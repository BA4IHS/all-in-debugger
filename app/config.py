# coding: utf-8
"""应用配置：qconfig 持久化（主题/接收上限/日志目录）+ data.json（发送历史/预设命令）"""
import json
from pathlib import Path

from qfluentwidgets import QConfig, Theme, qconfig
from qfluentwidgets.common.config import (
    ConfigItem,
    EnumSerializer,
    OptionsConfigItem,
    OptionsValidator,
    RangeConfigItem,
    RangeValidator,
)

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = APP_DIR / "config.json"
DATA_FILE = APP_DIR / "data.json"


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


cfg = Config()


def loadConfig() -> None:
    qconfig.load(str(CONFIG_FILE), cfg)


def saveConfig() -> None:
    qconfig.save()


# ---------------------------------------------------------------------------
# data.json：结构化数据（发送历史、预设命令），不走 qconfig
# ---------------------------------------------------------------------------

def loadData() -> dict:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"sendHistory": [], "presets": []}


def saveData(data: dict) -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

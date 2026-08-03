# coding: utf-8
"""设置页：本版本 qfluentwidgets 无 SettingInterface，
用 ScrollArea + SettingCardGroup + SettingCard 族手工搭建。
"""
import json
import uuid

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QClipboard
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    ComboBox, ComboBoxSettingCard, ExpandLayout, FluentIcon, PushSettingCard,
    RangeSettingCard, ScrollArea, SettingCard, SettingCardGroup, SpinBox,
    SwitchSettingCard, ToolButton, setTheme,
)

from app import adb_runner as ar
from app.config import cfg, qconfig


class _AdbModelCard(SettingCard):
    """设置里的"默认设备型号"选择卡：下拉来源 adb_profiles 目录。"""
    modelChanged = pyqtSignal(str)   # 发出文件名 stem

    def __init__(self, parent=None):
        super().__init__(FluentIcon.LIBRARY, "默认设备型号",
                         "ADB 页打开时预选的型号（命令集）", parent)
        self.combo = ComboBox(self)
        self.combo.setFixedWidth(180)
        self.refreshBtn = ToolButton(FluentIcon.UPDATE, self)
        self.refreshBtn.setToolTip("重载型号文件")
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.combo, 0, Qt.AlignmentFlag.AlignVCenter)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.refreshBtn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.hBoxLayout.addSpacing(16)
        self.combo.currentTextChanged.connect(self._on_sel)
        self.refreshBtn.clicked.connect(lambda _=False: self.reload())
        self._stem_by_label = {}
        self.reload()

    def reload(self):
        items = ar.list_profiles()
        from collections import Counter
        cnt = Counter(m for _, m, _ in items)
        cur_stem = self._stem_by_label.get(self.combo.currentText(), None)
        self._stem_by_label = {}
        labels = []
        for stem, model, _d in items:
            label = f"{model}  [{stem}]" if cnt[model] > 1 else model
            labels.append(label)
            self._stem_by_label[label] = stem
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(labels)
        # 恢复：优先配置里的默认型号，其次原选中
        target_label = next((l for l, s in self._stem_by_label.items()
                             if s == (cfg_default() or cur_stem)), None)
        if target_label:
            self.combo.setCurrentText(target_label)
        self.combo.blockSignals(False)

    def _on_sel(self, label: str):
        stem = self._stem_by_label.get(label, "")
        qconfig.set(cfg.defaultModel, stem)
        self.modelChanged.emit(stem)


def cfg_default() -> str:
    return qconfig.get(cfg.defaultModel)


class SettingPage(ScrollArea):
    maxCharsChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._adbVersionProbe = ar.AdbProbe(self)
        self._adbVersionProbe.finished.connect(self._onAdbVersionReady)
        self._adbVersionPath = ""
        self._shuttingDown = False
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._container = QWidget(self)
        self.setWidget(self._container)
        # 官方透明化：透出窗口主题底色，修复深色模式下白字白底
        self.enableTransparentBackground()
        layout = QVBoxLayout(self._container)
        layout.setContentsMargins(24, 40, 24, 24)  # 顶部留白避开悬浮标题栏
        layout.setSpacing(16)
        self._expand = ExpandLayout()
        layout.addLayout(self._expand)
        layout.addStretch(1)

        self._buildAppearance()
        self._buildReceive()
        self._buildLog()
        self._buildAdb()
        self._buildMcp()

        self.setObjectName("settingInterface")
        self._container.setObjectName("settingContainer")

    def _buildAppearance(self):
        group = SettingCardGroup("外观", self._container)
        self.themeCard = ComboBoxSettingCard(
            cfg.themeMode, FluentIcon.PALETTE, "应用主题",
            "切换浅色 / 深色 / 跟随系统",
            texts=["浅色", "深色", "跟随系统"], parent=group)
        # ComboBoxSettingCard 内部已写回 qconfig，这里负责即时应用
        self.themeCard.comboBox.currentIndexChanged.connect(
            lambda _: setTheme(qconfig.get(cfg.themeMode)))
        group.addSettingCard(self.themeCard)
        self._expand.addWidget(group)

    def _buildReceive(self):
        group = SettingCardGroup("接收", self._container)
        self.maxCharsCard = RangeSettingCard(
            cfg.maxChars, FluentIcon.UPDATE, "接收区容量上限",
            "超出后自动截断最早的内容", parent=group)
        self.maxCharsCard.valueChanged.connect(self.maxCharsChanged.emit)
        group.addSettingCard(self.maxCharsCard)
        self._expand.addWidget(group)

    def _buildLog(self):
        group = SettingCardGroup("日志", self._container)
        current = qconfig.get(cfg.logDir) or "（默认：程序目录 logs/）"
        self.logCard = PushSettingCard(
            "选择文件夹", FluentIcon.FOLDER, "日志目录",
            current, parent=group)
        self.logCard.clicked.connect(self._chooseLogDir)
        group.addSettingCard(self.logCard)
        self._expand.addWidget(group)

    def _buildAdb(self):
        group = SettingCardGroup("ADB", self._container)
        path0, _ = ar.find_adb(qconfig.get(cfg.adbPath))
        shown = path0 or (qconfig.get(cfg.adbPath) or "adb")
        status = "正在检测…" if path0 else "未检测到，点浏览选择"
        content = f"{shown}  |  {status}"
        self.adbCard = PushSettingCard(
            "浏览…", FluentIcon.FOLDER, "adb 可执行文件", content, parent=group)
        self.adbCard.clicked.connect(self._chooseAdb)
        group.addSettingCard(self.adbCard)

        self.modelCard = _AdbModelCard(group)
        group.addSettingCard(self.modelCard)
        self._expand.addWidget(group)
        if path0:
            QTimer.singleShot(0, lambda path=path0: self._probeAdbVersion(path))

    def _chooseAdb(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 adb 可执行文件", "", "adb (adb.exe adb);;All Files (*)")
        if not path:
            return
        qconfig.set(cfg.adbPath, path)
        self.adbCard.setContent(f"{path}  |  正在检测…")
        self._probeAdbVersion(path)

    def _probeAdbVersion(self, path: str):
        if self._shuttingDown:
            return
        self._adbVersionPath = path
        self._adbVersionProbe.start(path, ["version"], timeout_ms=6000)

    def _onAdbVersionReady(self, data: bytes, code: int, error: str):
        text = data.decode("utf-8", "replace").strip()
        version = ar.adb_version_line(text)
        version_tuple = ar.parse_adb_version(text)
        if code != 0 and not error:
            error = text.splitlines()[0] if text else f"退出码 {code}"
        if error:
            shown = f"检测失败：{error}"
        elif ar.is_legacy_adb_version(version_tuple):
            shown = f"{version or '版本未知'}（过旧，建议 1.0.40+）"
        else:
            shown = version or "版本未知"
        self.adbCard.setContent(f"{self._adbVersionPath}  |  {shown}")

    def _buildMcp(self):
        group = SettingCardGroup("MCP 服务", self._container)
        self.mcpSwitchCard = SwitchSettingCard(
            FluentIcon.ROBOT, "启用 MCP 服务",
            "向 AI 客户端暴露本调试器的收发/读写能力（重启后生效）",
            configItem=cfg.mcpEnabled, parent=group)
        group.addSettingCard(self.mcpSwitchCard)

        self.mcpPortCard = SettingCard(
            FluentIcon.LIBRARY, "服务端口",
            "仅监听 127.0.0.1（重启后生效）", parent=group)
        self.mcpPortBox = SpinBox(self.mcpPortCard)
        self.mcpPortBox.setRange(1024, 65535)
        self.mcpPortBox.setValue(int(qconfig.get(cfg.mcpPort)))
        self.mcpPortCard.hBoxLayout.addWidget(
            self.mcpPortBox, 0, Qt.AlignmentFlag.AlignRight)
        self.mcpPortCard.hBoxLayout.addSpacing(16)
        self.mcpPortBox.valueChanged.connect(
            lambda v: qconfig.set(cfg.mcpPort, int(v)))
        group.addSettingCard(self.mcpPortCard)

        token = qconfig.get(cfg.mcpToken) or ""
        if not token:
            token = uuid.uuid4().hex[:16]
            qconfig.set(cfg.mcpToken, token)
        self.mcpCopyCard = PushSettingCard(
            "复制接入配置", FluentIcon.SHARE, "AI 客户端接入",
            f"http://127.0.0.1:{qconfig.get(cfg.mcpPort)}/mcp（Bearer 密钥已生成）",
            parent=group)
        self.mcpCopyCard.clicked.connect(self._copyMcpConfig)
        group.addSettingCard(self.mcpCopyCard)
        self._expand.addWidget(group)

    def _copyMcpConfig(self):
        snippet = {
            "mcpServers": {
                "serial-debugger": {
                    "url": f"http://127.0.0.1:{qconfig.get(cfg.mcpPort)}/mcp",
                    "headers": {
                        "Authorization":
                            f"Bearer {qconfig.get(cfg.mcpToken)}",
                    },
                }
            }
        }
        QApplication.clipboard().setText(
            json.dumps(snippet, ensure_ascii=False, indent=2),
            QClipboard.Mode.Clipboard)

    def _chooseLogDir(self):
        path = QFileDialog.getExistingDirectory(self, "选择日志目录")
        if not path:
            return
        qconfig.set(cfg.logDir, path)
        self.logCard.contentLabel.setText(path)

    def shutdown(self):
        self._shuttingDown = True
        self._adbVersionProbe.shutdown()

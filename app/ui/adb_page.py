# coding: utf-8
"""ADB 调试页：左连接/采集配置 + 右(终端 + 命令面板)。

- 终端复用 pyte QTerminalWidget；交互 shell 与采集输出共用此终端
- 交互 shell = `adb -s <serial> shell -t`；采集 = `adb -s <serial> shell <cmd>`
- 型号(=命令集) 与 serial(=连接目标) 分开选择
"""
from PyQt6.QtCore import (
    QEasingCurve, QPoint, QPropertyAnimation, QRect, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ComboBox, FluentIcon, InfoBadge,
    InfoBar, PrimaryPushButton, PushButton, ScrollArea, SearchLineEdit,
    SubtitleLabel, SwitchButton, ToolButton, isDarkTheme,
)

from app import adb_runner as ar
from app.config import cfg, qconfig
from app.serial_utils import CODECS
from app.ui.adb_file_manager import AdbFileManagerWindow
from app.ui.terminal_widget import QTerminalWidget


def _labeled_switch(sw, text: str) -> None:
    sw.setOnText(text)
    sw.setOffText(text)


class _OpaqueSearchLineEdit(SearchLineEdit):
    """始终使用实色底，展开时完整遮住标题文字。"""

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(31, 31, 31) if isDarkTheme()
                         else QColor(255, 255, 255))
        painter.drawRoundedRect(self.rect(), 5, 5)
        painter.end()
        super().paintEvent(event)


class AdbPage(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.shell = ar.AdbShellProcess(self)
        self.runner = ar.AdbCommandRunner(self)
        self._deviceProbe = ar.AdbProbe(self)
        self._versionProbe = ar.AdbProbe(self)
        self._deviceProbe.finished.connect(self._on_devices_refreshed)
        self._versionProbe.finished.connect(self._on_version_refreshed)
        self._deviceRefreshNotify = False
        self._versionRefreshNotify = False
        self._versionPath = ""
        self.terminal = QTerminalWidget(self)
        # adb -t（尤旧版）把回车回显成裸 CR 导致覆盖/错位、像要回车两次；
        # cooked 模式下 \n 同样结束命令行，且其回显经 onlcr 变正常 CRLF。
        self.terminal.set_enter_mode("\n")

        self._serial_items = []          # 与 serialCombo 下标对应的纯 serial
        self._model_items = []           # [(stem, model, data)]
        self._active_profile = None      # 当前型号 profile 数据
        self._commands = []              # 当前型号的全部命令
        self._filtered_commands = []     # 当前搜索结果
        self._fileManagers = set()       # 独立顶层文件管理窗口

        # ── 布局：左=连接+命令集；右=终端；底=采集选项细条 ───────
        left = QWidget(self)
        left.setFixedWidth(330)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(12)
        ll.addWidget(self._build_connect_card())
        ll.addWidget(self._build_command_card(), 1)

        term_wrap = QWidget(self)
        tw = QVBoxLayout(term_wrap)
        tw.setContentsMargins(6, 6, 6, 6)
        tw.addWidget(self.terminal)

        right = QWidget(self)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        rl.addWidget(term_wrap, 1)
        rl.addWidget(self._build_option_strip())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 40, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(left)
        layout.addWidget(right, 1)

        self._connect_signals()
        self._refresh_adb_label()
        self.reload_models(preselect_default=True)
        self.refresh_serials()

    # ── 左：连接卡 ──────────────────────────────────────────────

    def _build_connect_card(self) -> CardWidget:
        card = CardWidget(self)
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("ADB 连接", card))

        self.serialCombo = ComboBox(card)
        self.serialCombo.setMinimumWidth(150)
        self.serialRefreshBtn = ToolButton(FluentIcon.UPDATE, card)
        self.serialRefreshBtn.setToolTip("刷新 adb 设备列表")
        self.serialRefreshBtn.clicked.connect(
            lambda _=False: self.refresh_serials(notify=True))
        sr = QHBoxLayout()
        sr.addWidget(self.serialCombo, 1)
        sr.addWidget(self.serialRefreshBtn)
        v.addWidget(BodyLabel("设备 (serial)", card))
        v.addLayout(sr)

        self.modelCombo = ComboBox(card)
        self.modelCombo.setMinimumWidth(150)
        model_refresh = ToolButton(FluentIcon.UPDATE, card)
        model_refresh.setToolTip("重载型号/命令集文件")
        model_refresh.clicked.connect(lambda _=False: self.reload_models())
        mr = QHBoxLayout(); mr.addWidget(self.modelCombo, 1); mr.addWidget(model_refresh)
        v.addWidget(BodyLabel("设备型号 (命令集)", card))
        v.addLayout(mr)

        self.adbLabel = CaptionLabel("", card)
        self.adbLabel.setWordWrap(True)
        self.adbDetectBtn = ToolButton(FluentIcon.SYNC, card)
        self.adbDetectBtn.setToolTip("检测 adb 版本")
        self.adbDetectBtn.clicked.connect(
            lambda _=False: self._refresh_adb_label(notify=True))
        ar_ = QHBoxLayout()
        ar_.addWidget(self.adbLabel, 1)
        ar_.addWidget(self.adbDetectBtn)
        v.addLayout(ar_)

        self.shellBtn = PrimaryPushButton("打开 ADB Shell", card)
        self.shellBtn.clicked.connect(lambda _=False: self._toggle_shell())
        v.addWidget(self.shellBtn)
        self.fileManagerBtn = PushButton(
            FluentIcon.FOLDER, "文件管理", card)
        self.fileManagerBtn.setToolTip("在独立窗口中管理设备文件")
        self.fileManagerBtn.clicked.connect(
            lambda _=False: self._open_file_manager())
        v.addWidget(self.fileManagerBtn)

        self._badgeBox = QHBoxLayout()
        self._badgeBox.addStretch(1)
        v.addLayout(self._badgeBox)
        self._badge = None
        self._set_badge("attention", "未连接")
        return card

    # ── 左：采集选项卡 ──────────────────────────────────────────

    def _build_option_strip(self) -> QWidget:
        strip = QWidget(self)
        strip.setFixedHeight(52)
        h = QHBoxLayout(strip)
        # 下方多留空白，让编码控件远离窗口底边。
        h.setContentsMargins(4, 4, 4, 12)
        h.setSpacing(10)
        h.addWidget(BodyLabel("编码", strip))
        self.codecCombo = ComboBox(strip)
        self.codecCombo.addItems(CODECS)
        self.codecCombo.setFixedWidth(110)
        self.codecCombo.currentTextChanged.connect(self._on_codec)
        h.addWidget(self.codecCombo)
        self.echoSwitch = SwitchButton(strip)
        _labeled_switch(self.echoSwitch, "本地回显")
        self.echoSwitch.checkedChanged.connect(self.terminal.set_local_echo)
        h.addWidget(self.echoSwitch)
        h.addStretch(1)
        return strip

    # ── 右：命令面板卡 ──────────────────────────────────────────

    def _build_command_card(self) -> QWidget:
        sec = QWidget(self)
        v = QVBoxLayout(sec)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # 标题行：大号标题 + 搜索/保存/清屏 工具按钮
        head = QHBoxLayout()
        self.modelTitle = SubtitleLabel("命令集", sec)
        self.commandSearchBtn = ToolButton(FluentIcon.SEARCH, sec)
        self.commandSearchBtn.setToolTip("按名称搜索命令")
        self.commandSearchBtn.clicked.connect(
            lambda _=False: self._toggle_command_search())
        save = ToolButton(FluentIcon.SAVE, sec)
        save.setToolTip("保存报告")
        save.clicked.connect(lambda _=False: self._save_report())
        clear = ToolButton(FluentIcon.BROOM, sec)
        clear.setToolTip("清屏")
        clear.clicked.connect(lambda _=False: self.terminal.clear())
        head.addWidget(self.modelTitle, 1)
        head.addWidget(self.commandSearchBtn)
        head.addWidget(save)
        head.addWidget(clear)
        v.addLayout(head)

        # 搜索框是页面内的普通悬浮控件，避免 Popup 抢占 Windows 输入法。
        # 它不参与布局，因此仍然不会挤占命令列表。
        self.commandSearch = _OpaqueSearchLineEdit(self)
        self.commandSearch.setPlaceholderText("搜索命令名称")
        self.commandSearch.setFixedHeight(36)
        self.commandSearch.setAttribute(
            Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.commandSearch.setInputMethodHints(Qt.InputMethodHint.ImhNone)
        self.commandSearch.textChanged.connect(self._apply_command_filter)
        self.commandSearch.editingFinished.connect(
            lambda: QTimer.singleShot(0, self._hide_command_search))
        self.commandSearch.hide()
        self._commandSearchAnimation = QPropertyAnimation(
            self.commandSearch, b"geometry", self)
        self._commandSearchAnimation.setDuration(180)
        self._commandSearchAnimation.setEasingCurve(
            QEasingCurve.Type.OutCubic)

        self.modelSubtitle = CaptionLabel("-", sec)
        v.addWidget(self.modelSubtitle)

        run_all = PrimaryPushButton(FluentIcon.PLAY, "全部采集", sec)
        run_all.clicked.connect(lambda _=False: self._run_all())
        v.addWidget(run_all)

        # 命令卡列表
        self._cmdScroll = ScrollArea(sec)
        self._cmdContainer = QWidget(self._cmdScroll)
        self._cmdLayout = QVBoxLayout(self._cmdContainer)
        self._cmdLayout.setContentsMargins(0, 0, 0, 0)
        self._cmdLayout.setSpacing(6)
        self._cmdLayout.addStretch(1)
        self._cmdScroll.setWidget(self._cmdContainer)
        self._cmdScroll.setWidgetResizable(True)
        self._cmdScroll.enableTransparentBackground()
        v.addWidget(self._cmdScroll, 1)
        return sec

    # ── 信号接线 ────────────────────────────────────────────────

    def _connect_signals(self):
        self.modelCombo.currentTextChanged.connect(self._on_model_changed)
        self.shell.dataReceived.connect(self.terminal.queue_bytes)
        self.shell.started.connect(self._on_shell_started)
        self.shell.stopped.connect(self._on_shell_stopped)
        self.runner.dataReceived.connect(self.terminal.queue_bytes)
        self.terminal.sendRequested.connect(self._on_terminal_input)

    # ── adb / serial / model ────────────────────────────────────

    def _resolve_adb(self, silent=False):
        path, err = ar.find_adb(qconfig.get(cfg.adbPath))
        if not path and not silent:
            InfoBar.warning(title="adb 不可用", content=err,
                            duration=5000, parent=self)
        return path

    def _refresh_adb_label(self, notify=False):
        path, err = ar.find_adb(qconfig.get(cfg.adbPath))
        if not path:
            self.adbLabel.setText(err)
            self.adbDetectBtn.setEnabled(True)
            if notify:
                InfoBar.warning(title="ADB 检测失败", content=err,
                                duration=4000, parent=self)
            return
        self._versionPath = path
        self._versionRefreshNotify = bool(notify)
        self.adbLabel.setText(f"{path}\n正在检测版本…")
        self.adbDetectBtn.setEnabled(False)
        self._versionProbe.start(path, ["version"], timeout_ms=6000)

    def _on_version_refreshed(self, data: bytes, code: int, error: str):
        self.adbDetectBtn.setEnabled(True)
        text = data.decode("utf-8", "replace").strip()
        version = ar.adb_version_line(text)
        version_tuple = ar.parse_adb_version(text)
        if code != 0 and not error:
            error = text.splitlines()[0] if text else f"adb version 退出码 {code}"
        if error:
            self.adbLabel.setText(f"{self._versionPath}\n检测失败：{error}")
            if self._versionRefreshNotify:
                InfoBar.warning(title="ADB 检测失败", content=error,
                                duration=4000, parent=self)
        elif ar.is_legacy_adb_version(version_tuple):
            warning = "版本过旧，交互输入可能严重延迟；请在设置中选择 ADB 1.0.40+"
            self.adbLabel.setText(
                f"{self._versionPath}\n{version or '(版本未知)'}\n⚠ {warning}")
            InfoBar.warning(title="ADB 版本过旧", content=warning,
                            duration=6000, parent=self)
        else:
            self.adbLabel.setText(
                f"{self._versionPath}\n{version or '(版本未知)'}")

    def refresh_serials(self, notify=False):
        path, err = ar.find_adb(qconfig.get(cfg.adbPath))
        if not path:
            self.serialRefreshBtn.setEnabled(True)
            if notify:
                InfoBar.warning(title="ADB 刷新失败", content=err,
                                duration=4000, parent=self)
            return
        self._deviceRefreshNotify = bool(notify)
        self.serialRefreshBtn.setEnabled(False)
        self._deviceProbe.start(
            path, ["devices", "-l"], timeout_ms=6000)

    def _on_devices_refreshed(self, data: bytes, code: int, error: str):
        self.serialRefreshBtn.setEnabled(True)
        text = data.decode("utf-8", "replace")
        if code != 0 and not error:
            error = text.strip() or f"adb devices 退出码 {code}"
        if error:
            if self._deviceRefreshNotify:
                InfoBar.warning(title="ADB 刷新失败", content=error,
                                duration=4000, parent=self)
            return
        self._apply_serial_devices(ar._parse_devices_text(text))

    def _apply_serial_devices(self, devs):
        prev = self._current_serial()
        first_device = None
        labels = []
        for d in devs:
            labels.append(f"{d['serial']}  [{d['state']}]")
            if d["state"] == "device" and first_device is None:
                first_device = d["serial"]
        serials = [d["serial"] for d in devs]

        # 列表未变化时不销毁/重建 ComboBox 项，减少 Qt 对象生命周期噪声。
        old_labels = [
            self.serialCombo.itemText(i)
            for i in range(self.serialCombo.count())
        ]
        self.serialCombo.blockSignals(True)
        if labels != old_labels:
            self.serialCombo.clear()
            self.serialCombo.addItems(labels)
        self._serial_items = serials
        target = prev if prev in self._serial_items else first_device
        if target:
            idx = self._serial_items.index(target)
            self.serialCombo.setCurrentIndex(idx)
        elif self._serial_items:
            self.serialCombo.setCurrentIndex(0)
        self.serialCombo.blockSignals(False)

    def _current_serial(self) -> str:
        i = self.serialCombo.currentIndex()
        return self._serial_items[i] if 0 <= i < len(self._serial_items) else ""

    def reload_models(self, preselect_default=False):
        self._model_items = ar.list_profiles()
        # 处理重名：重名时 label 追加 [stem]
        from collections import Counter
        cnt = Counter(m for _, m, _ in self._model_items)
        labels, self._stem_by_label, self._label_by_stem = [], {}, {}
        for stem, model, _data in self._model_items:
            label = f"{model}  [{stem}]" if cnt[model] > 1 else model
            labels.append(label)
            self._stem_by_label[label] = stem
            self._label_by_stem[stem] = label

        cur_stem = self._stem_by_label.get(
            self.modelCombo.currentText(), None)
        self.modelCombo.blockSignals(True)
        self.modelCombo.clear()
        self.modelCombo.addItems(labels)
        sel_stem = (qconfig.get(cfg.defaultModel) if preselect_default
                    else cur_stem)
        if sel_stem in self._label_by_stem:
            self.modelCombo.setCurrentText(self._label_by_stem[sel_stem])
        self.modelCombo.blockSignals(False)
        self._on_model_changed(self.modelCombo.currentText())

    def _on_model_changed(self, label: str):
        stem = self._stem_by_label.get(label, "")
        data = next((d for s, _m, d in self._model_items if s == stem), None)
        self._active_profile = data
        if data:
            self.modelSubtitle.setText(
                f"{data.get('model') or stem}  ·  {len(data['commands'])} 条命令")
            codec = (data.get("codec") or "UTF-8")
            if codec in CODECS:
                self.codecCombo.blockSignals(True)
                self.codecCombo.setCurrentText(codec)
                self.codecCombo.blockSignals(False)
                self._on_codec(codec)
        else:
            self.modelSubtitle.setText("未选择型号")
        self._rebuild_cmd_list(data["commands"] if data else [])

    def _rebuild_cmd_list(self, commands):
        self._commands = list(commands)
        self._apply_command_filter(self.commandSearch.text())

    def _apply_command_filter(self, text: str):
        needle = text.strip().casefold()
        self._filtered_commands = [
            c for c in self._commands
            if not needle or needle in str(c.get("name", "")).casefold()
        ]

        # 清空现有行（保留末尾 stretch）
        while self._cmdLayout.count() > 1:
            item = self._cmdLayout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for c in self._filtered_commands:
            self._cmdLayout.insertWidget(self._cmdLayout.count() - 1,
                                         self._make_cmd_row(c))

        if needle and not self._filtered_commands:
            empty = CaptionLabel("未找到匹配的命令", self._cmdContainer)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._cmdLayout.insertWidget(0, empty)

        self._cmdScroll.verticalScrollBar().setValue(0)
        self.commandSearchBtn.setToolTip(
            f"按名称搜索命令（当前：{text.strip()}）" if needle
            else "按名称搜索命令")

    def _toggle_command_search(self):
        if self.commandSearch.isVisible():
            self._hide_command_search()
            return

        # 与标题同一行，右边缘固定在搜索按钮右侧，宽度向左展开并覆盖标题。
        button_pos = self.mapFromGlobal(
            self.commandSearchBtn.mapToGlobal(QPoint(0, 0)))
        title_pos = self.mapFromGlobal(
            self.modelTitle.mapToGlobal(QPoint(0, 0)))
        height = self.commandSearch.height()
        y = button_pos.y() + (self.commandSearchBtn.height() - height) // 2
        right = button_pos.x() + self.commandSearchBtn.width()
        left = max(0, title_pos.x())
        end_rect = QRect(left, y, max(120, right - left), height)
        start_width = min(self.commandSearchBtn.width(), end_rect.width())
        start_rect = QRect(
            right - start_width, y, start_width, height)

        self._commandSearchAnimation.stop()
        self.commandSearch.setGeometry(start_rect)
        self.commandSearch.show()
        self.commandSearch.raise_()
        self.commandSearch.setFocus()
        self.commandSearch.selectAll()
        self._commandSearchAnimation.setStartValue(start_rect)
        self._commandSearchAnimation.setEndValue(end_rect)
        self._commandSearchAnimation.start()

    def _hide_command_search(self):
        self._commandSearchAnimation.stop()
        self.commandSearch.hide()

    def _make_cmd_row(self, c: dict) -> QWidget:
        row = CardWidget(self._cmdContainer)
        row.setClickEnabled(True)
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setToolTip(f"{c['name']}\n$ {c['cmd']}")
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 9, 12, 9)
        h.setSpacing(10)

        play = BodyLabel("▶", row)
        play.setTextColor(QColor(18, 150, 140), QColor(45, 210, 195))
        play.setAlignment(Qt.AlignmentFlag.AlignCenter)
        play.setFixedWidth(14)
        pf = play.font()
        pf.setPointSize(10)
        play.setFont(pf)

        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(2)
        nl = BodyLabel(self._elide(c["name"], 16), row)
        cl = CaptionLabel(self._elide(c["cmd"], 34), row)
        cl.setFont(QFont("Consolas", 9))
        texts.addWidget(nl)
        texts.addWidget(cl)

        h.addWidget(play, 0, Qt.AlignmentFlag.AlignVCenter)
        h.addLayout(texts, 1)

        n, cc = c["name"], c["cmd"]
        row.clicked.connect(lambda _=False, name=n, cmd=cc: self._run_one(name, cmd))
        return row

    @staticmethod
    def _elide(s: str, n: int) -> str:
        s = s.replace("\n", " ⏎ ")
        return s if len(s) <= n else s[:n] + "…"

    # ── 动作 ────────────────────────────────────────────────────

    def _on_codec(self, codec: str):
        self.terminal.set_codec(codec)
        self.runner.set_codec(codec)

    def _on_terminal_input(self, data: bytes):
        if self.shell.is_running():
            self.shell.write(data)

    def _toggle_shell(self):
        if self.shell.is_running():
            self.shell.stop()
            return
        path = self._resolve_adb()
        serial = self._current_serial()
        if not path or not serial:
            if not serial:
                InfoBar.warning(title="提示", content="请先选择/刷新设备",
                                duration=3000, parent=self)
            return
        self.shell.start(path, serial)

    def _open_file_manager(self):
        path = self._resolve_adb()
        serial = self._current_serial()
        if not path or not serial:
            if not serial:
                InfoBar.warning(
                    title="无法打开文件管理",
                    content="请先选择或刷新 ADB 设备",
                    duration=3500, parent=self)
            return
        window = AdbFileManagerWindow(path, serial)
        self._fileManagers.add(window)
        window.destroyed.connect(
            lambda _=None, w=window: self._fileManagers.discard(w))
        window.show()
        window.raise_()
        window.activateWindow()

    def _on_shell_started(self):
        self.shellBtn.setText("关闭 ADB Shell")
        self._set_badge("success", "Shell 已连接")

    def _on_shell_stopped(self, code, msg):
        self.shellBtn.setText("打开 ADB Shell")
        self._set_badge("attention", "未连接")
        if msg:
            InfoBar.error(title="ADB Shell 错误", content=msg,
                          duration=4000, parent=self)

    def _run_one(self, name: str, cmd: str):
        path = self._resolve_adb()
        serial = self._current_serial()
        if not path or not serial:
            if not serial:
                InfoBar.warning(title="提示", content="请先选择/刷新设备",
                                duration=3000, parent=self)
            return
        self.runner.run_one(path, serial, name, cmd)

    def _run_all(self):
        if not self._active_profile:
            InfoBar.warning(title="提示", content="无可用命令集",
                            duration=3000, parent=self)
            return
        path = self._resolve_adb()
        serial = self._current_serial()
        if not path or not serial:
            if not serial:
                InfoBar.warning(title="提示", content="请先选择/刷新设备",
                                duration=3000, parent=self)
            return
        self.runner.run_all(path, serial, self._active_profile["commands"])

    def _save_report(self):
        import time
        default = f"adb_report_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "保存报告", default, "Text (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", errors="replace") as f:
                f.write(self.terminal.all_text())
            InfoBar.success(title="已保存", content=path,
                            duration=3000, parent=self)
        except OSError as e:
            InfoBar.error(title="保存失败", content=str(e),
                          duration=5000, parent=self)

    # ── 状态徽标 / 停机 ─────────────────────────────────────────

    def _set_badge(self, kind: str, text: str):
        if self._badge is not None:
            self._badgeBox.removeWidget(self._badge)
            self._badge.deleteLater()
            self._badge = None
        maker = {"success": InfoBadge.success, "error": InfoBadge.error,
                 "attention": InfoBadge.attension}[kind]
        self._badge = maker(text, parent=self)
        self._badgeBox.insertWidget(0, self._badge)

    def shutdown(self):
        self.terminal.discard_queued_bytes()
        self._deviceProbe.shutdown()
        self._versionProbe.shutdown()
        try:
            self.shell.shutdown()
        except Exception:
            pass
        try:
            self.runner.shutdown()
        except Exception:
            pass

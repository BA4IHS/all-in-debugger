# coding: utf-8
"""ADB 调试页：左连接/采集配置 + 右(终端 + 命令面板)。

- 终端复用 pyte QTerminalWidget；交互 shell 与采集输出共用此终端
- 交互 shell = `adb -s <serial> shell -t`；采集 = `adb -s <serial> shell <cmd>`
- 型号(=命令集) 与 serial(=连接目标) 分开选择
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ComboBox, FluentIcon, InfoBadge,
    InfoBar, PrimaryPushButton, ScrollArea, SubtitleLabel, SwitchButton,
    ToolButton,
)

from app import adb_runner as ar
from app.config import cfg, qconfig
from app.serial_utils import CODECS
from app.ui.terminal_widget import QTerminalWidget


def _labeled_switch(sw, text: str) -> None:
    sw.setOnText(text)
    sw.setOffText(text)


class AdbPage(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.shell = ar.AdbShellProcess(self)
        self.runner = ar.AdbCommandRunner(self)
        self.terminal = QTerminalWidget(self)
        # adb -t（尤旧版）把回车回显成裸 CR 导致覆盖/错位、像要回车两次；
        # cooked 模式下 \n 同样结束命令行，且其回显经 onlcr 变正常 CRLF。
        self.terminal.set_enter_mode("\n")

        self._serial_items = []          # 与 serialCombo 下标对应的纯 serial
        self._model_items = []           # [(stem, model, data)]
        self._active_profile = None      # 当前型号 profile 数据

        # ── 布局：左=连接+命令集；右=终端；底=采集选项细条 ───────
        left = QWidget(self)
        left.setFixedWidth(340)
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
        v.setSpacing(10)
        v.addWidget(SubtitleLabel("ADB 连接", card))

        self.serialCombo = ComboBox(card)
        self.serialCombo.setMinimumWidth(150)
        serial_refresh = ToolButton(FluentIcon.UPDATE, card)
        serial_refresh.setToolTip("刷新 adb 设备列表")
        serial_refresh.clicked.connect(lambda _=False: self.refresh_serials())
        sr = QHBoxLayout(); sr.addWidget(self.serialCombo, 1); sr.addWidget(serial_refresh)
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
        detect = ToolButton(FluentIcon.SYNC, card)
        detect.setToolTip("检测 adb 版本")
        detect.clicked.connect(lambda _=False: self._refresh_adb_label())
        ar_ = QHBoxLayout(); ar_.addWidget(self.adbLabel, 1); ar_.addWidget(detect)
        v.addLayout(ar_)

        self.shellBtn = PrimaryPushButton("打开 ADB Shell", card)
        self.shellBtn.clicked.connect(lambda _=False: self._toggle_shell())
        v.addWidget(self.shellBtn)

        self._badgeBox = QHBoxLayout()
        self._badgeBox.addStretch(1)
        v.addLayout(self._badgeBox)
        self._badge = None
        self._set_badge("attention", "未连接")
        return card

    # ── 左：采集选项卡 ──────────────────────────────────────────

    def _build_option_strip(self) -> QWidget:
        strip = QWidget(self)
        strip.setFixedHeight(40)
        h = QHBoxLayout(strip)
        h.setContentsMargins(4, 4, 4, 4)
        h.setSpacing(10)
        h.addWidget(BodyLabel("采集选项", strip))
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

        # 标题行：大号标题 + 保存/清屏 工具按钮
        head = QHBoxLayout()
        self.modelTitle = SubtitleLabel("命令集", sec)
        save = ToolButton(FluentIcon.SAVE, sec)
        save.setToolTip("保存报告")
        save.clicked.connect(lambda _=False: self._save_report())
        clear = ToolButton(FluentIcon.BROOM, sec)
        clear.setToolTip("清屏")
        clear.clicked.connect(lambda _=False: self.terminal.clear())
        head.addWidget(self.modelTitle, 1)
        head.addWidget(save)
        head.addWidget(clear)
        v.addLayout(head)
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
        self.shell.dataReceived.connect(self.terminal.feed_bytes)
        self.shell.started.connect(self._on_shell_started)
        self.shell.stopped.connect(self._on_shell_stopped)
        self.runner.dataReceived.connect(self.terminal.feed_bytes)
        self.terminal.sendRequested.connect(self._on_terminal_input)

    # ── adb / serial / model ────────────────────────────────────

    def _resolve_adb(self, silent=False):
        path, err = ar.find_adb(qconfig.get(cfg.adbPath))
        if not path and not silent:
            InfoBar.warning(title="adb 不可用", content=err,
                            duration=5000, parent=self)
        return path

    def _refresh_adb_label(self):
        path, err = ar.find_adb(qconfig.get(cfg.adbPath))
        if not path:
            self.adbLabel.setText(err)
            return
        ver, _ = ar.adb_version(path)
        self.adbLabel.setText(f"{path}\n{ver or '(版本未知)'}")

    def refresh_serials(self):
        path = self._resolve_adb(silent=True)
        devs, err = (ar.list_adb_devices(path) if path else ([], err))
        prev = self._current_serial()
        self._serial_items = [d["serial"] for d in devs]
        self.serialCombo.blockSignals(True)
        self.serialCombo.clear()
        first_device = None
        for d in devs:
            label = f"{d['serial']}  [{d['state']}]"
            self.serialCombo.addItem(label)
            if d["state"] == "device" and first_device is None:
                first_device = d["serial"]
        target = prev if prev in self._serial_items else first_device
        if target:
            idx = self._serial_items.index(target)
            self.serialCombo.setCurrentIndex(idx)
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
        # 清空现有行（保留末尾 stretch）
        while self._cmdLayout.count() > 1:
            item = self._cmdLayout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for c in commands:
            self._cmdLayout.insertWidget(self._cmdLayout.count() - 1,
                                         self._make_cmd_row(c))

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
        try:
            self.shell.stop()
        except Exception:
            pass
        try:
            self.runner.cancel()
        except Exception:
            pass

# coding: utf-8
"""带浮动检索条的只读日志文本框。"""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QKeyEvent, QTextCharFormat, QTextCursor, QTextDocument
from PyQt6.QtWidgets import QMenu, QTextEdit

from qfluentwidgets import PlainTextEdit, SmoothMode

from app.ui.find_bar import FindBar


class SearchablePlainTextEdit(PlainTextEdit):
    """支持 Ctrl+F、右键查找、循环跳转和全量匹配高亮。"""

    MATCH_COLOR = QColor(110, 90, 15, 150)
    CURRENT_COLOR = QColor(210, 145, 20, 210)

    def __init__(self, parent=None):
        super().__init__(parent)
        # 串口日志持续追加时，Fluent 默认的定时平滑滚动容易与刷新定时器
        # 相互叠加，造成滚轮响应滞后。改用 Qt 原生的同步滚动。
        self.scrollDelegate.verticalSmoothScroll.setSmoothMode(
            SmoothMode.NO_SMOOTH)
        self._query = ""
        self._case_sensitive = False
        self._matches = []
        self._current = -1

        self._find_bar = FindBar(self)
        self._find_bar.textChanged.connect(self._on_find_text)
        self._find_bar.nextRequested.connect(self._find_next)
        self._find_bar.prevRequested.connect(self._find_prev)
        self._find_bar.caseChanged.connect(self._on_find_case)
        self._find_bar.closeRequested.connect(self._close_find)
        self.document().contentsChanged.connect(self._on_document_changed)

    def _open_find(self):
        selected = self.textCursor().selectedText().replace("\u2029", " ").strip()
        self._find_bar.open(selected)

    def _close_find(self):
        self._find_bar.hide()
        self._query = ""
        self._matches = []
        self._current = -1
        self.setExtraSelections([])
        self.setFocus()

    def _on_find_text(self, text: str):
        self._query = text
        self._recompute(keep_current=False)

    def _on_find_case(self, on: bool):
        self._case_sensitive = bool(on)
        self._recompute(keep_current=False)

    def _on_document_changed(self):
        if self._query:
            self._recompute(keep_current=True)

    def _recompute(self, keep_current=False):
        old = self._current
        self._matches = []
        if self._query:
            flags = QTextDocument.FindFlag(0)
            if self._case_sensitive:
                flags |= QTextDocument.FindFlag.FindCaseSensitively
            cursor = QTextCursor(self.document())
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            while True:
                cursor = self.document().find(self._query, cursor, flags)
                if cursor.isNull():
                    break
                self._matches.append(QTextCursor(cursor))

        if not self._matches:
            self._current = -1
        elif keep_current:
            self._current = min(max(0, old), len(self._matches) - 1)
        else:
            self._current = 0
        self._apply_highlights(scroll=not keep_current)

    def _find_next(self):
        if not self._matches:
            return
        self._current = (self._current + 1) % len(self._matches)
        self._apply_highlights(scroll=True)

    def _find_prev(self):
        if not self._matches:
            return
        self._current = (self._current - 1) % len(self._matches)
        self._apply_highlights(scroll=True)

    def _apply_highlights(self, scroll=False):
        selections = []
        for i, cursor in enumerate(self._matches):
            selection = QTextEdit.ExtraSelection()
            selection.cursor = QTextCursor(cursor)
            fmt = QTextCharFormat()
            fmt.setBackground(
                self.CURRENT_COLOR if i == self._current else self.MATCH_COLOR)
            selection.format = fmt
            selections.append(selection)
        self.setExtraSelections(selections)
        total = len(self._matches)
        self._find_bar.set_count(self._current + 1 if self._current >= 0 else 0, total)
        if scroll and self._current >= 0:
            self.setTextCursor(QTextCursor(self._matches[self._current]))
            self.ensureCursorVisible()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_F and \
                event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._open_find()
            return
        if event.key() == Qt.Key.Key_Escape and self._find_bar.isVisible():
            self._close_find()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu: QMenu = self.createStandardContextMenu()
        menu.addSeparator()
        find_action = menu.addAction("查找")
        find_action.triggered.connect(self._open_find)
        menu.exec(event.globalPos())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._find_bar.place()

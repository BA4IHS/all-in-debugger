# coding: utf-8
"""交互式串口终端：pyte(VT100/ANSI 仿真) + Qt 自绘字符网格 + 键盘直发。

设计要点（均经实测确定，非猜测）：
- pyte.Char 字段：data/fg/bg/bold/italics/underscore/strikethrough/reverse/blink
- 颜色名：标准色用英文名，且 33='brown'(=黄)；亮色(90-97/100-107)是独立名字
  bright*；pyte0.8.2 背景亮洋红拼写为 'bfightmagenta'(库 bug)，需一并映射。
  256/24bit 颜色为 6 位 hex 串。
- buffer[row][col] 稀疏，用 .get(col, default_char)。
- 宽字符占两列，右列 data='' 作占位；渲染时跳过占位、左列画两格宽。
- 回滚不用 pyte.HistoryScreen（其 history 行切分异常），改为自维护 scrollback：
  重写 index()，在顶行滚出时快照该行入 deque。
"""
import collections

import pyte
from wcwidth import wcwidth as _wcw

from PyQt6.QtCore import QPointF, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QFontMetrics, QKeyEvent, QPainter, QPainterPath, QPen,
    QMouseEvent,
)
from PyQt6.QtWidgets import QApplication, QMenu, QScrollBar, QWidget

from qfluentwidgets import isDarkTheme

from app.serial_utils import decode_chunk, make_decoder
from app.ui.find_bar import FindBar

# 终端面板内边距与圆角（描边画框，文本区向内缩进避免压到圆角）
_PAD = 8
_RADIUS = 8
_QUEUED_FEED_CHUNK = 2048       # 单次解析控制在约一帧内，避免 ADB 大输出阻塞输入

# ---------------------------------------------------------------------------
# 调色板（VSCode 风格 16 色 + pyte typo 兼容）
# ---------------------------------------------------------------------------

PALETTE = {
    "black": (0, 0, 0), "red": (205, 49, 49), "green": (13, 188, 121),
    "brown": (228, 192, 53), "blue": (36, 114, 200), "magenta": (188, 63, 188),
    "cyan": (17, 165, 183), "white": (229, 229, 229),
    "brightblack": (102, 102, 102), "brightred": (241, 76, 76),
    "brightgreen": (35, 209, 139), "brightbrown": (245, 245, 67),
    "brightblue": (59, 142, 234), "brightmagenta": (214, 112, 214),
    "brightcyan": (41, 184, 219), "brightwhite": (255, 255, 255),
    "bfightmagenta": (214, 112, 214),  # pyte 0.8.2 背景亮洋红拼写 bug
}
_BOLD2BRIGHT = {
    "black": "brightblack", "red": "brightred", "green": "brightgreen",
    "brown": "brightbrown", "blue": "brightblue", "magenta": "brightmagenta",
    "cyan": "brightcyan", "white": "brightwhite",
}
DEFAULT_FG = (212, 212, 212)
DEFAULT_BG = (30, 30, 30)
CURSOR_COLOR = (220, 220, 220)
SELECTION_BG = (38, 79, 120)
FIND_BG = (90, 75, 15)          # 匹配（暗黄，终端恒深色，浅字仍可读）
FIND_CUR_BG = (170, 120, 20)    # 当前匹配


def resolve_color(name, is_bg: bool):
    if name == "default" or not name:
        return DEFAULT_BG if is_bg else DEFAULT_FG
    if name in PALETTE:
        return PALETTE[name]
    if isinstance(name, str) and len(name) == 6:
        try:
            return (int(name[0:2], 16), int(name[2:4], 16), int(name[4:6], 16))
        except ValueError:
            pass
    return DEFAULT_BG if is_bg else DEFAULT_FG


# ---------------------------------------------------------------------------
# 键盘映射
# ---------------------------------------------------------------------------

# Qt Key 常量延迟取（避免顶部导入 QtWidgets 之外的依赖问题）
def _k(name):
    from PyQt6.QtCore import Qt as _Qt
    return getattr(_Qt.Key, "Key_" + name)


_FUNCTION_KEYS = {
    "F1": b"\x1bOP", "F2": b"\x1bOQ", "F3": b"\x1bOR", "F4": b"\x1bOS",
    "F5": b"\x1b[15~", "F6": b"\x1b[17~", "F7": b"\x1b[18~", "F8": b"\x1b[19~",
    "F9": b"\x1b[20~", "F10": b"\x1b[21~", "F11": b"\x1b[23~", "F12": b"\x1b[24~",
}


def key_event_to_bytes(key: int, modifiers, text: str, codec: str,
                       enter_mode: str) -> bytes:
    """把 QKeyEvent 信息映射为发往串口的字节；不需要发送时返回 b''。

    纯函数，便于单测（不依赖 QWidget）。
    """
    from PyQt6.QtCore import Qt as _Qt
    ctrl = bool(modifiers & _Qt.KeyboardModifier.ControlModifier)
    shift = bool(modifiers & _Qt.KeyboardModifier.ShiftModifier)
    alt = bool(modifiers & _Qt.KeyboardModifier.AltModifier)

    # Ctrl+字母 -> 控制字符
    if ctrl and not alt and _Qt.Key.Key_A <= key <= _Qt.Key.Key_Z:
        return bytes([key - _Qt.Key.Key_A + 1])
    if ctrl and not alt:
        if key == _Qt.Key.Key_BracketLeft:
            return b"\x1b"
        if key == _Qt.Key.Key_Backslash:
            return b"\x1c"
        if key == _Qt.Key.Key_BracketRight:
            return b"\x1d"

    # 回车 / 退格 / Tab / Esc
    if key in (_Qt.Key.Key_Return, _Qt.Key.Key_Enter):
        return enter_mode.encode("latin-1", "replace")
    if key == _Qt.Key.Key_Backspace:
        return b"\x7f"
    if key == _Qt.Key.Key_Tab:
        return b"\t"
    if key == _Qt.Key.Key_Escape:
        return b"\x1b"

    # 方向 / 编辑键
    nav = {
        _Qt.Key.Key_Up: b"\x1b[A", _Qt.Key.Key_Down: b"\x1b[B",
        _Qt.Key.Key_Right: b"\x1b[C", _Qt.Key.Key_Left: b"\x1b[D",
        _Qt.Key.Key_Home: b"\x1b[H", _Qt.Key.Key_End: b"\x1b[F",
        _Qt.Key.Key_Insert: b"\x1b[2~", _Qt.Key.Key_Delete: b"\x1b[3~",
    }
    if key in nav:
        return nav[key]

    # 功能键
    for name, seq in _FUNCTION_KEYS.items():
        if key == getattr(_Qt.Key, "Key_" + name):
            return seq

    # 可打印字符（不含纯修饰键）
    if text and not (ctrl or alt):
        # 过滤掉仅修饰键产生的空/控制 text
        if text.isprintable():
            return text.encode(codec, "replace")
    return b""


# ---------------------------------------------------------------------------
# pyte 屏幕子类：自维护回滚
# ---------------------------------------------------------------------------

class TermScreen(pyte.Screen):

    def __init__(self, columns, lines, scrollback=2000):
        super().__init__(columns, lines)
        self.scrollback = collections.deque(maxlen=scrollback)

    def _snapshot(self, row):
        line = self.buffer.get(row, {})
        return [line.get(c, self.default_char) for c in range(self.columns)]

    def index(self):
        top = self.margins[0] if self.margins else 0
        bottom = self.margins[1] if self.margins else self.lines - 1
        if self.cursor.y >= bottom:
            self.scrollback.append(self._snapshot(top))
        super().index()

    def reset_scrollback(self):
        self.scrollback.clear()


# ---------------------------------------------------------------------------
# 终端控件
# ---------------------------------------------------------------------------

class QTerminalWidget(QWidget):
    """键盘输入 -> sendRequested(bytes)；外部调 feed_bytes() 喂入接收数据。"""

    sendRequested = pyqtSignal(bytes)

    def __init__(self, parent=None, scrollback=2000):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._codec = "UTF-8"
        self._decoder = make_decoder(self._codec)
        self._enter_mode = "\r"
        self._local_echo = False
        self._feed_queue = collections.deque()
        self._feed_head_offset = 0
        self._queued_bytes = 0
        self._feed_timer = QTimer(self)
        self._feed_timer.setSingleShot(True)
        self._feed_timer.timeout.connect(self._drain_feed_queue)

        self._cols = 80
        self._rows = 24
        self._screen = TermScreen(self._cols, self._rows, scrollback)
        self._stream = pyte.Stream(self._screen)

        self._font = QFont("Consolas", 11)
        self._font.setStyleStrategy(QFont.StyleStrategy.PreferNoAntialias
                                    if False else QFont.StyleStrategy.PreferAntialias)
        self._recalc_metrics()

        # 滚动模型：_top_line=可见首行绝对索引；_stick=粘底(自动跟随输出)
        self._top_line = 0
        self._stick = True
        self._gx = 12                      # 与普通日志一致的窄滚动条槽

        self._blink_on = True
        self._blink = QTimer(self)
        self._blink.setInterval(530)
        self._blink.timeout.connect(self._toggle_blink)
        self._blink.start()

        # 右侧垂直滚动条（作为子控件叠在深色面板右侧）
        self._vbar = QScrollBar(Qt.Orientation.Vertical, self)
        self._vbar.setFixedWidth(self._gx)
        self._vbar.setRange(0, 0)
        self._vbar.valueChanged.connect(self._on_vbar)

        # 拖选越过上/下边缘时自动滚动并延伸选区
        self._drag_dir = 0
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(80)
        self._drag_timer.timeout.connect(self._drag_scroll_step)

        # 选择 (abs_row, col)
        self._sel_anchor = None
        self._sel_current = None

        self.setMouseTracking(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

        # 检索
        self._find_text = ""
        self._find_case = False
        self._matches = []          # [(abs_row, start_cell, end_cell), ...]
        self._cur = -1
        self._cur_match = None
        self._find = FindBar(self)
        # 查找条与右侧滚动条保持间距，避免覆盖滚动槽。
        self._find.set_right_margin(self._gx + 10)
        self._find.textChanged.connect(self._on_find_text)
        self._find.nextRequested.connect(self._find_next)
        self._find.prevRequested.connect(self._find_prev)
        self._find.caseChanged.connect(self._on_find_case)
        self._find.closeRequested.connect(self._close_find)
        self._find_refresh_timer = QTimer(self)
        self._find_refresh_timer.setSingleShot(True)
        self._find_refresh_timer.setInterval(80)
        self._find_refresh_timer.timeout.connect(
            lambda: self._recompute(keep_cur=True))

    # ── 对外 API ────────────────────────────────────────────────

    def feed_bytes(self, data: bytes):
        text = decode_chunk(self._decoder, data)
        if text:
            self._stream.feed(text)
        self._after_data()

    def queue_bytes(self, data: bytes):
        """异步分片投喂大块输出，让键盘/窗口事件保持可响应。"""
        if not data:
            return
        chunk = bytes(data)
        self._feed_queue.append(chunk)
        self._queued_bytes += len(chunk)
        if not self._feed_timer.isActive():
            self._feed_timer.start(0)

    def queued_byte_count(self) -> int:
        return self._queued_bytes

    def discard_queued_bytes(self):
        self._feed_timer.stop()
        self._feed_queue.clear()
        self._feed_head_offset = 0
        self._queued_bytes = 0

    def _drain_feed_queue(self):
        if not self._feed_queue:
            self._feed_head_offset = 0
            self._queued_bytes = 0
            return
        head = self._feed_queue[0]
        start = self._feed_head_offset
        end = min(len(head), start + _QUEUED_FEED_CHUNK)
        chunk = head[start:end]
        if end >= len(head):
            self._feed_queue.popleft()
            self._feed_head_offset = 0
        else:
            self._feed_head_offset = end
        self._queued_bytes -= len(chunk)
        self.feed_bytes(chunk)
        if self._feed_queue:
            self._feed_timer.start(0)

    def set_codec(self, codec: str):
        if codec != self._codec:
            self._codec = codec
            self._decoder = make_decoder(codec)

    def set_enter_mode(self, mode: str):
        self._enter_mode = mode

    def set_local_echo(self, on: bool):
        self._local_echo = bool(on)

    def clear(self):
        self.discard_queued_bytes()
        self._find_refresh_timer.stop()
        self._decoder = make_decoder(self._codec)
        self._screen.reset()
        self._screen.reset_scrollback()
        self._top_line = 0
        self._stick = True
        self._sel_anchor = self._sel_current = None
        self._sync_vbar()
        if self._find_text:
            self._recompute(keep_cur=False)
        self.update()

    def set_font_size(self, pt: int):
        self._font = QFont("Consolas", max(6, int(pt)))
        self._recalc_metrics()
        self._refit()

    # ── 度量 / 行模型 ───────────────────────────────────────────

    def _recalc_metrics(self):
        fm = QFontMetrics(self._font)
        self._cell_w = max(1, fm.horizontalAdvance("M"))
        self._cell_h = max(1, fm.height())
        self._ascent = fm.ascent()

    def _refit(self):
        area_w = max(1, self.width() - self._gx - 2 * _PAD)
        area_h = max(1, self.height() - 2 * _PAD)
        cols = max(2, area_w // self._cell_w)
        rows = max(2, area_h // self._cell_h)
        changed = cols != self._cols or rows != self._rows
        if changed:
            self._cols, self._rows = cols, rows
            self._screen.resize(rows, cols)
        mt = self._max_top()
        if self._stick:
            self._top_line = mt
        else:
            self._top_line = min(self._top_line, mt)
        self._sync_vbar()
        self._layout_vbar()
        if changed and self._find_text:
            self._recompute(keep_cur=True)
        self.update()

    def _total_lines(self):
        return len(self._screen.scrollback) + self._screen.lines

    def _first_abs(self):
        return self._top_line

    def _max_top(self):
        return max(0, self._total_lines() - self._rows)

    def _after_data(self):
        """新数据到达：粘底则跟随；已上翻则保持当前顶行，不被新数据拽走。"""
        mt = self._max_top()
        if self._stick:
            self._top_line = mt
        elif self._top_line > mt:
            self._top_line = mt
        self._sync_vbar()
        if self._find_text:
            # ADB 大输出可能被拆成数百块；合并查找刷新，避免每块都全量扫描。
            self._find_refresh_timer.start()
        self.update()

    def _sync_vbar(self):
        mt = self._max_top()
        self._vbar.blockSignals(True)
        self._vbar.setRange(0, mt)
        self._vbar.setPageStep(max(1, self._rows))
        self._vbar.setValue(self._top_line)
        self._vbar.setEnabled(mt > 0)
        self._vbar.blockSignals(False)

    def _on_vbar(self, value):
        self._top_line = int(value)
        self._stick = self._top_line >= self._max_top()
        self.update()

    def _layout_vbar(self):
        self._vbar.setGeometry(max(0, self.width() - self._gx),
                               0, self._gx, self.height())

    def _jump_bottom(self):
        self._stick = True
        self._top_line = self._max_top()
        self._sync_vbar()
        self.update()

    def _scroll_up(self, n):
        self._top_line = max(0, self._top_line - n)
        self._stick = self._top_line >= self._max_top()
        self._sync_vbar()
        self.update()

    def _scroll_down(self, n):
        self._top_line = min(self._max_top(), self._top_line + n)
        self._stick = self._top_line >= self._max_top()
        self._sync_vbar()
        self.update()

    def _drag_scroll_step(self):
        if self._sel_anchor is None:
            self._drag_timer.stop()
            return
        if self._drag_dir < 0:
            self._scroll_up(1)
            self._sel_current = (self._first_abs(), 0)
        elif self._drag_dir > 0:
            self._scroll_down(1)
            self._sel_current = (min(self._first_abs() + self._rows - 1,
                                     self._total_lines() - 1),
                                 self._cols - 1)
        self.update()

    def _doc_line(self, abs_row):
        sb = self._screen.scrollback
        if abs_row < len(sb):
            row = sb[abs_row]
        else:
            r = abs_row - len(sb)
            line = self._screen.buffer.get(r, {})
            return [line.get(c, self._screen.default_char)
                    for c in range(self._cols)]
        # 回滚快照的列宽可能等于"捕获时"的列宽，与窗口缩放后的当前列宽不同，
        # 必须规整到 self._cols，否则绘制/选择按当前列宽遍历会越界。
        n = self._cols
        if len(row) == n:
            return row
        if len(row) > n:
            return row[:n]
        return row + [self._screen.default_char] * (n - len(row))

    # ── 绘制 ────────────────────────────────────────────────────

    def paintEvent(self, _event):
        painter = QPainter(self)
        try:
            self._draw(painter)
        finally:
            painter.end()

    def _draw(self, painter: QPainter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        W, H = self._cell_w, self._cell_h
        ox, oy = _PAD, _PAD                # 右侧为滚动条预留空间

        # 圆角深色面板（终端始终深色，做成带描边的画框面板）
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, max(0.0, w - 1), max(0.0, h - 1),
                            _RADIUS, _RADIUS)
        painter.fillPath(path, QColor(*DEFAULT_BG))

        painter.save()
        painter.setClipPath(path)
        painter.setFont(self._font)

        first = self._first_abs()
        sel = self._sel_rect()

        for r in range(self._rows):
            line = self._doc_line(first + r)
            y = oy + r * H
            row_marks = self._row_marks(first + r)
            c = 0
            while c < self._cols:
                ch = line[c]
                data = ch.data
                if data == "":            # 宽字符右半占位
                    c += 1
                    continue
                cw = _wcw(data) if data else 1
                ww = 2 if (cw or 1) >= 2 else 1

                fg_name, bg_name, bold, reverse = ch.fg, ch.bg, ch.bold, ch.reverse
                if reverse:
                    fg_name, bg_name = bg_name, fg_name
                if bold and fg_name in _BOLD2BRIGHT:
                    fg_name = _BOLD2BRIGHT[fg_name]

                bg = resolve_color(bg_name, True)
                in_sel = sel is not None and self._in_sel(first + r, c, sel)
                m = row_marks[c] if c < len(row_marks) else 0
                if m == 2:
                    painter.fillRect(ox + c * W, y, ww * W, H, QColor(*FIND_CUR_BG))
                elif m == 1:
                    painter.fillRect(ox + c * W, y, ww * W, H, QColor(*FIND_BG))
                elif in_sel:
                    painter.fillRect(ox + c * W, y, ww * W, H, QColor(*SELECTION_BG))
                elif bg != DEFAULT_BG:
                    painter.fillRect(ox + c * W, y, ww * W, H, QColor(*bg))

                if data and data != " " and (cw or 0) >= 1:
                    painter.setPen(QPen(QColor(*resolve_color(fg_name, False))))
                    painter.drawText(QPointF(ox + c * W, y + self._ascent), data)
                c += ww

        # 光标（仅粘底实时视图且可见）
        if self._stick and self._blink_on and self.hasFocus():
            cx, cy = self._screen.cursor.x, self._screen.cursor.y
            if 0 <= cy < self._rows:
                cr = QRect(ox + cx * W, oy + cy * H, W, H)
                painter.fillRect(cr, QColor(*CURSOR_COLOR))
                ch = self._screen.buffer.get(cy, {}).get(
                    cx, self._screen.default_char)
                if ch.data and ch.data != " ":
                    painter.setPen(QPen(QColor(*DEFAULT_BG)))
                    painter.drawText(QPointF(ox + cx * W, oy + cy * H + self._ascent),
                                     ch.data)
        painter.restore()

        # 描边（主题自适应：浅主题深边、深主题浅边）
        border = QColor(0, 0, 0, 120) if not isDarkTheme() else QColor(255, 255, 255, 90)
        painter.setPen(QPen(border, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    # ── 选择 ────────────────────────────────────────────────────

    def _sel_rect(self):
        if self._sel_anchor is None or self._sel_current is None:
            return None
        (r0, c0), (r1, c1) = self._sel_anchor, self._sel_current
        if (r0, c0) > (r1, c1):
            (r0, c0), (r1, c1) = (r1, c1), (r0, c0)
        return (r0, c0, r1, c1)

    def _in_sel(self, abs_row, col, rect):
        r0, c0, r1, c1 = rect
        if abs_row < r0 or abs_row > r1:
            return False
        if r0 == r1:
            return c0 <= col <= c1
        if abs_row == r0:
            return col >= c0
        if abs_row == r1:
            return col <= c1
        return True

    def _cell_at(self, pos):
        # 坐标为 QPointF，// 在 Python 下得 float，必须 int()，否则 selected_text 的 range() 崩溃
        col = int((pos.x() - _PAD) // self._cell_w)
        row = int((pos.y() - _PAD) // self._cell_h)
        col = min(self._cols - 1, max(0, col))
        row = min(self._rows - 1, max(0, row))
        return (self._first_abs() + row, col)

    def all_text(self) -> str:
        """导出全部文档（回滚历史 + 当前屏）为纯文本，用于保存报告。"""
        total = self._total_lines()
        out = []
        for r in range(total):
            line = self._doc_line(r)
            chars = []
            c = 0
            while c < self._cols:
                d = line[c].data
                if d != "":
                    chars.append(d)
                c += 1
            out.append("".join(chars).rstrip())
        return "\n".join(out)

    def selected_text(self) -> str:
        rect = self._sel_rect()
        if rect is None:
            return ""
        r0, c0, r1, c1 = rect
        out = []
        for r in range(r0, r1 + 1):
            line = self._doc_line(r)
            cs = c0 if r == r0 else 0
            ce = c1 if r == r1 else self._cols - 1
            chars = []
            c = cs
            while c <= ce and c < self._cols:
                d = line[c].data
                if d != "":
                    chars.append(d)
                c += 1
            out.append("".join(chars).rstrip())
        return "\n".join(out)

    def mousePressEvent(self, e: QMouseEvent):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_timer.stop()
            self._drag_dir = 0
            self._sel_anchor = self._sel_current = self._cell_at(e.position())
            self.setFocus()
            self.update()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent):
        if self._sel_anchor is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self._sel_current = self._cell_at(e.position())
            self._update_drag(e.position().y())
            self.update()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent):
        if e.button() == Qt.MouseButton.LeftButton and self._sel_anchor is not None:
            self._sel_current = self._cell_at(e.position())
            self._drag_timer.stop()
            self._drag_dir = 0
            self.update()
        super().mouseReleaseEvent(e)

    def _update_drag(self, y):
        edge = 24
        if y < edge:
            d = -1
        elif y > self.height() - edge:
            d = 1
        else:
            d = 0
        self._drag_dir = d
        if d and not self._drag_timer.isActive():
            self._drag_timer.start()
        elif not d:
            self._drag_timer.stop()

    def _context_menu(self, pos):
        m = QMenu(self)
        copy_act = m.addAction("复制")
        paste_act = m.addAction("粘贴")
        find_act = m.addAction("查找")
        clear_act = m.addAction("清屏")
        act = m.exec(self.mapToGlobal(pos))
        if act == copy_act:
            self._copy()
        elif act == paste_act:
            self._paste()
        elif act == find_act:
            self._open_find()
        elif act == clear_act:
            self.clear()

    def _copy(self):
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)

    def _paste(self):
        text = QApplication.clipboard().text()
        if text:
            self.sendRequested.emit(text.encode(self._codec, "replace"))

    # ── 检索 ────────────────────────────────────────────────────

    def _open_find(self):
        pre = self.selected_text().replace("\n", " ").strip()
        self._find.open(pre)

    def _close_find(self):
        self._find_refresh_timer.stop()
        self._find.hide()
        self._find_text = ""
        self._matches = []
        self._cur = -1
        self._cur_match = None
        self.setFocus()
        self.update()

    def _on_find_text(self, text: str):
        self._find_text = text
        self._recompute(keep_cur=False)

    def _on_find_case(self, on: bool):
        self._find_case = bool(on)
        self._recompute(keep_cur=False)

    def _recompute(self, keep_cur=False):
        q = self._find_text
        self._matches = []
        if q:
            needle = q if self._find_case else q.lower()
            for absr in range(self._total_lines()):
                line = self._doc_line(absr)
                chars, gcols, widths = [], [], []
                c = 0
                while c < self._cols:
                    d = line[c].data
                    if d != "":
                        chars.append(d)
                        gcols.append(c)
                        widths.append(max(1, _wcw(d) or 1))
                    c += 1
                if not chars:
                    continue
                hay = "".join(chars)
                if not self._find_case:
                    hay = hay.lower()
                s = 0
                L = len(needle)
                while True:
                    i = hay.find(needle, s)
                    if i < 0:
                        break
                    e = i + L
                    cs = gcols[i]
                    ce = gcols[e - 1] + widths[e - 1] - 1
                    self._matches.append((absr, cs, ce))
                    s = e
        n = len(self._matches)
        if keep_cur:
            if n == 0:
                self._set_cur(-1)
            elif self._cur >= n:
                self._set_cur(n - 1)
            else:
                self._set_cur(self._cur)
        else:
            self._set_cur(0 if n else -1)
        if not keep_cur and self._cur >= 0:
            self._scroll_to_match()
        self.update()

    def _set_cur(self, i):
        n = len(self._matches)
        self._cur = i if (n and i >= 0) else -1
        self._cur_match = self._matches[self._cur] if self._cur >= 0 else None
        self._find.set_count(self._cur + 1 if self._cur >= 0 else 0, n)

    def _find_next(self):
        n = len(self._matches)
        if not n:
            return
        self._set_cur((self._cur + 1) % n)
        self._scroll_to_match()
        self.update()

    def _find_prev(self):
        n = len(self._matches)
        if not n:
            return
        self._set_cur((self._cur - 1) % n)
        self._scroll_to_match()
        self.update()

    def _scroll_to_match(self):
        if self._cur < 0:
            return
        self._scroll_to_row(self._matches[self._cur][0])

    def _scroll_to_row(self, r):
        mt = self._max_top()
        if r < self._top_line:
            t = r
        elif r >= self._top_line + self._rows:
            t = r - self._rows + 1
        else:
            t = self._top_line
        t = max(0, min(mt, t))
        self._top_line = t
        self._stick = t >= mt
        self._sync_vbar()

    def _row_marks(self, abs_row):
        if not self._matches:
            return ()
        marks = None
        for m in self._matches:
            if m[0] != abs_row:
                continue
            if marks is None:
                marks = [0] * self._cols
            v = 2 if m == self._cur_match else 1
            lo = max(0, m[1])
            hi = min(self._cols - 1, m[2])
            for cc in range(lo, hi + 1):
                marks[cc] = v if v == 2 else max(marks[cc], 1)
        return marks or ()

    # ── 键盘 ────────────────────────────────────────────────────

    def keyPressEvent(self, e: QKeyEvent):
        from PyQt6.QtCore import Qt as _Qt
        # 检索：检索条开启时 Esc 关闭（不发给设备）；Ctrl+F 打开
        if self._find.isVisible() and e.key() == _Qt.Key.Key_Escape:
            self._close_find()
            return
        if (e.modifiers() & _Qt.KeyboardModifier.ControlModifier) \
                and not (e.modifiers() & _Qt.KeyboardModifier.ShiftModifier) \
                and e.key() == _Qt.Key.Key_F:
            self._open_find()
            return
        # 复制 / 粘贴快捷键
        if e.modifiers() & _Qt.KeyboardModifier.ControlModifier and \
                e.modifiers() & _Qt.KeyboardModifier.ShiftModifier:
            if e.key() == _Qt.Key.Key_C:
                self._copy(); return
            if e.key() == _Qt.Key.Key_V:
                self._paste(); return

        # 翻页
        if e.key() == _Qt.Key.Key_PageUp:
            self._scroll_up(max(1, self._rows - 1)); return
        if e.key() == _Qt.Key.Key_PageDown:
            self._scroll_down(max(1, self._rows - 1)); return

        data = key_event_to_bytes(e.key(), e.modifiers(), e.text(),
                                  self._codec, self._enter_mode)
        if data:
            self.sendRequested.emit(data)
            if self._local_echo:
                self._echo_local(e, data)
            self._jump_bottom()

    def _echo_local(self, e, data):
        from PyQt6.QtCore import Qt as _Qt
        if e.key() in (_Qt.Key.Key_Return, _Qt.Key.Key_Enter):
            self._stream.feed("\r\n")
        elif e.key() == _Qt.Key.Key_Backspace:
            self._stream.feed("\b \b")
        elif e.text() and e.text().isprintable() and not (
                e.modifiers() & _Qt.KeyboardModifier.ControlModifier):
            self._stream.feed(e.text())
        self.update()

    def wheelEvent(self, e):
        d = e.angleDelta().y()
        if d > 0:
            self._scroll_up(3)
        elif d < 0:
            self._scroll_down(3)

    def _toggle_blink(self):
        if self._stick and self.hasFocus():
            self._blink_on = not self._blink_on
            self.update()

    # ── 尺寸 ────────────────────────────────────────────────────

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refit()
        self._find.place()

    def showEvent(self, e):
        super().showEvent(e)
        self._refit()
        self._find.place()

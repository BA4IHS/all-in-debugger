# coding: utf-8
"""终端组件纯逻辑 + 无头绘制测试（无需串口/窗口显示）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QImage, QPainter, QWheelEvent


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------
# 键盘映射
# ---------------------------------------------------------------------------

def _key(name):
    return getattr(Qt.Key, "Key_" + name)


def test_keymap_enter_modes():
    from app.ui.terminal_widget import key_event_to_bytes as k
    assert k(_key("Return"), Qt.KeyboardModifier.NoModifier, "", "UTF-8", "\r") == b"\r"
    assert k(_key("Return"), Qt.KeyboardModifier.NoModifier, "", "UTF-8", "\r\n") == b"\r\n"
    assert k(_key("Return"), Qt.KeyboardModifier.NoModifier, "", "UTF-8", "\n") == b"\n"


def test_keymap_backspace_tab_esc():
    from app.ui.terminal_widget import key_event_to_bytes as k
    assert k(_key("Backspace"), Qt.KeyboardModifier.NoModifier, "", "UTF-8", "\r") == b"\x7f"
    assert k(_key("Tab"), Qt.KeyboardModifier.NoModifier, "", "UTF-8", "\r") == b"\t"
    assert k(_key("Escape"), Qt.KeyboardModifier.NoModifier, "", "UTF-8", "\r") == b"\x1b"


def test_keymap_arrows_and_f1():
    from app.ui.terminal_widget import key_event_to_bytes as k
    assert k(_key("Up"), Qt.KeyboardModifier.NoModifier, "", "UTF-8", "\r") == b"\x1b[A"
    assert k(_key("Home"), Qt.KeyboardModifier.NoModifier, "", "UTF-8", "\r") == b"\x1b[H"
    assert k(_key("F1"), Qt.KeyboardModifier.NoModifier, "", "UTF-8", "\r") == b"\x1bOP"


def test_keymap_ctrl_c():
    from app.ui.terminal_widget import key_event_to_bytes as k
    assert k(_key("C"), Qt.KeyboardModifier.ControlModifier, "", "UTF-8", "\r") == b"\x03"


def test_keymap_printable_and_codec():
    from app.ui.terminal_widget import key_event_to_bytes as k
    assert k(_key("A"), Qt.KeyboardModifier.NoModifier, "a", "UTF-8", "\r") == b"a"
    # 可打印字符走 text 编码（与 key 码无关），验证 GBK 中文
    assert k(_key("A"), Qt.KeyboardModifier.NoModifier, "中", "GBK", "\r") == "中".encode("gbk")


# ---------------------------------------------------------------------------
# 调色板
# ---------------------------------------------------------------------------

def test_resolve_color_named_and_default():
    from app.ui.terminal_widget import resolve_color, DEFAULT_FG, DEFAULT_BG
    assert resolve_color("default", False) == DEFAULT_FG
    assert resolve_color("default", True) == DEFAULT_BG
    assert resolve_color("red", False) == (205, 49, 49)


def test_resolve_color_hex_and_typo():
    from app.ui.terminal_widget import resolve_color
    assert resolve_color("ff8700", False) == (255, 135, 0)
    # pyte 0.8.2 背景亮洋红拼写 bug 必须被兼容
    assert resolve_color("bfightmagenta", True) == resolve_color("brightmagenta", True)


# ---------------------------------------------------------------------------
# pyte 屏幕：回滚 + 宽字符
# ---------------------------------------------------------------------------

def test_scrollback_captures_scrolled_lines():
    from app.ui.terminal_widget import TermScreen
    from pyte import Stream
    s = TermScreen(10, 3, scrollback=100)
    Stream(s).feed("L1\r\nL2\r\nL3\r\nL4\r\nL5\r\n")
    texts = ["".join(ch.data for ch in row).rstrip() for row in s.scrollback]
    assert texts == ["L1", "L2", "L3"]
    # 当前可见区应包含最后一行 L5
    visible = "\n".join(
        "".join(s.buffer.get(r, {}).get(c, s.default_char).data for c in range(10))
        for r in range(3))
    assert "L5" in visible and "L4" in visible


def test_wide_char_placeholder():
    from app.ui.terminal_widget import TermScreen
    from pyte import Stream
    s = TermScreen(10, 2)
    Stream(s).feed("中X")
    assert s.buffer[0][0].data == "中"
    assert s.buffer[0][1].data == ""     # 右半占位
    assert s.buffer[0][2].data == "X"


# ---------------------------------------------------------------------------
# 控件：喂数据 / 选择 / 无头绘制
# ---------------------------------------------------------------------------

def test_feed_and_draw_no_crash(qapp):
    from app.ui.terminal_widget import QTerminalWidget
    w = QTerminalWidget()
    w.feed_bytes(b"\x1b[31mHI\x1b[0m")
    assert w._screen.buffer[0][0].data == "H"
    assert w._screen.buffer[0][0].fg == "red"
    img = QImage(320, 240, QImage.Format.Format_ARGB32)
    p = QPainter(img)
    w._draw(p)        # 渲染路径不抛异常
    p.end()


def test_queued_terminal_feed_is_split_across_event_turns(qapp):
    from app.ui.terminal_widget import QTerminalWidget, _QUEUED_FEED_CHUNK

    w = QTerminalWidget()
    payload = b"A" * (_QUEUED_FEED_CHUNK * 3)
    w.queue_bytes(payload)
    assert w.queued_byte_count() == len(payload)

    # 单次定时回调只解析一个小块，其余内容留给后续事件循环，
    # 因而键盘和窗口事件可以穿插执行。
    w._feed_timer.stop()
    w._drain_feed_queue()
    w._feed_timer.stop()
    assert w.queued_byte_count() == len(payload) - _QUEUED_FEED_CHUNK
    assert w._screen.buffer[0][0].data == "A"
    w.discard_queued_bytes()
    assert w.queued_byte_count() == 0


def test_queued_terminal_feed_keeps_keyboard_responsive(qapp):
    import time
    from PyQt6.QtCore import QEvent, QTimer
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtWidgets import QApplication
    from app.ui.terminal_widget import QTerminalWidget

    w = QTerminalWidget()
    sent = []
    w.sendRequested.connect(
        lambda data: sent.append((bytes(data), w.queued_byte_count())))
    w.queue_bytes(b"X" * (512 * 1024))
    QTimer.singleShot(
        20,
        lambda: QApplication.sendEvent(
            w, QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_A,
                Qt.KeyboardModifier.NoModifier, "a")))

    end = time.time() + 2
    while time.time() < end and not sent:
        qapp.processEvents()

    assert sent and sent[0][0] == b"a"
    assert sent[0][1] > 0, "键盘直到全部输出处理完后才响应"
    w.discard_queued_bytes()


def test_selection_text(qapp):
    from app.ui.terminal_widget import QTerminalWidget
    w = QTerminalWidget()
    w.feed_bytes(b"ABCD")
    w._sel_anchor = (0, 0)
    w._sel_current = (0, 2)
    assert w.selected_text() == "ABC"


def _make_term(qapp):
    from app.ui.terminal_widget import QTerminalWidget
    w = QTerminalWidget()
    w.resize(400, 240)
    w._refit()
    return w


def _fill(w, n):
    w.feed_bytes(("\n".join(f"L{i:03d}" for i in range(n)) + "\n").encode())


def test_stick_follows_then_holds(qapp):
    w = _make_term(qapp)
    _fill(w, w._rows + 30)
    assert w._stick is True
    assert w._top_line == w._max_top() and w._max_top() > 0
    w._scroll_up(5)
    assert w._stick is False
    held = w._top_line
    w.feed_bytes(b"X\n")              # 上翻时新数据不得拽走视图
    assert w._top_line == held
    w._scroll_down(99999)
    assert w._stick is True


def test_vbar_sync_and_jump(qapp):
    w = _make_term(qapp)
    _fill(w, w._rows + 20)
    w._scroll_up(4)
    assert w._vbar.value() == w._top_line
    w._on_vbar(0)                     # 模拟用户拖滚动条到顶
    assert w._top_line == 0 and w._stick is False
    w._jump_bottom()
    assert w._stick is True and w._top_line == w._max_top()


def test_drag_scroll_extends_selection(qapp):
    w = _make_term(qapp)
    _fill(w, w._rows + 20)
    w._jump_bottom()
    w._sel_anchor = (w._first_abs() + 2, 0)
    w._sel_current = (w._first_abs() + 2, 5)
    w._drag_dir = -1
    before = w._first_abs()
    w._drag_scroll_step()
    assert w._first_abs() == before - 1
    assert w._sel_current == (w._first_abs(), 0)


def test_find_matches_and_count(qapp):
    w = _make_term(qapp)
    w.feed_bytes(b"hello world\r\nfoo hello bar\r\n")
    w._on_find_text("hello")
    assert w._matches == [(0, 0, 4), (1, 4, 8)]
    assert w._cur == 0
    assert w._row_marks(0)[0] == 2 and w._row_marks(0)[4] == 2 and w._row_marks(0)[5] == 0
    assert w._row_marks(1)[4] == 1     # 非当前匹配


def test_find_case_sensitive(qapp):
    w = _make_term(qapp)
    w.feed_bytes(b"Hello hello\r\n")
    w._on_find_text("Hello")
    assert len(w._matches) == 2
    w._on_find_case(True)
    assert len(w._matches) == 1 and w._matches[0] == (0, 0, 4)


def test_find_wide_char_columns(qapp):
    w = _make_term(qapp)
    w.feed_bytes("中abc中文".encode("utf-8"))
    w._on_find_text("中文")
    assert w._matches == [(0, 5, 8)]


def test_find_next_prev_wrap(qapp):
    w = _make_term(qapp)
    w.feed_bytes(b"x\nx\nx\n")
    w._on_find_text("x")
    assert w._cur == 0
    w._find_next(); w._find_next(); w._find_next()   # 0->1->2->0
    assert w._cur == 0
    w._find_prev()                                    # 0->2
    assert w._cur == 2


def test_find_scrolls_to_match(qapp):
    w = _make_term(qapp)
    w.feed_bytes(("FIRST\n" + "\n".join(f"L{i}" for i in range(60)) + "\n").encode())
    w._jump_bottom()
    assert w._top_line > 0
    w._on_find_text("FIRST")
    assert w._top_line == 0 and w._stick is False


def test_find_open_close(qapp):
    w = _make_term(qapp)
    w._open_find()
    assert not w._find.isHidden()
    w._close_find()
    assert w._find.isHidden() and w._matches == []


def test_right_scrollbar_layout_and_gutter(qapp):
    w = _make_term(qapp)
    assert w._gx == 12
    assert w._vbar.width() == w._gx
    assert w._vbar.geometry().height() == w.height()
    assert w._vbar.geometry().left() == w.width() - w._gx
    # 文本从左侧内边距开始；落在右侧滚动条槽内时应夹紧到最后一列。
    assert w._cell_at(QPointF(2, 20))[1] == 0
    assert w._cell_at(QPointF(w.width() - 2, 20))[1] == w._cols - 1


def test_all_scrollbars_use_translucent_style(qapp):
    from PyQt6.QtWidgets import QScrollBar
    from qfluentwidgets import ScrollArea
    from app.ui.scrollbar_style import (
        _native_qss, _fluent_colors, apply_white_scrollbars,
        install_white_scrollbars,
    )

    install_white_scrollbars(qapp)
    # 主题自适应：深色半透明白 / 浅色半透明黑
    assert _native_qss().strip() in qapp.styleSheet()

    handle, _arrow, _groove = _fluent_colors()
    handle_alpha = handle.alpha()
    rgb = f"{handle.red()}, {handle.green()}, {handle.blue()}"
    assert rgb in qapp.styleSheet()

    area = ScrollArea()
    apply_white_scrollbars(area)
    delegate = area.scrollDelagate
    for bar in (delegate.vScrollBar, delegate.hScrollBar):
        assert bar.handle.lightColor.alpha() == handle_alpha
        assert bar.handle.darkColor.alpha() == handle_alpha
        assert bar.handle.lightColor.red() == handle.red()
        assert bar.handle.darkColor.red() == handle.red()


def test_find_bar_does_not_cover_right_scrollbar(qapp):
    w = _make_term(qapp)
    w.resize(800, 240)
    w._refit()
    w._open_find()
    assert w._find.geometry().right() < w._vbar.geometry().left()


def test_mouse_selection_copy_no_float_crash(qapp):
    """鼠标坐标经 _cell_at 必须为 int，否则 selected_text 的 range() 崩溃。"""
    from app.ui.terminal_widget import QTerminalWidget
    w = QTerminalWidget()
    w.feed_bytes(b"HELLO WORLD")
    ox = 8
    w._sel_anchor = w._cell_at(QPointF(ox + 0.2, 9.3))
    w._sel_current = w._cell_at(QPointF(ox + 4.8 * w._cell_w, 9.3))
    assert all(isinstance(v, int) for v in w._sel_anchor)
    assert all(isinstance(v, int) for v in w._sel_current)
    # 复制路径不得抛 TypeError
    assert "HELLO" in w.selected_text()


def test_log_find_matches_navigation_and_close(qapp):
    from app.ui.searchable_text_edit import SearchablePlainTextEdit
    w = SearchablePlainTextEdit()
    w.setPlainText("Hello hello\nhello")
    w._on_find_text("hello")
    assert len(w._matches) == 3 and w._current == 0
    w._find_prev()
    assert w._current == 2
    w._on_find_case(True)
    assert len(w._matches) == 2
    w._close_find()
    assert not w._matches and w.extraSelections() == []


def test_log_find_recomputes_when_text_appends(qapp):
    from app.ui.searchable_text_edit import SearchablePlainTextEdit
    w = SearchablePlainTextEdit()
    w.setPlainText("needle")
    w._on_find_text("needle")
    assert len(w._matches) == 1
    w.appendPlainText("needle")
    assert len(w._matches) == 2


def test_log_wheel_scrolls_immediately_and_pauses_follow(qapp):
    from PyQt6.QtWidgets import QApplication
    from app.ui.receive_panel import ReceivePanel
    from qfluentwidgets import SmoothMode

    panel = ReceivePanel()
    panel._ctrl._timer.stop()
    panel._countTimer.stop()
    panel.resize(500, 260)
    panel.show()
    try:
        panel.view.setPlainText("\n".join(f"line {i}" for i in range(200)))
        qapp.processEvents()

        assert panel.view.scrollDelegate.verticalSmoothScroll.smoothMode \
            == SmoothMode.NO_SMOOTH
        sb = panel.view.verticalScrollBar()
        sb.setValue(sb.maximum())
        before = sb.value()
        wheel_up = QWheelEvent(
            QPointF(20, 20), QPointF(20, 20), QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(panel.view.viewport(), wheel_up)

        assert sb.value() < before
        assert panel._ctrl._autoScroll is False
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_connect_panel_infobar_uses_wide_anchor(qapp):
    from PyQt6.QtWidgets import QWidget
    from qfluentwidgets import InfoBar
    from app.ui.connect_panel import ConnectPanel

    panel = ConnectPanel()
    anchor = QWidget()
    panel.setInfoBarParent(anchor)
    try:
        panel.setOpenFailed("test error")
        qapp.processEvents()
        assert anchor.findChildren(InfoBar)
        assert not panel.findChildren(
            InfoBar, options=Qt.FindChildOption.FindDirectChildrenOnly)
    finally:
        anchor.close()
        panel.close()
        anchor.deleteLater()
        panel.deleteLater()
        qapp.processEvents()


def test_log_disabled_follow_preserves_position_on_append(qapp):
    from app.ui.receive_panel import ReceivePanel

    panel = ReceivePanel()
    panel._ctrl._timer.stop()
    panel._countTimer.stop()
    panel.resize(500, 260)
    panel.show()
    try:
        panel.view.setPlainText("\n".join(f"line {i}" for i in range(200)))
        qapp.processEvents()

        panel.setAutoScroll(False)
        sb = panel.view.verticalScrollBar()
        sb.setValue(sb.maximum())
        old_value, old_max = sb.value(), sb.maximum()
        panel._ctrl.feed(b"new line", 0)
        panel._ctrl._flush()

        assert sb.maximum() > old_max
        assert sb.value() == old_value
        assert panel._ctrl._autoScroll is False
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_adb_navigation_icon_and_period_width(qapp):
    from PyQt6.QtGui import QIcon
    from qfluentwidgets import Theme
    from app.ui.main_window import (
        ANDROID_ICON, ANDROID_ICON_DARK_PATH, ANDROID_ICON_PATH,
    )
    from app.ui.send_panel import SendPanel

    assert ANDROID_ICON_PATH.is_file()
    assert ANDROID_ICON_DARK_PATH.is_file()
    assert not QIcon(str(ANDROID_ICON_PATH)).isNull()
    assert ANDROID_ICON.path(Theme.LIGHT) == str(ANDROID_ICON_PATH)
    assert ANDROID_ICON.path(Theme.DARK) == str(ANDROID_ICON_DARK_PATH)

    panel = SendPanel()
    try:
        assert panel.intervalSpin.width() == 180
        panel.intervalSpin.setValue(86_400_000)
        assert panel.intervalSpin.value() == 86_400_000
    finally:
        panel.stopPeriodic()
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_window_is_centered_in_available_screen(qapp):
    from PyQt6.QtWidgets import QWidget
    from app.ui.main_window import center_window

    window = QWidget()
    window.resize(400, 300)
    screen = qapp.primaryScreen()
    center_window(window, screen)
    assert window.frameGeometry().center() == screen.availableGeometry().center()
    window.deleteLater()
    qapp.processEvents()


def test_resize_with_scrollback_no_crash(qapp):
    """回滚快照列宽与当前列宽不一致时，绘制/选择不得越界崩溃。"""
    from app.ui.terminal_widget import QTerminalWidget
    w = QTerminalWidget()
    w.feed_bytes(b"\r\n".join(f"L{i:02d}xxxx".encode() for i in range(40)))
    assert len(w._screen.scrollback) > 0
    img = QImage(640, 480, QImage.Format.Format_ARGB32)
    for cols in (200, 5, 80):      # 比快照宽 / 比快照窄 / 相等
        w._cols = cols
        p = QPainter(img)
        w._draw(p)
        p.end()
        w._sel_anchor = (0, 0)
        w._sel_current = (0, min(cols - 1, 3))
        w.selected_text()         # 不得抛 IndexError
    w._sel_anchor = w._sel_current = None


def test_codec_switch_resets_decoder(qapp):
    from app.ui.terminal_widget import QTerminalWidget
    w = QTerminalWidget()
    w.set_codec("GBK")
    w.feed_bytes("中".encode("gbk")[:1])   # 半个字符
    w.feed_bytes("中".encode("gbk")[1:])
    assert w._screen.buffer[0][0].data == "中"


# ---------------------------------------------------------------------------
# 端到端：worker loop:// -> 终端渲染
# ---------------------------------------------------------------------------

def _wait(pred, qapp, ms=3000):
    import time
    end = time.time() + ms / 1000
    while time.time() < end:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.005)
    return False


def test_terminal_via_loopback(qapp):
    from app import serial_utils as su
    from app.serial_worker import SerialThread
    from app.ui.terminal_widget import QTerminalWidget

    st = SerialThread()
    w = QTerminalWidget()
    opened = []
    st.worker.portOpened.connect(opened.append)
    st.worker.dataReceived.connect(w.feed_bytes)
    st.start()

    cfg = su.build_open_config("loop://", "9600", "8", "1", "None", "None", True, False)
    st.sigOpen.emit(cfg)
    assert _wait(lambda: opened, qapp)

    st.sigWrite.emit(b"\x1b[32mOK\x1b[0m")
    assert _wait(lambda: w._screen.buffer[0][0].data == "O", qapp)
    assert w._screen.buffer[0][0].fg == "green"
    assert w._screen.buffer[0][1].data == "K"

    st.sigClose.emit()
    st.stop()

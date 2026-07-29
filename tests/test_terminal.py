# coding: utf-8
"""终端组件纯逻辑 + 无头绘制测试（无需串口/窗口显示）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import Qt
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QImage, QPainter


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


def test_selection_text(qapp):
    from app.ui.terminal_widget import QTerminalWidget
    w = QTerminalWidget()
    w.feed_bytes(b"ABCD")
    w._sel_anchor = (0, 0)
    w._sel_current = (0, 2)
    assert w.selected_text() == "ABC"


def test_mouse_selection_copy_no_float_crash(qapp):
    """鼠标坐标经 _cell_at 必须为 int，否则 selected_text 的 range() 崩溃。"""
    from app.ui.terminal_widget import QTerminalWidget
    w = QTerminalWidget()
    w.feed_bytes(b"HELLO WORLD")
    w._sel_anchor = w._cell_at(QPointF(12.7, 9.3))
    w._sel_current = w._cell_at(QPointF(80.1, 9.3))
    assert all(isinstance(v, int) for v in w._sel_anchor)
    assert all(isinstance(v, int) for v in w._sel_current)
    # 复制路径不得抛 TypeError
    assert "HELLO" in w.selected_text()


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

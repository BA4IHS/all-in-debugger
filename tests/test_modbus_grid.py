# coding: utf-8
"""Modbus 网格数据表纯函数测试：地址换算与双字编解码。"""
import math
import struct

import pytest

from app.ui.modbus_page import (
    ROWS_PER_GROUP, cell_address, format_ascii_pair,
    format_value, parse_ascii_pair, parse_float_pair,
)


class TestCellAddress:
    """格子 (行, 列) ↔ 绝对地址换算。"""

    def test_origin(self):
        assert cell_address(0, 0, 0) == 0

    def test_row_offset(self):
        assert cell_address(100, 7, 0) == 107

    def test_column_group(self):
        # 第 3 列（c=2）第 5 行：100 + 2*10 + 4
        assert cell_address(100, 4, 2) == 124

    def test_nonzero_base(self):
        assert cell_address(40001, 9, 0) == 40010

    def test_roundtrip_all_cells(self):
        """任意起始地址读 n 个寄存器，网格每格地址唯一且连续。"""
        base, n = 33, 47
        cols = math.ceil(n / ROWS_PER_GROUP)
        addrs = set()
        for c in range(cols):
            for r in range(ROWS_PER_GROUP):
                idx = c * ROWS_PER_GROUP + r
                if idx < n:
                    addrs.add(cell_address(base, r, c))
        assert addrs == set(range(base, base + n))


class TestFloatPair:
    """Float 双字编解码互逆。"""

    @pytest.mark.parametrize("v", [0.0, 1.0, -1.0, 3.14, 12345.678, 1e-30])
    def test_roundtrip(self, v):
        hi, lo = parse_float_pair(str(v))
        assert 0 <= hi <= 0xFFFF and 0 <= lo <= 0xFFFF
        # 二进制编码往返无损；显示经 %.6g 截断只校验相对精度
        word = ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
        decoded = struct.unpack(">f", struct.pack(">I", word))[0]
        assert decoded == pytest.approx(v, rel=1e-6)

    def test_big_endian_order(self):
        hi, lo = parse_float_pair("1.0")
        word = ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
        assert struct.pack(">I", word) == struct.pack(">f", 1.0)

    def test_invalid_text(self):
        with pytest.raises(ValueError):
            parse_float_pair("abc")


class TestAsciiPair:
    """ASCII 双字编解码互逆。"""

    def test_roundtrip_4chars(self):
        hi, lo = parse_ascii_pair("ABCD")
        assert format_ascii_pair(hi, lo) == "ABCD"

    def test_short_text_padded(self):
        hi, lo = parse_ascii_pair("AB")
        # 不足 4 字符补 \x00，显示为 "."
        assert format_ascii_pair(hi, lo) == "AB.."

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_ascii_pair("")

    def test_truncate_over_4(self):
        hi, lo = parse_ascii_pair("ABCDEF")
        assert format_ascii_pair(hi, lo) == "ABCD"


class TestFormatValue:
    """单词格式显示。"""

    def test_unsigned(self):
        assert format_value(0xFFFF, "Unsigned") == "65535"

    def test_signed_negative(self):
        assert format_value(0xFFFF, "Signed") == "-1"

    def test_signed_positive(self):
        assert format_value(0x7FFF, "Signed") == "32767"

    def test_hex(self):
        assert format_value(0x1A2B, "Hex") == "1A2B"

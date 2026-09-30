"""图表位图的**纯函数**边界（0105 安全审查：解压炸弹）。

只测「拿字节 → 出字节/整数」这两个不碰库的静态方法，因此不经 API 链路；
**凡是需要 DB 的行为（归属校验、总量累加）都在
``app/tests/integration/test_session_export_charts.py`` 里走真 PG + 完整 API**。

为什么单独一个文件：``session_history_service`` 此前没有单元测试文件（``unit/
test_history_service.py`` 测的是同名的 ``history_service`` —— KPI 快照，另一个模块），
而这两个函数的失败模式（解压炸弹、畸形头）与「导出流程」无关，塞进集成文件会让
它们必须连库才能跑。
"""

from __future__ import annotations

import base64
import struct
import zlib

import pytest

from app.domain.exceptions import ValidationError
from app.services.session_history_service import (
    _CHART_IMAGE_DATA_URL_PREFIX,
    _MAX_EXPORT_IMAGE_PIXELS,
    SessionHistoryService,
)
from app.tests._png_support import pngDeclaringSize, solidPng

# PIL 默认的 MAX_IMAGE_PIXELS（超过它才警告，超过 2 倍才抛异常）
_PIL_DEFAULT_MAX_PIXELS = 89_478_485


def _pngWithHeaderDims(width: int, height: int) -> bytes:
    """只有签名 + 一个合法 IHDR 的最小 PNG 头（用来构造畸形宽高）。

    不走 ``_png_support.pngDeclaringSize``：那个 helper 会拒绝 0 尺寸，
    而 0 尺寸正是这里要测的分支。
    """
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    )


def _dataUrl(png: bytes) -> str:
    return _CHART_IMAGE_DATA_URL_PREFIX + base64.b64encode(png).decode()


class TestPngPixelCount:
    def test_reads_real_dimensions(self) -> None:
        assert SessionHistoryService._pngPixelCount(solidPng(400, 300)) == 120_000

    def test_reads_declared_dimensions_not_actual(self) -> None:
        """解压炸弹的判据必须是 **IHDR 声明值** —— 那才是 Pillow 会去分配的尺寸。

        这张图的实际像素数据只有 8×8。如果实现误用「实际解出来的大小」，闸就永远
        拦不住炸弹（炸弹的真实数据同样很小，只是压缩后也小）。
        """
        png = pngDeclaringSize(12000, 12000)

        assert SessionHistoryService._pngPixelCount(png) == 144_000_000
        # 前提：它确实小到能通过 2 MiB 的字节闸，否则测的不是像素闸
        assert len(png) < 2 * 1024 * 1024

    def test_non_ihdr_header_returns_none(self) -> None:
        """签名对但第一个块不是 IHDR（非 PNG 或畸形）。"""
        junk = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 4) + b"IDAT" + b"\x00" * 20

        assert SessionHistoryService._pngPixelCount(junk) is None

    def test_truncated_header_returns_none(self) -> None:
        assert SessionHistoryService._pngPixelCount(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0d") is None

    @pytest.mark.parametrize("width,height", [(0, 8), (8, 0), (0, 0)])
    def test_zero_dimension_returns_none(self, width: int, height: int) -> None:
        """0 是畸形的（PNG 规范要求 ≥1）：照 None 处理，别让 `0×N` 绕过像素闸。"""
        assert SessionHistoryService._pngPixelCount(_pngWithHeaderDims(width, height)) is None


class TestDecodeChartImage:
    def test_returns_bytes_for_valid_png(self) -> None:
        png = solidPng(8, 8)

        decoded = SessionHistoryService._decodeChartImage(_dataUrl(png))

        assert decoded == png

    def test_rejects_missing_prefix(self) -> None:
        with pytest.raises(ValidationError):
            SessionHistoryService._decodeChartImage("iVBORw0KGgo=")

    def test_rejects_non_base64_payload(self) -> None:
        with pytest.raises(ValidationError):
            SessionHistoryService._decodeChartImage(
                _CHART_IMAGE_DATA_URL_PREFIX + "!!!not base64!!!"
            )

    def test_rejects_payload_over_byte_limit_before_decoding(self) -> None:
        """先按长度反推、再解码：反过来等于让超长串先把内存占住，上限就成了摆设。"""
        oversized = _CHART_IMAGE_DATA_URL_PREFIX + "A" * (4 * 1024 * 1024)

        with pytest.raises(ValidationError):
            SessionHistoryService._decodeChartImage(oversized)

    # 「魔数对但 IHDR 读不出来 → 422」不在这一层：``_decodeChartImage`` 只管
    # 前缀/大小/魔数，IHDR 可读性由 ``resolveChartImages`` 调 ``_pngPixelCount``
    # 时判（见 TestPngPixelCount.test_truncated_header_returns_none 与集成
    # test_rejects_png_with_unreadable_ihdr）。


def test_pixel_cap_is_below_pillows_own_threshold() -> None:
    """这道闸之所以必须自己设：PIL 默认 MAX_IMAGE_PIXELS 要到 **2 倍**才抛异常，
    中间那一大段（含 144M px 的典型炸弹）完全不设防 —— 不能把防炸弹外包给 Pillow。"""
    assert _MAX_EXPORT_IMAGE_PIXELS < 2 * _PIL_DEFAULT_MAX_PIXELS

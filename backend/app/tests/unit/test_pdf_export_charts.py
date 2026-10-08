"""导出 PDF 的图表渲染单测（0105，图表进最终报告）。

覆盖三档降级（真图 / 原生 table·kpi / 占位框）、位图解码的四道闸（前缀/上限/
魔数/base64）、以及 payload 挂图后的不可变性。对外契约（HTTP 状态码、跨 session
归属）属于集成层，见 ``app/tests/integration/test_session_export_charts.py``。
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from reportlab.platypus import Image as PdfImage
from reportlab.platypus import Paragraph, Table

from app.domain.exceptions import ValidationError
from app.services.pdf_export_service import (
    ChatExportPayload,
    ChatExportPdfBuilder,
    ChatExportTurn,
)
from app.services.session_history_service import (
    _CHART_IMAGE_DATA_URL_PREFIX,
    _MAX_EXPORT_IMAGE_BYTES,
    SessionHistoryService,
)
from app.tests._png_support import solidPng

_ASST_TIME = datetime(2026, 1, 1, 10, 1, tzinfo=UTC)


def _turn(**overrides) -> ChatExportTurn:
    """最小可渲染轮次；只覆盖本文件关心的图表字段。"""
    base = {
        "user_content": "问",
        "user_time": _ASST_TIME,
        "assistant_content": "答",
        "assistant_time": _ASST_TIME,
    }
    return ChatExportTurn(**{**base, **overrides})


def _dataUrl(png: bytes) -> str:
    return _CHART_IMAGE_DATA_URL_PREFIX + base64.b64encode(png).decode()


class TestDecodeChartImage:
    def test_accepts_png_data_url(self) -> None:
        # Arrange
        png = solidPng(8, 8)

        # Act
        decoded = SessionHistoryService._decodeChartImage(_dataUrl(png))

        # Assert
        assert decoded == png

    def test_rejects_missing_data_url_prefix(self) -> None:
        """裸 base64（没有 data:image/png;base64, 前缀）不算合法输入。"""
        png = solidPng(8, 8)
        with pytest.raises(ValidationError):
            SessionHistoryService._decodeChartImage(
                base64.b64encode(png).decode()
            )

    def test_rejects_non_png_payload_with_png_prefix(self) -> None:
        """只信前缀等于允许「任意二进制贴个 PNG 标签」送进来 —— 魔数必须校验。"""
        notPng = base64.b64encode(b"PK\x03\x04 this is a zip").decode()
        with pytest.raises(ValidationError):
            SessionHistoryService._decodeChartImage(
                _CHART_IMAGE_DATA_URL_PREFIX + notPng
            )

    def test_rejects_invalid_base64(self) -> None:
        with pytest.raises(ValidationError):
            SessionHistoryService._decodeChartImage(
                _CHART_IMAGE_DATA_URL_PREFIX + "!!!not base64!!!"
            )

    def test_rejects_image_over_per_image_limit(self) -> None:
        """超限要在**解码之前**按 base64 长度判掉，否则上限挡不住内存占用。"""
        oversize = base64.b64encode(b"\x00" * (_MAX_EXPORT_IMAGE_BYTES + 1)).decode()
        with pytest.raises(ValidationError):
            SessionHistoryService._decodeChartImage(
                _CHART_IMAGE_DATA_URL_PREFIX + oversize
            )


class TestChartFlowables:
    def test_table_kind_renders_native_table_not_image(self) -> None:
        """table 由服务端原生画：文字比位图清晰，且前端截图失败也照样出。

        必须钉**单元格内容**，不能只钉「是个 Table」：占位框自己也是个单格
        Table，只断言类型的话，表格分支挂掉退回占位框同样通过。
        """
        # Arrange
        turn = _turn(
            chart_type="table",
            chart_option={
                "columns": ["地区", "销量"],
                "rows": [{"地区": "华北", "销量": 100}],
            },
            chart_image=solidPng(8, 8),
        )

        # Act
        flowables = ChatExportPdfBuilder()._chart_flowables(turn)

        # Assert
        assert isinstance(flowables[0], Table)
        assert not any(isinstance(f, PdfImage) for f in flowables)
        body = [
            [cell.text for cell in row] for row in flowables[0]._cellvalues
        ]
        assert body == [["地区", "销量"], ["华北", "100"]]

    def test_truncated_table_carries_row_disclosure(self) -> None:
        """落库时截了行就必须如实说，不假装是全部。"""
        turn = _turn(
            chart_type="table",
            chart_option={
                "columns": ["地区"],
                "rows": [{"地区": "华北"}],
                "truncated": True,
            },
        )

        flowables = ChatExportPdfBuilder()._chart_flowables(turn)

        assert isinstance(flowables[0], Table)
        assert "仅显示前 1 行" in flowables[1].text

    def test_table_without_columns_falls_back_to_placeholder(self) -> None:
        turn = _turn(chart_type="table", chart_option={"columns": [], "rows": []})

        flowables = ChatExportPdfBuilder()._chart_flowables(turn)

        assert isinstance(flowables[0], Table)
        assert "图表类型: table" in flowables[0]._cellvalues[0][0].text

    def test_table_values_are_html_escaped(self) -> None:
        """业务库里的值可能带尖括号：不转义会污染排版，非法标签还会直接抛异常。"""
        turn = _turn(
            chart_type="table",
            chart_option={
                "columns": ["名称"],
                "rows": [{"名称": "<img src=x onerror=alert(1)>"}],
            },
        )

        flowables = ChatExportPdfBuilder()._chart_flowables(turn)

        # 值以转义后的字面量渲染，未被当成标签
        cell = flowables[0]._cellvalues[1][0]
        assert "&lt;img" in cell.text

    def test_kpi_kind_renders_native_text_block(self) -> None:
        turn = _turn(
            chart_type="kpi",
            chart_option={
                "kpi": {"label": "库存周转率", "value": 3.5, "unit": "次", "delta": -0.25}
            },
        )

        flowables = ChatExportPdfBuilder()._chart_flowables(turn)

        assert isinstance(flowables[0], Paragraph)
        assert "库存周转率" in flowables[0].text
        assert "3.5" in flowables[0].text

    def test_kpi_without_payload_falls_back_to_placeholder(self) -> None:
        turn = _turn(chart_type="kpi", chart_option={"kpi": None})

        flowables = ChatExportPdfBuilder()._chart_flowables(turn)

        assert "图表类型: kpi" in flowables[0]._cellvalues[0][0].text

    def test_echarts_kind_uses_uploaded_bitmap(self) -> None:
        # Arrange
        png = solidPng(8, 8)
        turn = _turn(chart_type="bar", chart_option={"series": []}, chart_image=png)

        # Act
        flowables = ChatExportPdfBuilder()._chart_flowables(turn)

        # Assert
        assert len(flowables) == 1
        assert isinstance(flowables[0], PdfImage)

    def test_wide_bitmap_is_scaled_to_content_width(self) -> None:
        """宽图必须缩到正文可容宽度，否则溢出页边距被裁掉。"""
        builder = ChatExportPdfBuilder()
        turn = _turn(chart_type="line", chart_image=solidPng(4000, 100))

        image = builder._chart_flowables(turn)[0]

        expected = (builder._PAGE_WIDTH - 2 * builder._MARGIN) * 0.7
        assert image.drawWidth == pytest.approx(expected)
        # 等比：高度按同一比例缩，不变形
        assert image.drawHeight == pytest.approx(100 * expected / 4000)

    def test_tall_bitmap_is_scaled_to_content_height(self) -> None:
        """窄高图必须**按高度**缩 —— 只夹宽度的话它宽度合规、高度溢出页面，
        reportlab 会在 ``doc.build()`` 里抛 ``LayoutError``（那已经出了 ``_chart_
        image_flowable`` 的 try），整份导出变 500 而不是降级成占位框。"""
        builder = ChatExportPdfBuilder()
        turn = _turn(chart_type="line", chart_image=solidPng(100, 4000))

        image = builder._chart_flowables(turn)[0]

        expected = (builder._PAGE_HEIGHT - 2 * builder._MARGIN) * 0.7
        assert image.drawHeight == pytest.approx(expected)
        # 等比：宽度按同一比例缩，不变形
        assert image.drawWidth == pytest.approx(100 * expected / 4000)

    def test_tall_bitmap_still_builds_a_pdf(self) -> None:
        """端到端兜底：真的把这张窄高图 `build()` 一遍，不许抛 ``LayoutError``。"""
        turn = _turn(chart_type="line", chart_image=solidPng(100, 4000))
        payload = ChatExportPayload(
            session_id="s-tall",
            title="t",
            generated_at=_ASST_TIME,
            turns=[turn],
        )

        pdf = ChatExportPdfBuilder().build(payload)

        assert pdf.startswith(b"%PDF-")

    def test_small_bitmap_is_not_upscaled(self) -> None:
        """小于正文框的图保持原尺寸 —— 放大只会糊，且会改变既有观感。"""
        builder = ChatExportPdfBuilder()
        turn = _turn(chart_type="line", chart_image=solidPng(200, 100))

        image = builder._chart_flowables(turn)[0]

        assert image.drawWidth == pytest.approx(200)
        assert image.drawHeight == pytest.approx(100)

    def test_undecodable_bitmap_falls_back_to_placeholder(self) -> None:
        """解不开的字节会让 reportlab 在 build 时抛异常，把整份 PDF 带走。"""
        turn = _turn(chart_type="bar", chart_image=b"not a png at all")

        flowables = ChatExportPdfBuilder()._chart_flowables(turn)

        assert "图表类型: bar" in flowables[0]._cellvalues[0][0].text

    def test_echarts_kind_without_bitmap_keeps_placeholder(self) -> None:
        """存量消息（0105 之前的行）没有图也没有图负载 —— 退回占位框而不是消失。"""
        turn = _turn(chart_type="donut", chart_option=None, chart_image=None)

        flowables = ChatExportPdfBuilder()._chart_flowables(turn)

        assert "图表类型: donut" in flowables[0]._cellvalues[0][0].text


class TestAttachChartImages:
    def test_attaches_bitmap_to_matching_turn_only(self) -> None:
        # Arrange
        png = solidPng(8, 8)
        payload = ChatExportPayload(
            session_id="s1",
            title="t",
            generated_at=_ASST_TIME,
            turns=[_turn(message_id=1), _turn(message_id=2)],
        )

        # Act
        merged = SessionHistoryService.attachChartImages(payload, {2: png})

        # Assert
        assert merged.turns[0].chart_image is None
        assert merged.turns[1].chart_image == png

    def test_does_not_mutate_input_payload(self) -> None:
        """不可变：调用方手里那份（未挂图）还要用于其他判断，不能被打洞。"""
        png = solidPng(8, 8)
        payload = ChatExportPayload(
            session_id="s1", title="t", generated_at=_ASST_TIME, turns=[_turn(message_id=1)]
        )

        SessionHistoryService.attachChartImages(payload, {1: png})

        assert payload.turns[0].chart_image is None

    def test_ignores_bitmap_for_unknown_message_id(self) -> None:
        """多出来一张没人要的图不该让导出失败（导出是主功能，图是增强）。"""
        payload = ChatExportPayload(
            session_id="s1", title="t", generated_at=_ASST_TIME, turns=[_turn(message_id=1)]
        )

        merged = SessionHistoryService.attachChartImages(payload, {999: b"x"})

        assert merged.turns[0].chart_image is None

    def test_no_bitmaps_returns_equivalent_payload(self) -> None:
        payload = ChatExportPayload(
            session_id="s1", title="t", generated_at=_ASST_TIME, turns=[_turn(message_id=1)]
        )

        assert SessionHistoryService.attachChartImages(payload, {}) == payload


class TestBuildPdfWithCharts:
    def test_build_embeds_bitmap_and_produces_valid_pdf(self) -> None:
        # Arrange
        payload = ChatExportPayload(
            session_id="s1",
            title="导出",
            generated_at=_ASST_TIME,
            turns=[
                _turn(
                    message_id=1,
                    chart_type="bar",
                    chart_option={"series": []},
                    chart_image=solidPng(240, 160),
                )
            ],
        )

        # Act
        pdf = ChatExportPdfBuilder().build(payload)

        # Assert
        assert pdf.startswith(b"%PDF-1.")
        assert len(pdf) > 1024

    def test_build_survives_legacy_turn_without_chart_payload(self) -> None:
        """0105 之前的行：有 chart_type、无 chart_option/bitmap，build 不能炸。"""
        payload = ChatExportPayload(
            session_id="s1",
            title="导出",
            generated_at=_ASST_TIME,
            turns=[_turn(message_id=1, chart_type="pie")],
        )

        pdf = ChatExportPdfBuilder().build(payload)

        assert pdf.startswith(b"%PDF-1.")

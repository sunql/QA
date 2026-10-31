"""导出 PDF 的分节插图（2026-10-31，D1）。

多步最终回答带 ``report_sections`` 时，PDF 必须按块顺序排版：文字 → 该步图 → 下一节，
而不是「整段回答 + 文末一张图」。图仍走既有的三档降级（PNG → 原生 table/kpi → 占位框）。

分节轮次**不再整轮 KeepTogether**：多步报告常跨好几页，整轮裹在一起会把整块推到
下一页，单块超过一页时 reportlab 直接抛 ``LayoutError`` —— 一份导出因此 500。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from reportlab.platypus import Image as PdfImage
from reportlab.platypus import KeepTogether, Paragraph, Table

from app.domain.export_limits import MAX_EXPORT_CHART_IMAGES, MAX_EXPORT_IMAGE_BYTES
from app.domain.schemas import ChatExportChartImage, ChatExportRequest
from app.services.chart_image_validation import attachChartImages
from app.services.chat_chart_persist import _PERSIST_MAX_TABLE_ROWS
from app.services.pdf_export_service import (
    ChatExportPayload,
    ChatExportPdfBuilder,
    ChatExportTurn,
    ExportStepChart,
)
from app.tests._png_support import solidPng

_T = datetime(2026, 1, 1, 10, 1, tzinfo=UTC)


def _stepChart(**overrides) -> ExportStepChart:
    base = {
        "step_index": 0,
        "chart_type": "bar",
        "chart_option": {"series": []},
        "chart_image": None,
    }
    return ExportStepChart(**{**base, **overrides})


def _sections() -> list[dict]:
    return [
        {"kind": "text", "content": "### 步骤 1：各供应商收货量\n华东领先。"},
        {"kind": "chart", "stepIndex": 0, "title": "各供应商收货量"},
        {"kind": "text", "content": "### 步骤 2：月度趋势\n逐月上升。"},
        {"kind": "chart", "stepIndex": 1, "title": "月度趋势"},
    ]


def _turn(**overrides) -> ChatExportTurn:
    base = {
        "user_content": "问题",
        "user_time": _T,
        "assistant_content": "### 步骤 1：各供应商收货量\n华东领先。",
        "assistant_time": _T,
        "report_sections": _sections(),
        "step_charts": (
            _stepChart(step_index=0, chart_image=solidPng(8, 8)),
            _stepChart(
                step_index=1,
                chart_type="kpi",
                chart_option={"kpi": {"label": "总收货量", "value": 9812, "unit": "件"}},
            ),
        ),
    }
    return ChatExportTurn(**{**base, **overrides})


def _flatten(flowables: list) -> list:
    """展开 KeepTogether 小组 —— 排版单位（标题+图同页）不等于文档顺序。"""
    out: list = []
    for item in flowables:
        if isinstance(item, KeepTogether):
            out.extend(_flatten(item._content))
        else:
            out.append(item)
    return out


def _paragraphTexts(flowables: list) -> list[tuple[int, str]]:
    return [
        (index, item.text)
        for index, item in enumerate(flowables)
        if isinstance(item, Paragraph)
    ]


class TestSectionedTurnRendering:
    def test_bitmap_lands_after_its_section_text_and_before_the_next(self) -> None:
        """``(messageId, stepIndex)`` 的图必须落在**它那一节**的正文之后。

        反向守卫：把 report_sections 忽略、退回「整段 markdown + 文末图」时，
        图会落到两节文字之后 ⇒ 本用例红。
        """
        # Act
        flowables = _flatten(ChatExportPdfBuilder()._build_turn(1, _turn()))

        # Assert
        image_index = next(
            index for index, item in enumerate(flowables) if isinstance(item, PdfImage)
        )
        texts = _paragraphTexts(flowables)
        assert any(
            "步骤 1" in text and index < image_index for index, text in texts
        ), "图之前必须有步骤 1 的正文"
        assert any(
            "步骤 2" in text and index > image_index for index, text in texts
        ), "图之后才是步骤 2 的正文"

    def test_table_step_is_rendered_natively_without_a_bitmap(self) -> None:
        """table 步不吃位图：服务端原生画，前端截图失败也照样出表。"""
        # Arrange
        turn = _turn(
            step_charts=(
                _stepChart(
                    step_index=0,
                    chart_type="table",
                    chart_option={
                        "columns": ["地区", "销量"],
                        "rows": [{"地区": "华北", "销量": 100}],
                    },
                ),
            )
        )

        # Act
        flowables = ChatExportPdfBuilder()._build_turn(1, turn)

        # Assert
        tables = [item for item in flowables if isinstance(item, Table)]
        body = [
            [cell.text for cell in row]
            for table in tables
            for row in table._cellvalues
        ]
        assert ["地区", "销量"] in body
        assert ["华北", "100"] in body
        assert not any(isinstance(item, PdfImage) for item in flowables)

    def test_missing_step_chart_falls_back_to_placeholder(self) -> None:
        """分节块引用了不存在/无图的步骤 —— 占一格的占位框，而不是静默消失。"""
        # Arrange
        turn = _turn(step_charts=())

        # Act
        flowables = ChatExportPdfBuilder()._build_turn(1, turn)

        # Assert
        placeholders = [
            item
            for item in flowables
            if isinstance(item, Table) and "图表类型" in str(item._cellvalues[0][0].text)
        ]
        assert len(placeholders) == 2

    def test_turn_is_not_wrapped_in_a_single_keep_together(self) -> None:
        """分节轮次不能整轮 KeepTogether：整轮超过一页会 LayoutError，正文也无法跨页。

        反向守卫：把分节轮次包回 ``KeepTogether(flowables)`` ⇒ 顶层就没有裸
        flowable 了，本用例红。允许的顶层小组只有「问题头」与「小标题 + 一张位图」。
        """
        flowables = ChatExportPdfBuilder()._build_turn(1, _turn())

        groups = [item for item in flowables if isinstance(item, KeepTogether)]
        assert groups and flowables[0] is groups[0], "问题头成组且排在最前"
        loose = [item for item in flowables if not isinstance(item, KeepTogether)]
        assert len(loose) >= 3, "正文必须在小组之外 —— 整轮包起来就没有裸 flowable 了"

    def test_caption_and_bitmap_stay_in_one_group(self) -> None:
        """小标题与位图不能被分页拆开：标题悬在页尾、图跑到次页是最难读的排版。"""
        flowables = ChatExportPdfBuilder()._build_turn(1, _turn())

        groups = [
            item._content for item in flowables if isinstance(item, KeepTogether)
        ]
        assert any(
            any(isinstance(child, Paragraph) for child in group)
            and any(isinstance(child, PdfImage) for child in group)
            for group in groups
        ), "小标题与位图必须在同一个小组里"

    def test_sectioned_turn_builds_a_pdf(self) -> None:
        payload = ChatExportPayload(
            session_id="s-sections",
            title="导出",
            generated_at=_T,
            turns=[_turn()],
        )

        pdf = ChatExportPdfBuilder().build(payload)

        assert pdf.startswith(b"%PDF-1.")
        assert b"/Subtype /Image" in pdf


class TestLegacyTurnRendering:
    def test_turn_without_sections_stays_one_keep_together(self) -> None:
        """无 report_sections 完全维持现状（单问答整轮不可拆）。"""
        turn = ChatExportTurn(
            user_content="问题",
            user_time=_T,
            assistant_content="回答",
            assistant_time=_T,
            chart_type="bar",
            chart_image=solidPng(8, 8),
        )

        flowables = ChatExportPdfBuilder()._build_turn(1, turn)

        assert len(flowables) == 1
        assert isinstance(flowables[0], KeepTogether)


class TestAttachStepImages:
    def test_attaches_bitmap_by_message_and_step(self) -> None:
        # Arrange
        png0, png2 = solidPng(8, 8), solidPng(8, 8)
        turn = _turn(
            message_id=7,
            step_charts=(
                _stepChart(step_index=0),
                _stepChart(step_index=2, chart_image=b"original"),
            ),
        )
        payload = ChatExportPayload(
            session_id="s1", title="t", generated_at=_T, turns=[turn]
        )

        # Act
        merged = attachChartImages(
            payload, {(7, 0): png0, (7, 2): png2}
        )

        # Assert
        assert merged.turns[0].step_charts[0].chart_image == png0
        assert merged.turns[0].step_charts[1].chart_image == png2
        assert merged.turns[0].chart_image is None, "顶层图不该被步骤图顺手填上"

    def test_top_level_and_step_images_are_independent(self) -> None:
        png = solidPng(8, 8)
        turn = _turn(message_id=7, step_charts=(_stepChart(step_index=0),))
        payload = ChatExportPayload(
            session_id="s1", title="t", generated_at=_T, turns=[turn]
        )

        merged = attachChartImages(payload, {(7, None): png})

        assert merged.turns[0].chart_image == png
        assert merged.turns[0].step_charts[0].chart_image is None

    def test_ignores_step_bitmap_for_unknown_step(self) -> None:
        """多出来一张没人要的图不该让导出失败（导出是主功能，图是增强）。"""
        payload = ChatExportPayload(
            session_id="s1",
            title="t",
            generated_at=_T,
            turns=[_turn(message_id=7, step_charts=(_stepChart(step_index=0),))],
        )

        merged = attachChartImages(payload, {(7, 99): b"x"})

        assert merged.turns[0].step_charts[0].chart_image is None


class TestTableRowCap:
    """分节图里的 table 负载可能**未经落库截断**。

    顶层 ``chart_option`` 落库前过 ``boundedChartOption``（rows 截到 200），但
    ``steps_snapshot`` 是另一条写入路径；存量行更不能指望已被截断。渲染侧必须
    再截一道 —— 否则一份导出会把全量结果集灌进 reportlab，而 ``build`` 在 async
    端点里同步执行，排版期间整个进程的事件循环被占住。
    """

    def test_bounds_rows_and_marks_truncated(self) -> None:
        rows = [{"a": i} for i in range(_PERSIST_MAX_TABLE_ROWS + 50)]

        flowables = ChatExportPdfBuilder()._table_from_chart_option(
            {"columns": ["a"], "rows": rows}
        )

        assert isinstance(flowables[0], Table)
        assert len(flowables[0]._cellvalues) == _PERSIST_MAX_TABLE_ROWS + 1
        # 截断必须如实标注，不假装是全部
        assert isinstance(flowables[1], Paragraph)
        assert "仅显示前" in flowables[1].text

    def test_small_table_has_no_truncation_note(self) -> None:
        flowables = ChatExportPdfBuilder()._table_from_chart_option(
            {"columns": ["a"], "rows": [{"a": 1}]}
        )

        assert len(flowables) == 1


class TestExportSchemaLimits:
    """请求体在**解析阶段**就得有上界。

    service 层的条数/字节闸门都是端点到手之后才跑：100 MB 的 JSON 已经全文读入
    并建成 pydantic 对象，闸门只能决定「拒绝」，救不了这次解析的开销。
    """

    def test_rejects_more_than_max_charts(self) -> None:
        charts = [{"messageId": 1, "imagePng": "x"} for _ in range(MAX_EXPORT_CHART_IMAGES + 1)]

        with pytest.raises(ValidationError):
            ChatExportRequest(charts=charts)

    def test_rejects_oversized_image_payload(self) -> None:
        with pytest.raises(ValidationError):
            ChatExportChartImage(message_id=1, image_png="x" * (MAX_EXPORT_IMAGE_BYTES * 2))

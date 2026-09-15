"""评估报告导出服务（feat-dq-evaluation-report，Phase 7a）。

支持 PDF（reportlab）和 Excel（openpyxl）两种格式。设计要点：

- 服务只接收 immutable payload（``EvaluationReportExportPayload`` dataclass），不
  接触 DB/ORM/FastAPI；与 ``pdf_export_service.ChatExportPdfBuilder`` 风格一致。
- 中文字体复用 ``pdf_export_service._register_cjk_font_once`` 提供的
  STSong-Light CID 字体（reportlab 内置，免依赖字体文件）。
- Excel 用 openpyxl Workbook；多 Sheet：Summary / Rules / Samples（可选）。
- 报告 PDF 内的「图表」用文字+表格占位，不重画 echarts——避免重新跑 SQL 的
  性能风险 + 跨平台字体不一致。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    PageTemplate,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.services.pdf_export_service import _register_cjk_font_once

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Payload (immutable) — 由调用方（service / API 层）从 ORM 拼出
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationReportExportPayload:
    """导出输入 payload（不可变 dataclass）。

    snapshot 字段是 ``dict[str, Any]``——服务只读，不校验 schema_version，
    缺键时给空值兜底（与详情页 UI 行为对齐：缺什么显示什么）。
    """

    report_id: int
    name: str
    description: str | None
    status: str
    class_ids: list[int]
    rule_ids: list[int]
    time_window_start: datetime
    time_window_end: datetime
    tags: list[str]
    created_by: str
    created_time: datetime
    snapshot: dict[str, Any]
    samples: list[dict[str, Any]]  # list of ViolationSampleRead.model_dump()


# ---------------------------------------------------------------------------
# PDF 渲染
# ---------------------------------------------------------------------------


_PDF_HEADER_BG = colors.HexColor("#1677ff")
_PDF_HEADER_FG = colors.white
_PDF_ROW_ALT = colors.HexColor("#f5f5f5")


class EvaluationReportPdfBuilder:
    """将 ``EvaluationReportExportPayload`` 渲染为 PDF 字节。"""

    def __init__(self) -> None:
        _register_cjk_font_once()
        cjk = "STSong-Light"
        self._styles = getSampleStyleSheet()
        self._styles.add(
            ParagraphStyle(
                name="ErTitle",
                parent=self._styles["Title"],
                fontName=cjk,
                fontSize=18,
                leading=22,
                spaceAfter=12,
            )
        )
        self._styles.add(
            ParagraphStyle(
                name="ErH2",
                parent=self._styles["Heading2"],
                fontName=cjk,
                fontSize=14,
                leading=18,
                spaceBefore=14,
                spaceAfter=8,
            )
        )
        self._styles.add(
            ParagraphStyle(
                name="ErBody",
                parent=self._styles["Normal"],
                fontName=cjk,
                fontSize=10,
                leading=14,
            )
        )
        self._cjk = cjk

    def build(
        self,
        payload: EvaluationReportExportPayload,
        include_samples: bool = True,
    ) -> bytes:
        import io

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=A4,
            leftMargin=2 * cm,
            rightMargin=2 * cm,
            topMargin=2 * cm,
            bottomMargin=2 * cm,
            title=f"Evaluation Report #{payload.report_id}",
            author="qa-system",
        )
        frames = [
            PageTemplate(
                id="main",
                frames=[],
            ),
        ]
        # 简化为单模板：SimpleDocTemplate 自动用 default Frame。
        del frames

        story = self._build_story(payload, include_samples)
        doc.build(story)
        return buf.getvalue()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build_story(
        self,
        payload: EvaluationReportExportPayload,
        include_samples: bool,
    ) -> list:
        story: list[Any] = []

        story.append(Paragraph(f"评估报告：{payload.name}", self._styles["ErTitle"]))
        if payload.description:
            story.append(Paragraph(payload.description, self._styles["ErBody"]))
        story.append(Spacer(1, 6))

        # 基本信息
        basic_rows = [
            ["报告 ID", str(payload.report_id)],
            ["状态", payload.status],
            ["评估对象数", str(len(payload.class_ids))],
            ["评估规则数", str(len(payload.rule_ids))],
            [
                "时间窗口",
                f"{payload.time_window_start.isoformat()} → {payload.time_window_end.isoformat()}",
            ],
            ["创建人", payload.created_by],
            ["创建时间", payload.created_time.isoformat()],
            [
                "标签",
                ", ".join(payload.tags) if payload.tags else "—",
            ],
        ]
        story.append(self._kv_table(basic_rows, col_widths=[3 * cm, 13 * cm]))
        story.append(Spacer(1, 12))

        # 评估摘要
        story.append(Paragraph("评估摘要", self._styles["ErH2"]))
        snap = payload.snapshot or {}
        overall = snap.get("overall") or {}
        story.append(
            Paragraph(
                f"综合评分：{self._fmt_score(overall.get('score'))}（状态：{overall.get('status', '—')}）",
                self._styles["ErBody"],
            )
        )

        # 六维度
        story.append(Spacer(1, 6))
        story.append(Paragraph("六维度得分", self._styles["ErBody"]))
        dims = snap.get("dimensions") or {}
        dim_rows = [["维度", "得分"]]
        for k in (
            "completeness",
            "validity",
            "uniqueness",
            "consistency",
            "timeliness",
            "referential",
        ):
            dim_rows.append([k, self._fmt_score(dims.get(k))])
        story.append(self._data_table(dim_rows))
        story.append(Spacer(1, 12))

        # 规则明细
        story.append(Paragraph("规则明细", self._styles["ErH2"]))
        tables = snap.get("tables") or []
        flat_rules: list[dict[str, Any]] = []
        for t in tables:
            for r in t.get("rules", []):
                flat_rules.append(r)
        if not flat_rules:
            story.append(Paragraph("（暂无规则明细）", self._styles["ErBody"]))
        else:
            rule_rows = [
                ["Rule", "Type", "Target", "Severity", "Pass Rate", "Status"],
            ]
            for r in flat_rules:
                target = r.get("target_table", "")
                col = r.get("target_column")
                if col:
                    target = f"{target}.{col}"
                rule_rows.append(
                    [
                        str(r.get("rule_code", "")),
                        str(r.get("rule_type", "")),
                        target,
                        str(r.get("severity", "")),
                        self._fmt_score(r.get("pass_rate")),
                        str(r.get("status", "")),
                    ]
                )
            story.append(self._data_table(rule_rows))

        # 违规样本（可选）
        if include_samples and payload.samples:
            story.append(Spacer(1, 12))
            story.append(Paragraph("违规样本", self._styles["ErH2"]))
            sample_rows = [
                ["Rule ID", "Target", "Column", "Total", "Sample Size", "Captured"],
            ]
            for s in payload.samples[:50]:  # 最多展示 50 条
                target = s.get("target_table", "")
                col = s.get("target_column")
                if col:
                    target = f"{target}.{col}"
                captured = s.get("captured_at", "")
                if isinstance(captured, datetime):
                    captured = captured.isoformat()
                sample_rows.append(
                    [
                        str(s.get("rule_id", "")),
                        target,
                        str(col or "—"),
                        str(s.get("total_violations", "")),
                        str(s.get("sample_size", "")),
                        str(captured),
                    ]
                )
            story.append(self._data_table(sample_rows))

        return story

    def _kv_table(self, rows: list[list[str]], col_widths: list[float]) -> Table:
        tbl = Table(rows, colWidths=col_widths, hAlign="LEFT")
        tbl.setStyle(
            TableStyle(
                [
                    ("FONT", (0, 0), (-1, -1), self._cjk, 10),
                    ("BACKGROUND", (0, 0), (0, -1), _PDF_ROW_ALT),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        return tbl

    def _data_table(self, rows: list[list[str]]) -> Table:
        tbl = Table(rows, hAlign="LEFT")
        tbl.setStyle(
            TableStyle(
                [
                    ("FONT", (0, 0), (-1, -1), self._cjk, 9),
                    ("FONT", (0, 0), (-1, 0), self._cjk, 10),
                    ("BACKGROUND", (0, 0), (-1, 0), _PDF_HEADER_BG),
                    ("TEXTCOLOR", (0, 0), (-1, 0), _PDF_HEADER_FG),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, _PDF_ROW_ALT],
                    ),
                ]
            )
        )
        return tbl

    @staticmethod
    def _fmt_score(value: Any) -> str:
        if value is None:
            return "—"
        if isinstance(value, (int, float)):
            return f"{value:.2f}%"
        return str(value)


# ---------------------------------------------------------------------------
# Excel 渲染
# ---------------------------------------------------------------------------


class EvaluationReportExcelBuilder:
    """将 ``EvaluationReportExportPayload`` 渲染为 Excel 字节。

    Sheet 布局：
      - Summary：基本信息 + 综合评分 + 六维度
      - Rules：每规则一行
      - Samples（可选）：每样本一行
    """

    def __init__(self) -> None:
        self._header_font = Font(bold=True, color="FFFFFFFF")
        self._header_fill = PatternFill("solid", fgColor="1677FF")

    def build(
        self,
        payload: EvaluationReportExportPayload,
        include_samples: bool = True,
    ) -> bytes:
        import io

        wb = Workbook()
        self._fill_summary(wb.active, payload)
        wb.create_sheet("Rules")
        self._fill_rules(wb["Rules"], payload)
        if include_samples:
            wb.create_sheet("Samples")
            self._fill_samples(wb["Samples"], payload)

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    # ------------------------------------------------------------------

    def _fill_summary(self, ws: Any, payload: EvaluationReportExportPayload) -> None:
        ws.title = "Summary"
        ws.append(["评估报告", payload.name])
        ws.append(["报告 ID", payload.report_id])
        ws.append(["状态", payload.status])
        ws.append(["评估对象数", len(payload.class_ids)])
        ws.append(["评估规则数", len(payload.rule_ids)])
        ws.append(
            [
                "时间窗口",
                f"{payload.time_window_start.isoformat()} → {payload.time_window_end.isoformat()}",
            ]
        )
        ws.append(["创建人", payload.created_by])
        ws.append(["创建时间", payload.created_time.isoformat()])
        ws.append(["标签", ", ".join(payload.tags) if payload.tags else ""])
        ws.append([])
        snap = payload.snapshot or {}
        overall = snap.get("overall") or {}
        ws.append(["综合评分", overall.get("score")])
        ws.append(["综合状态", overall.get("status")])
        ws.append([])
        ws.append(["六维度得分"])
        ws.append(["维度", "得分"])
        for k in (
            "completeness",
            "validity",
            "uniqueness",
            "consistency",
            "timeliness",
            "referential",
        ):
            ws.append([k, (snap.get("dimensions") or {}).get(k)])

        for col in range(1, 3):
            ws.column_dimensions[get_column_letter(col)].width = 28

    def _fill_rules(self, ws: Any, payload: EvaluationReportExportPayload) -> None:
        headers = [
            "Rule Code",
            "Rule Type",
            "Target Table",
            "Target Column",
            "Severity",
            "Total Count",
            "Passed Count",
            "Violation Count",
            "Pass Rate",
            "Status",
        ]
        ws.append(headers)
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = self._header_font
            cell.fill = self._header_fill
            cell.alignment = Alignment(horizontal="center")
        tables = (payload.snapshot or {}).get("tables") or []
        for t in tables:
            for r in t.get("rules", []):
                ws.append(
                    [
                        r.get("rule_code"),
                        r.get("rule_type"),
                        r.get("target_table"),
                        r.get("target_column"),
                        r.get("severity"),
                        r.get("total_count"),
                        r.get("passed_count"),
                        r.get("violation_count"),
                        r.get("pass_rate"),
                        r.get("status"),
                    ]
                )
        for col_idx in range(1, len(headers) + 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = 18

    def _fill_samples(self, ws: Any, payload: EvaluationReportExportPayload) -> None:
        headers = [
            "Report ID",
            "Rule ID",
            "Target Table",
            "Target Column",
            "Total Violations",
            "Sample Size",
            "Captured At",
        ]
        ws.append(headers)
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = self._header_font
            cell.fill = self._header_fill
            cell.alignment = Alignment(horizontal="center")
        for s in payload.samples:
            captured = s.get("captured_at", "")
            if isinstance(captured, datetime):
                captured = captured.isoformat()
            ws.append(
                [
                    payload.report_id,
                    s.get("rule_id"),
                    s.get("target_table"),
                    s.get("target_column"),
                    s.get("total_violations"),
                    s.get("sample_size"),
                    captured,
                ]
            )
        for col_idx in range(1, len(headers) + 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = 18


# ---------------------------------------------------------------------------
# 顶层 service（包装两个 builder；外部依赖 evaluator 接口的 snapshot/samples）
# ---------------------------------------------------------------------------


class EvaluationReportExportService:
    """评估报告导出 — 上层 service 入口。

    调用方传入 report_id + session；本服务从 DB 拼 payload 然后调相应 builder。
    """

    def __init__(
        self,
        pdf_builder: EvaluationReportPdfBuilder | None = None,
        excel_builder: EvaluationReportExcelBuilder | None = None,
    ) -> None:
        self._pdf = pdf_builder or EvaluationReportPdfBuilder()
        self._excel = excel_builder or EvaluationReportExcelBuilder()

    async def export_pdf(
        self,
        session: Any,
        report_id: int,
        *,
        include_samples: bool = True,
    ) -> bytes:
        payload = await self._build_payload(session, report_id)
        return self._pdf.build(payload, include_samples=include_samples)

    async def export_excel(
        self,
        session: Any,
        report_id: int,
        *,
        include_samples: bool = True,
    ) -> bytes:
        payload = await self._build_payload(session, report_id)
        return self._excel.build(payload, include_samples=include_samples)

    async def _build_payload(
        self, session: Any, report_id: int
    ) -> EvaluationReportExportPayload:
        """从 ORM 取 EvaluationReport + ViolationSamples 拼成 payload。"""
        from sqlalchemy import select

        from app.domain.models import (
            DataQualityViolationSample,
            EvaluationReport,
        )

        report: EvaluationReport | None = (
            await session.execute(
                select(EvaluationReport).where(EvaluationReport.id == report_id)
            )
        ).scalar_one_or_none()
        if report is None:
            raise LookupError(f"evaluation report {report_id} not found")

        sample_stmt = (
            select(DataQualityViolationSample)
            .where(DataQualityViolationSample.report_id == report_id)
            .order_by(DataQualityViolationSample.captured_at.desc())
            .limit(200)
        )
        sample_rows = (await session.execute(sample_stmt)).scalars().all()
        sample_dicts = [
            {
                "report_id": r.report_id,
                "rule_id": r.rule_id,
                "target_table": r.target_table,
                "target_column": r.target_column,
                "total_violations": r.total_violations,
                "sample_size": r.sample_size,
                "captured_at": r.captured_at,
            }
            for r in sample_rows
        ]

        return EvaluationReportExportPayload(
            report_id=report.id,
            name=report.name,
            description=report.description,
            status=report.status,
            class_ids=list(report.class_ids or []),
            rule_ids=list(report.rule_ids or []),
            time_window_start=report.time_window_start,
            time_window_end=report.time_window_end,
            tags=list(report.tags or []),
            created_by=report.created_by,
            created_time=report.created_time,
            snapshot=dict(report.snapshot or {}),
            samples=sample_dicts,
        )


def get_evaluation_report_export_service() -> EvaluationReportExportService:
    return EvaluationReportExportService()
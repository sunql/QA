"""A8（M4 Report 模板 MVP）单元测试。

覆盖（brief §验收 unit 部分）：
- 模板注册表结构校验：2 模板、section 数 1..6、kind/source 白名单、
  sectionId 唯一、paramsModel 是 Pydantic 模型、占位符键 ⊆ params 字段
- validateTemplateStructure：REPORT_MAX_DATA_QUERIES 超限拒绝 + 非法 kind/source 拒绝
- 占位符白名单替换：仅 {{params.xxx}} 整串形式；未知键 / 混拼形式拒绝
- 总结解析：fence SSOT（stripJsonFence）+ 前缀分类 + 无前缀 [推断] 兜底 + ≤200 字截断
- 总结 LLM：恰好 1 次 complete 调用 + purpose="report_summary" 计量；
  LLM 失败 → 返回 None（报告主体照常）；配置缺失 → 不触发不调
- buildSummaryInput：4k 字符截断防御

零 DB 依赖：session 用 AsyncMock 桩，计量用 spy TokenUsageService。
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from app.domain.models import LlmConfig

from app.domain.exceptions import ValidationError
from app.services.report_template_service import (
    PURPOSE_REPORT_SUMMARY,
    REPORT_MAX_DATA_QUERIES,
    SUMMARY_FAILED_TEXT,
    SUMMARY_INPUT_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    ReportTemplateService,
    buildSummaryInput,
    classifySummaryLine,
    normalizeSummary,
    resolvePlaceholder,
    validateTemplateStructure,
)
from app.services.report_templates import REPORT_TEMPLATES
from app.services.token_usage_service import TokenUsageService


# ---------------------------------------------------------------------------
# 模板注册表结构校验
# ---------------------------------------------------------------------------


class TestTemplateRegistry:
    def test_has_exactly_two_templates(self) -> None:
        assert set(REPORT_TEMPLATES.keys()) == {"monthly-ops-v1", "supplier-360-v1"}

    @pytest.mark.parametrize("code", ["monthly-ops-v1", "supplier-360-v1"])
    def test_template_structure_contract(self, code: str) -> None:
        template = REPORT_TEMPLATES[code]
        assert template["code"] == code
        assert isinstance(template["title"], str) and template["title"]
        sections = template["sections"]
        # 蓝图 §21：≤2 页 → 模板声明即定，4-6 节
        assert 1 <= len(sections) <= 6
        sectionIds = [s["sectionId"] for s in sections]
        assert len(sectionIds) == len(set(sectionIds)), "sectionId 必须唯一"
        for section in sections:
            assert section["kind"] in ("table", "kpi_cards", "text")
            source = section["source"]
            assert source["type"] in ("kpi", "supplier360")
        # paramsModel 是 Pydantic 模型类（值必须经 Pydantic 校验后注入）
        assert issubclass(template["paramsModel"], BaseModel)

    def test_placeholders_only_reference_declared_params(self) -> None:
        """占位符只允许 {{params.xxx}} 且键必须在 paramsModel 字段内。"""
        import re

        pattern = re.compile(r"^\{\{params\.([A-Za-z_][A-Za-z0-9_]*)\}\}$")
        for template in REPORT_TEMPLATES.values():
            fields = set(template["paramsModel"].model_fields.keys())
            for section in template["sections"]:
                for value in section["source"].values():
                    if isinstance(value, str) and "{{" in value:
                        match = pattern.match(value)
                        assert match is not None, (
                            f"非法占位符形式: {value!r}（只支持整串 {{{{params.xxx}}}}）"
                        )
                        assert match.group(1) in fields, (
                            f"占位符键 {match.group(1)!r} 不在 paramsModel 字段内"
                        )


class TestValidateTemplateStructure:
    def _template(self, sections: list[dict]) -> dict:
        return {"code": "x", "title": "t", "sections": sections}

    def _section(self, idx: int) -> dict:
        return {
            "sectionId": f"s{idx}",
            "title": "t",
            "kind": "table",
            "source": {"type": "kpi", "kpiCodes": ["K1"]},
        }

    def test_over_max_data_queries_rejected(self) -> None:
        sections = [self._section(i) for i in range(REPORT_MAX_DATA_QUERIES + 1)]
        with pytest.raises(ValidationError):
            validateTemplateStructure(self._template(sections))

    def test_at_max_data_queries_accepted(self) -> None:
        sections = [self._section(i) for i in range(REPORT_MAX_DATA_QUERIES)]
        validateTemplateStructure(self._template(sections))

    def test_unknown_kind_rejected(self) -> None:
        bad = self._section(0)
        bad["kind"] = "chart"
        with pytest.raises(ValidationError):
            validateTemplateStructure(self._template([bad]))

    def test_unknown_source_type_rejected(self) -> None:
        bad = self._section(0)
        bad["source"] = {"type": "raw_sql", "sql": "SELECT 1"}
        with pytest.raises(ValidationError):
            validateTemplateStructure(self._template([bad]))

    def test_duplicate_section_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validateTemplateStructure(
                self._template([self._section(0), self._section(0)])
            )

    def test_non_placeholder_template_form_rejected(self) -> None:
        bad = self._section(0)
        bad["source"] = {"type": "supplier360", "supplierKey": "{{env.SECRET}}"}
        with pytest.raises(ValidationError):
            validateTemplateStructure(self._template([bad]))


# ---------------------------------------------------------------------------
# 占位符白名单替换
# ---------------------------------------------------------------------------


class _Params(BaseModel):
    supplierKey: str = "S1"
    month: str = "2026-09"


class TestResolvePlaceholder:
    def test_exact_placeholder_resolves(self) -> None:
        assert resolvePlaceholder("{{params.supplierKey}}", _Params()) == "S1"

    def test_plain_string_passthrough(self) -> None:
        assert resolvePlaceholder("KPI_SUPPLIER_OTD", _Params()) == "KPI_SUPPLIER_OTD"

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            resolvePlaceholder("{{params.nope}}", _Params())

    def test_mixed_form_rejected(self) -> None:
        """{{params.x}} 拼进别的文本 = 非白名单形式，拒绝（禁止拼 SQL 语义）。"""
        with pytest.raises(ValidationError):
            resolvePlaceholder("prefix-{{params.supplierKey}}", _Params())

    def test_other_namespace_rejected(self) -> None:
        with pytest.raises(ValidationError):
            resolvePlaceholder("{{env.HOME}}", _Params())


# ---------------------------------------------------------------------------
# 总结解析（前缀分类 + fence SSOT + 截断）
# ---------------------------------------------------------------------------


class TestClassifySummaryLine:
    def test_fact_prefix(self) -> None:
        assert classifySummaryLine("[事实] 9 月收货量 100 吨") == "[事实]"

    def test_inference_prefix(self) -> None:
        assert classifySummaryLine("[推断] 波动与交付延期相关") == "[推断]"

    def test_assumption_prefix(self) -> None:
        assert classifySummaryLine("[假设] 下月可能回升") == "[假设]"

    def test_missing_prefix_defaults_to_inference(self) -> None:
        assert classifySummaryLine("没有前缀的行") == "[推断]"


class TestNormalizeSummary:
    def test_json_with_fence_parsed_via_ssot(self) -> None:
        raw = "```json\n" + json.dumps(
            {"lines": ["[事实] A", "B 无前缀", "[假设] C"]}, ensure_ascii=False
        ) + "\n```"
        out = normalizeSummary(raw)
        lines = out.splitlines()
        assert lines[0] == "[事实] A"
        assert lines[1] == "[推断] B 无前缀"
        assert lines[2] == "[假设] C"

    def test_plain_json_without_fence(self) -> None:
        raw = json.dumps({"lines": ["[事实] A"]}, ensure_ascii=False)
        assert normalizeSummary(raw) == "[事实] A"

    def test_non_json_falls_back_to_raw_lines(self) -> None:
        raw = "第一行\n第二行"
        out = normalizeSummary(raw)
        assert out == "[推断] 第一行\n[推断] 第二行"

    def test_empty_lines_dropped(self) -> None:
        assert normalizeSummary("a\n\n\nb") == "[推断] a\n[推断] b"

    def test_empty_raw_returns_empty(self) -> None:
        assert normalizeSummary("") == ""

    def test_hard_truncated_to_max_chars(self) -> None:
        raw = json.dumps({"lines": ["[事实] " + "长" * 500]}, ensure_ascii=False)
        assert len(normalizeSummary(raw)) <= SUMMARY_MAX_CHARS

    def test_summary_failed_text_constant(self) -> None:
        assert SUMMARY_FAILED_TEXT == "（总结生成失败）"


class TestBuildSummaryInput:
    def test_truncated_to_4k(self) -> None:
        sections = [
            {"sectionId": "s", "title": "t", "kind": "table", "data": [{"k": "长" * 5000}]}
        ]
        assert len(buildSummaryInput(sections)) <= SUMMARY_INPUT_MAX_CHARS

    def test_serializes_decimals_and_datetimes(self) -> None:
        from datetime import datetime
        from decimal import Decimal

        sections = [
            {
                "sectionId": "s",
                "kind": "table",
                "data": [{"value": Decimal("1.5"), "at": datetime(2026, 9, 1)}],
            }
        ]
        out = buildSummaryInput(sections)
        assert "1.5" in out


# ---------------------------------------------------------------------------
# 总结 LLM：恰好 1 次 + 计量 purpose="report_summary" + 失败降级
# ---------------------------------------------------------------------------


class _FakeLlm:
    """计数 fake：返回带前缀的 JSON 总结。"""

    def __init__(self, *, content: str | None = None, raiseExc: Exception | None = None):
        self.calls = 0
        self._content = content
        self._raise = raiseExc

    async def complete(self, messages, model=None):
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return MagicMock(
            content=self._content, promptTokens=10, completionTokens=5, cachedTokens=None
        )


class _SpyTokenUsage(TokenUsageService):
    """记参数不走真库：覆盖 recordUsage 拦截调用。"""

    def __init__(self) -> None:
        self.recorded: list[dict] = []
        super().__init__()

    async def recordUsage(self, session, **kwargs):  # noqa: ANN001, ANN003
        self.recorded.append(kwargs)
        return MagicMock()


def _llmConfig() -> LlmConfig:
    """真实 ORM 实例（不落库）——selectModel 需要数值型 weight/阈值字段。"""
    config = LlmConfig(
        model_name="test-model",
        provider="openai",
        is_active=True,
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
        cost_threshold=Decimal("999"),
        weight=10,
    )
    config.id = 7
    return config


class TestGenerateSummary:
    def _service(self, llm: _FakeLlm, usage: _SpyTokenUsage) -> ReportTemplateService:
        return ReportTemplateService(llmFactory=lambda _c: llm, tokenUsage=usage)

    async def test_exactly_one_llm_call_and_recorded_usage(self) -> None:
        llm = _FakeLlm(content=json.dumps({"lines": ["[事实] A"]}, ensure_ascii=False))
        usage = _SpyTokenUsage()
        service = self._service(llm, usage)
        summary = await service._generateSummary(
            AsyncMock(), [{"sectionId": "s", "kind": "table", "data": []}],
            configs=[_llmConfig()],
        )
        assert llm.calls == 1, "每次报告生成恰好 ≤1 次 LLM"
        assert summary == "[事实] A"
        assert len(usage.recorded) == 1
        assert usage.recorded[0]["purpose"] == PURPOSE_REPORT_SUMMARY

    async def test_no_configs_skips_llm(self) -> None:
        llm = _FakeLlm()
        service = self._service(llm, _SpyTokenUsage())
        summary = await service._generateSummary(AsyncMock(), [], configs=[])
        assert llm.calls == 0, "无可用模型配置 → 不触发不调"
        assert summary is None

    async def test_factory_returns_none_degrades(self) -> None:
        service = ReportTemplateService(llmFactory=lambda _c: None, tokenUsage=_SpyTokenUsage())
        summary = await service._generateSummary(AsyncMock(), [], configs=[_llmConfig()])
        assert summary is None

    async def test_llm_failure_returns_none(self) -> None:
        llm = _FakeLlm(raiseExc=RuntimeError("boom"))
        usage = _SpyTokenUsage()
        service = self._service(llm, usage)
        summary = await service._generateSummary(AsyncMock(), [], configs=[_llmConfig()])
        assert llm.calls == 1
        assert summary is None, "LLM 失败 → None（调用方落「（总结生成失败）」）"


# ---------------------------------------------------------------------------
# 可见性纯逻辑（列表/详情 where 条件的判定核心）
# ---------------------------------------------------------------------------


class TestVisibilityDecision:
    def _viewer(self, userId: str, roles: tuple[str, ...]) -> MagicMock:
        viewer = MagicMock()
        viewer.userId = userId
        viewer.roles = roles
        return viewer

    def _report(self, createdBy: str, status: str) -> MagicMock:
        report = MagicMock()
        report.created_by = createdBy
        report.status = status
        return report

    def _service(self) -> ReportTemplateService:
        return ReportTemplateService(
            llmFactory=lambda _c: None, tokenUsage=_SpyTokenUsage()
        )

    def test_admin_sees_all(self) -> None:
        svc = self._service()
        report = self._report("someone-else", "PENDING_REVIEW")
        assert svc.canView(report, self._viewer("admin1", ("admin",))) is True

    def test_owner_sees_own_pending(self) -> None:
        svc = self._service()
        report = self._report("u1", "PENDING_REVIEW")
        assert svc.canView(report, self._viewer("u1", ("user",))) is True

    def test_other_pending_invisible(self) -> None:
        svc = self._service()
        report = self._report("u1", "PENDING_REVIEW")
        assert svc.canView(report, self._viewer("u2", ("user",))) is False

    def test_other_approved_visible(self) -> None:
        svc = self._service()
        report = self._report("u1", "APPROVED")
        assert svc.canView(report, self._viewer("u2", ("user",))) is True

    def test_other_rejected_invisible(self) -> None:
        svc = self._service()
        report = self._report("u1", "REJECTED")
        assert svc.canView(report, self._viewer("u2", ("user",))) is False

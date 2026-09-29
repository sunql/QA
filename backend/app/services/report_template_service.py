"""M4 Report 模板服务（A8 / 蓝图 §5.8 + §21 Phase 3a 硬约束）。

职责边界：取模板 → Pydantic 校验 params → 逐节绑定数据（全部走
KpiCatalogService / Supplier360Service 既有方法，零新增 SQL 执行路径）
→ 组装 sections JSONB → 恰好 ≤1 次 LLM 总结 → 落库。审批状态机与
可见性收敛在本模块，API 层只做依赖注入与 HTTP 语义。

蓝图 §21 Phase 3a 五条硬约束的落点：
1. 查询 ≤10：``validateTemplateStructure``（渲染前校验，超限 ValidationError）
2. 强制人审：生成即 PENDING_REVIEW；review 仅 admin；非 admin 只看
   自己的报告 + 全员 APPROVED
3. 结论有据：总结 prompt 明示「只能基于给定分节数据」+ 输入只含已渲染数据
4. 结论分类：逐行 [事实]/[推断]/[假设] 前缀，解析不出前缀的行兜底 [推断]
5. ≤2 页：模板声明即定（2 模板各 4 节）+ 总结 ≤200 字（prompt 硬约束 +
   ``SUMMARY_MAX_CHARS`` 渲染层硬截断）
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from decimal import Decimal
from typing import Any

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import ReportInstance
from app.infrastructure.llm.base_client import LlmMessage
from app.services.kpi_catalog_service import KpiCatalogService
from app.services.learning.prompt_fence import neutralizeFence
from app.services.llm_json_fence import stripJsonFence
from app.services.llm_retry_policy import consumedTokens
from app.services.messages_zh import (
    MSG_REPORT_ALREADY_REVIEWED,
    MSG_REPORT_PARAMS_INVALID,
    MSG_REPORT_PLACEHOLDER_INVALID,
    MSG_REPORT_SECTION_LIMIT,
    MSG_REPORT_STATUS_INVALID,
    MSG_REPORT_STRUCTURE_INVALID,
    MSG_REPORT_TEMPLATE_NOT_FOUND,
)
from app.services.model_router_service import ModelRouterService, RoutingContext
from app.services.report_templates import REPORT_TEMPLATES, listTemplateMetas
from app.services.supplier_360_service import Supplier360Service
from app.services.token_usage_service import TokenUsageService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（蓝图 §21 硬约束 + brief 裁决）
# ---------------------------------------------------------------------------

REPORT_MAX_DATA_QUERIES = 10  # 硬约束 1：单次生成数据绑定数上限
SUMMARY_MAX_CHARS = 200       # 硬约束 5：总结 ≤200 字（渲染层硬截断防御）
SUMMARY_INPUT_MAX_CHARS = 4000  # 总结输入截断防御（brief §实勘 4）
SUMMARY_FAILED_TEXT = "（总结生成失败）"
PURPOSE_REPORT_SUMMARY = "report_summary"

REPORT_STATUS_PENDING = "PENDING_REVIEW"
REPORT_STATUS_APPROVED = "APPROVED"
REPORT_STATUS_REJECTED = "REJECTED"
REPORT_STATUSES = (
    REPORT_STATUS_PENDING,
    REPORT_STATUS_APPROVED,
    REPORT_STATUS_REJECTED,
)

ALLOWED_SECTION_KINDS = ("table", "kpi_cards", "text")
ALLOWED_SOURCE_TYPES = ("kpi", "supplier360")

SUMMARY_LINE_PREFIXES = ("[事实]", "[推断]", "[假设]")
DEFAULT_SUMMARY_PREFIX = "[推断]"  # 硬约束 4：解析不出前缀的行兜底

# 占位符唯一合法形式：整串 {{params.xxx}}（brief §实勘 2，禁止拼 SQL 语义）
_PLACEHOLDER_RE = re.compile(r"^\{\{params\.([A-Za-z_][A-Za-z0-9_]*)\}\}$")

_REPORT_SUMMARY_SYSTEM_PROMPT = (
    "你是经营分析助手。基于给定的报告分节数据（JSON）撰写中文总结。\n"
    "硬约束：\n"
    "1. 只能基于给定数据总结，禁止编造任何数值或结论；\n"
    "2. 总结不超过 200 字；\n"
    "3. 每条结论独占一行，并以 [事实] / [推断] / [假设] 之一开头："
    "数据直接支持的标 [事实]，由数据推演的标 [推断]，"
    "无数据支撑的猜测标 [假设]；\n"
    '4. 只输出 JSON：{"lines": ["[事实] ...", ...]}，不要任何解释性文字。'
)


# ---------------------------------------------------------------------------
# 纯函数：占位符 / 模板结构 / 总结解析
# ---------------------------------------------------------------------------


def resolvePlaceholder(value: str, params: BaseModel) -> Any:
    """白名单占位符解析：仅整串 ``{{params.xxx}}``；其余含 {{ 的形式拒绝。"""
    if "{{" not in value:
        return value
    match = _PLACEHOLDER_RE.match(value)
    if match is None:
        raise ValidationError(MSG_REPORT_PLACEHOLDER_INVALID.format(detail=value))
    key = match.group(1)
    values = params.model_dump()
    if key not in values:
        raise ValidationError(MSG_REPORT_PLACEHOLDER_INVALID.format(detail=value))
    return values[key]


def _sectionBindingCount(section: dict) -> int:
    """单节展开后的数据绑定调用数：kpi 节 = len(kpiCodes)，supplier360 节 = 1。

    蓝图 §21 硬约束 1 的口径是「数据绑定调用数」而非「section 数」——
    kpi 节每个 kpiCode 展开为一次 get_by_code 调用。
    """
    source = section.get("source") or {}
    if source.get("type") == "kpi":
        return len(source.get("kpiCodes") or [])
    return 1


def validateTemplateStructure(template: dict) -> None:
    """渲染前模板结构校验：展开绑定数上限 + kind/source 白名单 + 占位符合法。

    上限口径 = Σ每节展开绑定数（kpi 节按 kpiCodes 长度展开），与蓝图
    §21「查询 ≤10」严格同口径。
    """
    sections = template.get("sections") or []
    seenIds: set[str] = set()
    paramsModel = template.get("paramsModel")
    paramKeys = (
        set(paramsModel.model_fields.keys()) if paramsModel is not None else set()
    )
    totalBindings = 0
    for section in sections:
        _validateSection(section, seenIds, paramKeys)
        totalBindings += _sectionBindingCount(section)
    if totalBindings > REPORT_MAX_DATA_QUERIES:
        raise ValidationError(
            MSG_REPORT_SECTION_LIMIT.format(limit=REPORT_MAX_DATA_QUERIES)
        )


def _validateSection(
    section: dict, seenIds: set[str], paramKeys: set[str]
) -> None:
    sectionId = section.get("sectionId")
    if not sectionId or sectionId in seenIds:
        raise ValidationError(
            MSG_REPORT_STRUCTURE_INVALID.format(
                detail=f"sectionId 缺失或重复: {sectionId!r}"
            )
        )
    seenIds.add(sectionId)
    if section.get("kind") not in ALLOWED_SECTION_KINDS:
        raise ValidationError(
            MSG_REPORT_STRUCTURE_INVALID.format(
                detail=f"kind 非法: {section.get('kind')!r}"
            )
        )
    source = section.get("source") or {}
    if source.get("type") not in ALLOWED_SOURCE_TYPES:
        raise ValidationError(
            MSG_REPORT_STRUCTURE_INVALID.format(
                detail=f"source.type 非法: {source.get('type')!r}"
            )
        )
    _validateSourcePlaceholders(source, paramKeys)


def _validateSourcePlaceholders(source: dict, paramKeys: set[str]) -> None:
    for value in source.values():
        if not isinstance(value, str) or "{{" not in value:
            continue
        match = _PLACEHOLDER_RE.match(value)
        if match is None or match.group(1) not in paramKeys:
            raise ValidationError(
                MSG_REPORT_PLACEHOLDER_INVALID.format(detail=value)
            )


def classifySummaryLine(line: str) -> str:
    """返回该行的结论前缀（[事实]/[推断]/[假设]）；无前缀兜底 [推断]。"""
    for prefix in SUMMARY_LINE_PREFIXES:
        if line.startswith(prefix):
            return prefix
    return DEFAULT_SUMMARY_PREFIX


def _extractSummaryLines(raw: str) -> list[str]:
    """总结输出 → 行列表：fence SSOT 剥离 + JSON 解析，失败回退原始分行。"""
    try:
        data = json.loads(stripJsonFence(raw))
    except json.JSONDecodeError:
        return raw.splitlines()
    if isinstance(data, dict):
        data = data.get("lines")
    if isinstance(data, list):
        return [str(item) for item in data]
    return raw.splitlines()


def normalizeSummary(raw: str) -> str:
    """总结文本归一：无前缀行补 [推断] + 空行丢弃 + ≤200 字硬截断。"""
    if not raw or not raw.strip():
        return ""
    normalized: list[str] = []
    for line in _extractSummaryLines(raw):
        line = line.strip()
        if not line:
            continue
        if classifySummaryLine(line) == DEFAULT_SUMMARY_PREFIX and not any(
            line.startswith(prefix) for prefix in SUMMARY_LINE_PREFIXES
        ):
            line = f"{DEFAULT_SUMMARY_PREFIX} {line}"
        normalized.append(line)
    return "\n".join(normalized)[:SUMMARY_MAX_CHARS]


def buildSummaryInput(sections: list[dict]) -> str:
    """总结 LLM 输入 = 已渲染分节数据摘要（4k 字符截断防御）。"""
    try:
        payload = json.dumps(sections, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = str(sections)
    return payload[:SUMMARY_INPUT_MAX_CHARS]


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class ReportTemplateService:
    """模板化报告生成 + 查询 + 审批。"""

    def __init__(
        self,
        *,
        llmFactory: Any = None,
        tokenUsage: TokenUsageService | None = None,
    ) -> None:
        # 缺省走 createClient（rag_qa 同模式：缺省 None 在 prod 必炸，故惰性解析）
        self._llmFactory = llmFactory
        self._tokenUsage = tokenUsage or TokenUsageService()
        self._kpiSvc = KpiCatalogService()
        self._supplier360Svc = Supplier360Service()

    # -- 模板 ------------------------------------------------------------

    def listTemplates(self) -> list[dict]:
        return listTemplateMetas()

    def getTemplate(self, code: str) -> dict:
        template = REPORT_TEMPLATES.get(code)
        if template is None:
            raise NotFoundError(MSG_REPORT_TEMPLATE_NOT_FOUND.format(code=code))
        return template

    # -- 生成 ------------------------------------------------------------

    async def generateReport(
        self,
        session: AsyncSession,
        *,
        templateCode: str,
        params: dict[str, Any],
        actor: CurrentUser,
        configs: list | None = None,
    ) -> ReportInstance:
        """同步生成：绑定 + 总结 + 落库（能同步就同步，导入台账僵尸教训）。"""
        template = self.getTemplate(templateCode)
        validateTemplateStructure(template)
        validated = self._validateParams(template, params)
        sections = await self._renderSections(session, template, validated)
        summary = await self._generateSummary(session, sections, configs=configs)
        if summary is None:
            logger.warning(
                "报告总结降级（LLM 不可用或失败）template=%s", templateCode
            )
            summary = SUMMARY_FAILED_TEXT
        instance = ReportInstance(
            template_code=template["code"],
            title=self._deriveTitle(template, validated),
            params=validated.model_dump(),
            sections=sections,
            summary=summary,
            status=REPORT_STATUS_PENDING,
            created_by=actor.userId,
        )
        session.add(instance)
        await session.commit()
        await session.refresh(instance)
        logger.info(
            "报告生成落库 id=%s template=%s by=%s",
            instance.id,
            templateCode,
            actor.userId,
        )
        return instance

    def _validateParams(self, template: dict, params: dict[str, Any]) -> BaseModel:
        """params 必须先过模板的 Pydantic 模型，才允许进入绑定器。"""
        try:
            return template["paramsModel"](**params)
        except PydanticValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                for err in exc.errors()[:5]
            )
            raise ValidationError(
                MSG_REPORT_PARAMS_INVALID.format(detail=detail)
            ) from exc

    def _deriveTitle(self, template: dict, validated: BaseModel) -> str:
        suffix = " · ".join(
            str(value) for value in validated.model_dump().values() if value
        )
        base = template["title"]
        title = f"{base} · {suffix}" if suffix else base
        return title[:200]

    # -- 数据绑定（零新增 SQL 执行路径：全部复用既有服务） ----------------

    async def _renderSections(
        self, session: AsyncSession, template: dict, validated: BaseModel
    ) -> list[dict]:
        bound: list[dict] = []
        # 同 code 的 get_by_code 结果做本次生成内的缓存复用（消除跨节冗余调用）
        kpiCache: dict[str, dict | None] = {}
        for section in template["sections"]:
            bound.append(
                await self._bindSection(session, section, validated, kpiCache)
            )
        return bound

    async def _bindSection(
        self,
        session: AsyncSession,
        section: dict,
        validated: BaseModel,
        kpiCache: dict[str, dict | None],
    ) -> dict:
        """单节绑定；失败降级 renderError，不拖垮整份报告（异常隔离）。"""
        base = {
            "sectionId": section["sectionId"],
            "title": section["title"],
            "kind": section["kind"],
        }
        try:
            source = section["source"]
            if source["type"] == "kpi":
                data = await self._bindKpiSource(session, source, kpiCache)
            else:
                data = await self._bindSupplier360Source(session, source, validated)
        except Exception as exc:
            logger.warning(
                "报告分节绑定失败 section=%s: %s",
                section.get("sectionId"),
                exc,
                exc_info=True,
            )
            return {**base, "data": None, "renderError": str(exc)}
        return {**base, "data": jsonable_encoder(data), "renderError": None}

    async def _bindKpiSource(
        self, session: AsyncSession, source: dict, kpiCache: dict[str, dict | None]
    ) -> list[dict]:
        """KPI 目录绑定：逐 code 走 get_by_code（既有接口），同 code 缓存复用。"""
        rows: list[dict] = []
        for code in source.get("kpiCodes") or []:
            cached = kpiCache.get(code)
            if code in kpiCache:
                rows.append(
                    {**cached} if cached is not None
                    else {"kpiCode": code, "found": False}
                )
                continue
            entity = await self._kpiSvc.get_by_code(session, code)
            if entity is None:
                kpiCache[code] = None
                rows.append({"kpiCode": code, "found": False})
                continue
            row = self._kpiRow(entity)
            kpiCache[code] = row
            rows.append({**row})
        return rows

    def _kpiRow(self, entity) -> dict:
        """KPI 目录行 → 渲染 dict（卡片段取 kpiName/unit/grain/status/owner）。"""
        return {
            "kpiCode": entity.kpi_code,
            "kpiName": entity.kpi_name,
            "unit": entity.unit,
            "grain": entity.grain,
            "formula": entity.formula,
            "numerator": entity.numerator,
            "denominator": entity.denominator,
            "status": entity.status,
            "owner": entity.owner,
            "version": entity.version,
            "found": True,
        }

    async def _bindSupplier360Source(
        self, session: AsyncSession, source: dict, validated: BaseModel
    ) -> list[dict]:
        """供应商 360° 绑定：get360（既有接口）+ part 选择渲染面。"""
        key = resolvePlaceholder(source["supplierKey"], validated)
        part = source.get("part", "kpis")
        view = await self._supplier360Svc.get360(session, key)
        if part == "profile":
            return [
                {"field": "enterpriseKey", "value": view.profile.enterprise_key},
                {"field": "enterpriseCode", "value": view.profile.enterprise_code},
                {"field": "entityType", "value": view.profile.entity_type},
            ]
        if part == "entityCodes":
            return [
                {
                    "sourceSystem": code.source_system,
                    "sourceCode": code.source_code,
                    "sourceKey": code.source_key,
                    "matchRule": code.match_rule,
                }
                for code in view.entity_codes
            ]
        return [
            {
                "featureName": kpi.feature_name,
                "featureAlias": kpi.feature_alias,
                "value": kpi.value,
                "valueText": kpi.value_text,
                "unit": kpi.unit,
                "latest": kpi.latest,
            }
            for kpi in view.kpis
        ]

    # -- 总结 LLM（恰好 ≤1 次；purpose="report_summary" 进台账）-----------

    async def _generateSummary(
        self,
        session: AsyncSession,
        sections: list[dict],
        *,
        configs: list | None,
    ) -> str | None:
        """单次总结调用；任何失败降级 None（报告主体照常落库）。

        报告无 chat session：合成键 ``report-<uuid12>`` 生成一次，RoutingContext
        与 token_usage 台账共用同一键（路由亲和与计量行可对账）。
        """
        selected = None
        reportSessionId = f"report-{uuid.uuid4().hex[:12]}"
        try:
            if not configs:
                logger.warning("报告总结跳过：无可用模型配置")
                return None
            selected = ModelRouterService().selectModel(
                configs,
                _REPORT_SUMMARY_SYSTEM_PROMPT,
                RoutingContext(sessionId=reportSessionId),
            )
            client = self._clientFor(selected)
            if client is None:
                logger.warning(
                    "报告总结跳过：模型 %s 无可用客户端", selected.model_name
                )
                return None
            messages = [
                LlmMessage(role="system", content=_REPORT_SUMMARY_SYSTEM_PROMPT),
                LlmMessage(
                    role="user",
                    content=(
                        "<report_data>\n"
                        f"{neutralizeFence(buildSummaryInput(sections))}\n"
                        "</report_data>"
                    ),
                ),
            ]
            resp = await client.complete(messages)
            await self._recordSummaryUsage(
                session,
                reportSessionId,
                selected,
                resp.promptTokens,
                resp.completionTokens,
                cachedTokens=getattr(resp, "cachedTokens", None),
            )
            return normalizeSummary(resp.content) or None
        except Exception as exc:
            await self._accountFailure(session, reportSessionId, selected, exc)
            logger.warning("报告总结生成失败，降级: %s", exc)
            return None

    def _clientFor(self, config) -> Any | None:
        factory = self._llmFactory
        if factory is None:
            from app.infrastructure.llm.factory import createClient

            factory = createClient
        return factory(config)

    async def _recordSummaryUsage(
        self,
        session: AsyncSession,
        reportSessionId: str,
        config,
        promptTokens: int,
        completionTokens: int,
        *,
        cachedTokens: int | None = None,
    ) -> None:
        """计量必须落 token_usage（红线）；零用量不落行（rag_qa 同口径）。"""
        if not promptTokens and not completionTokens:
            return
        cost = (
            Decimal(promptTokens) * Decimal(str(config.cost_per_1k_input))
            + Decimal(completionTokens) * Decimal(str(config.cost_per_1k_output))
        ) / Decimal(1000)
        await self._tokenUsage.recordUsage(
            session,
            sessionId=reportSessionId,
            modelConfigId=config.id,
            modelName=config.model_name,
            promptTokens=promptTokens,
            completionTokens=completionTokens,
            cost=cost,
            purpose=PURPOSE_REPORT_SUMMARY,
        )

    async def _accountFailure(
        self, session: AsyncSession, reportSessionId: str, selected: Any, exc: Exception
    ) -> None:
        """失败路径已消耗 token 补账（M4 口径：逃逸异常必须带走用量）。"""
        if selected is None:
            return
        try:
            promptTokens, completionTokens = consumedTokens(exc)
            if promptTokens or completionTokens:
                await self._recordSummaryUsage(
                    session, reportSessionId, selected, promptTokens, completionTokens
                )
        except Exception as accountExc:  # pragma: no cover - 防御分支
            logger.warning("报告总结失败路径补账失败: %s", accountExc)

    # -- 可见性 + 审批（蓝图 §21 硬约束 2）--------------------------------

    def canView(self, report: ReportInstance, viewer: CurrentUser) -> bool:
        """admin 全见；非 admin：自己的（任意状态）+ 全员 APPROVED。"""
        if "admin" in (viewer.roles or ()):
            return True
        if report.created_by == viewer.userId:
            return True
        return report.status == REPORT_STATUS_APPROVED

    async def listReports(
        self,
        session: AsyncSession,
        *,
        viewer: CurrentUser,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[ReportInstance], int]:
        if status is not None and status not in REPORT_STATUSES:
            raise ValidationError(MSG_REPORT_STATUS_INVALID.format(value=status))
        conds = []
        if status is not None:
            conds.append(ReportInstance.status == status)
        if "admin" not in (viewer.roles or ()):
            conds.append(
                or_(
                    ReportInstance.created_by == viewer.userId,
                    ReportInstance.status == REPORT_STATUS_APPROVED,
                )
            )
        stmt = select(ReportInstance)
        if conds:
            stmt = stmt.where(*conds)
        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (
            await session.execute(
                stmt.order_by(ReportInstance.id.desc()).limit(limit).offset(offset)
            )
        ).scalars().all()
        return list(rows), int(total)

    async def getReportVisible(
        self, session: AsyncSession, instanceId: int, viewer: CurrentUser
    ) -> ReportInstance | None:
        """不可见返回 None（API 层 404）。

        选 404 而非 403（brief 二选一裁决）：不向非归属者泄露他人
        PENDING_REVIEW / REJECTED 报告的存在性——报告可见性本身就是过滤面，
        403 会构成存在性侧信道（/evidences 的 403 是「资源行为受限」口径，
        与此处语义不同）。
        """
        report = await session.get(ReportInstance, instanceId)
        if report is None:
            return None
        if not self.canView(report, viewer):
            return None
        return report

    async def reviewReport(
        self,
        session: AsyncSession,
        report: ReportInstance,
        *,
        decision: str,
        note: str | None,
        reviewer: CurrentUser,
    ) -> ReportInstance:
        """状态机 PENDING_REVIEW → APPROVED / REJECTED（admin only 在 API 层）。"""
        if report.status != REPORT_STATUS_PENDING:
            raise ConflictError(MSG_REPORT_ALREADY_REVIEWED)
        report.status = (
            REPORT_STATUS_APPROVED if decision == "APPROVE" else REPORT_STATUS_REJECTED
        )
        report.review_note = note
        report.reviewed_by = reviewer.userId
        await session.commit()
        await session.refresh(report)
        logger.info(
            "报告审批 id=%s decision=%s by=%s",
            report.id,
            decision,
            reviewer.userId,
        )
        return report

"""研究侧假设 prompt 的 schema 反射（R1，2026-10-09）。

把 chat 侧 ``OntologyService`` + ``ValueSampler`` 的真列 + 值域采样**搬运**到
研究侧假设生成 prompt，让 LLM 看到 ``DWD_GOODS_RECEIPT_DTL(rcv_qty, ...)`` 而
不是只有 ``供应商`` 三个字。

**不动 chat**（``chat_service.py`` / ``chat_multistep.py`` 等）—— 仅 import 用
``OntologyService`` / ``ValueSampler`` / ``get_adapter`` / ``_safeSchemaPrefix``。

失败面**全部降级**（与 chat ``_sampleValueDomains`` 同款契约）：
- 无 ESL / 无 BOs → ``（无可用 schema 反射）``
- 会话缺数据源 / 数据源缺失 → 同上占位
- 单个 BO 缺 ``classId`` / ``source_table`` → 降级行 ``- NAME（无 source_table）``
- 值域采样抛错 → 整段不抛，返**只含列名**的 hint

公开面只有一个函数 ``buildHypothesisSchemaHint``；被 ``ResearchAgentService.
_generateCandidates`` 调用。

**ESL 形态兼容**：`esl` 参数接受 ``ESLExtraction`` 或 ``dict``（生产 ``state["esl"]``
是 ``_stageEsl`` 里 ``asdict(extraction)`` 出来的 dict，测试 fixture 也用 dataclass
对象；两者都支持，duck-typed normalize）。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchSession
from app.infrastructure.business_db_pool import get_adapter
from app.services.datasource_service import DataSourceService
from app.services.nl2sql_service import Nl2SqlService, _safeSchemaPrefix
from app.services.ontology_service import OntologyService
from app.services.value_sampler import ValueSampler

logger = logging.getLogger(__name__)

# 占位符（出现此串的 hint 表示"啥都没拿到"）
_NO_SCHEMA_PLACEHOLDER = "（无可用 schema 反射）"


def _extractBos(esl: Any) -> list[dict[str, Any]]:
    """从 ESL（ESLExtraction / dict / None）抽出 BOs 为 list[dict]。

    生产路径：``state["esl"]`` 是 ``asdict(ESLExtraction)`` —— BOs 已是 dict 列表。
    测试路径：直接传 ``ESLExtraction`` —— BOs 是 ``BusinessObjectRef`` 列表。
    统一 normalize 成 ``[{classId, className, ...}, ...]``。
    """
    if esl is None:
        return []
    raw = esl.get("businessObjects", []) if isinstance(esl, dict) else (
        esl.businessObjects if hasattr(esl, "businessObjects") else []
    )
    out: list[dict[str, Any]] = []
    for bo in raw or []:
        if isinstance(bo, dict):
            out.append(bo)
        else:
            # 兜底：BO 可能是 dataclass / 普通对象 —— 一律 duck-type 拿 classId/className
            out.append({
                "classId": getattr(bo, "classId", None),
                "className": getattr(bo, "className", None),
            })
    return out


async def buildHypothesisSchemaHint(
    session: AsyncSession,
    sessionId: uuid.UUID,
    esl: Any,
    *,
    valueSampleMax: int = 5,
) -> str:
    """研究假设 prompt 的 schema 提示文本：BO 真表 + 真列 + 关键列值域。

    `esl` 兼容 ``ESLExtraction`` / ``dict`` / ``None``（见模块 docstring）。

    Returns:
        多行字符串，形如::

            可用 schema：
            - DWD_GOODS_RECEIPT_DTL（收货单）: rcv_qty, supplier_id [FK], receipt_id [PK]
            - DIM_SUPPLIER（供应商）: supplier_code, supplier_name
            值域采样：
              DIM_SUPPLIER.supplier_code ∈ {S001, S002, S003}

        失败面**降级**为占位符，**不抛**。
    """
    bos = _extractBos(esl)
    if not bos:
        return _NO_SCHEMA_PLACEHOLDER

    # 1. 取会话绑定的数据源（与 `depsForSession` 同源）
    dsId = await session.scalar(
        select(ResearchSession.datasource_id).where(ResearchSession.id == sessionId)
    )
    if dsId is None:
        logger.warning("研究侧 schema 反射：会话缺数据源 session=%s", sessionId)
        return _NO_SCHEMA_PLACEHOLDER

    ds = await DataSourceService().get(session, dsId)
    if ds is None:
        logger.warning("研究侧 schema 反射：数据源缺失 dsId=%s", dsId)
        return _NO_SCHEMA_PLACEHOLDER

    # 2. 反射每个 BO 的真表 + 真列
    onto = OntologyService()
    blocks: list[str] = []
    resolvedClasses: list[Any] = []
    for bo in bos:
        classId = bo.get("classId")
        className = bo.get("className") or "未知"
        if classId is None:
            blocks.append(f"- {className}（无 classId）")
            continue
        cls = await onto.getClass(session, classId)
        if cls is None or not cls.source_table:
            blocks.append(f"- {className}（无 source_table）")
            continue
        resolvedClasses.append(cls)
        props = await onto.listPropertiesByClass(session, classId)
        colTokens: list[str] = []
        for p in props:
            name = p.source_column or p.property_name or ""
            if not name:
                continue
            tag = ""
            if p.is_primary_key:
                tag += " [PK]"
            if p.is_foreign_key:
                tag += " [FK]"
            colTokens.append(f"{name}{tag}")
        if colTokens:
            blocks.append(
                f"- {cls.source_table}（{className}）: " + ", ".join(colTokens)
            )
        else:
            blocks.append(f"- {cls.source_table}（{className}）: （无列）")

    if not blocks:
        return _NO_SCHEMA_PLACEHOLDER

    # 3. 关键列值域采样（与 chat `_sampleValueDomains` 同款构造）
    try:
        dialect = Nl2SqlService.resolveDialect(ds.type, ds.oracle_version)
        safePrefix = _safeSchemaPrefix(ds.username) if dialect.useSchemaPrefix else None
        adapter = get_adapter(ds.id, ds)
        sampler = ValueSampler(
            adapter.execute_read_only,
            datasourceId=ds.id, dialect=dialect, safePrefix=safePrefix,
        )
        samples = await sampler.sample(resolvedClasses)
    except Exception:  # noqa: BLE001 —— 采样失败**降级**，不阻断研究流水线
        logger.warning("研究侧 schema 反射值域采样失败，降级只返列名", exc_info=True)
        samples = {}

    if samples:
        sampleLines: list[str] = []
        for (tbl, col), vals in list(samples.items())[:valueSampleMax]:
            if not vals:
                continue
            preview = ", ".join(str(v) for v in vals[:5])
            sampleLines.append(f"  {tbl}.{col} ∈ {{{preview}}}")
        if sampleLines:
            blocks.append("值域采样：\n" + "\n".join(sampleLines))

    return "可用 schema：\n" + "\n".join(blocks)

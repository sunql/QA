"""P4 RAG 路（RAG）：向量召回（**按发布 + 按权限**收窄）。

语义源：**语义注册表向量**（类 / 属性 / 指标 / KPI 的向量行）。

为什么必须收窄而不是「先召回再过滤」
------------------------------------
向量召回是**跨治理维度的近似检索**（HNSW）⇒ **先召回再过滤会漏**（被过滤掉的高分项
占位后，剩下的邻域已经偏了）⇒ 过滤必须**下推到 Milvus 表达式**（P3a 的
`RegistryFilter`）⇒ 本路只负责「把这一轮允许看的维度**声明**出去」。

为什么关开关就降级（而不是回落成不过滤的召回）
----------------------------------------------
P3a 的口径（D19）：**调用方声明了作用域，而收窄能力不可用 ⇒ fail-closed（拒绝）**。
本路永远带作用域（四路统一口径 ⇒ 作用域必然存在）⇒ 开关关 ⇒ **降级**。
这同时省掉了「为了拿一份不该看的答案去调 embedding」的成本。

口径
----
1. **过滤器四维**全部取自编排层给的**同一份** `KnowledgeScope` 与 `ResolvedRelease`
   ⇒ 「四路同一口径」在 RAG 路**可证**（过滤器对象本身就是证据）。
2. **发布不可用 ⇒ 降级**：没有发布可过滤 ⇒ 不许放行。
3. **闸门给了降级原因 ⇒ 显式降级**（`v2_missing` / `scope_denied` / …），
   **不静默空集**（静默空集会被误判成「模型没召回到」）。
4. **没命中 ⇒ `EMPTY`**（与降级分开）。

⚠️ 计量缺口（L-P4-9）
--------------------
查询向量由 embedding 服务生成，本批**没有会话级 Token 计量行**可写
（`TokenUsageService.recordUsage` 需要 sessionId / modelConfigId / cost，
而 Facade 是无会话的编排入口）⇒ 本路**记录 token 用量日志**，落库计量留待后续
（不在本批伪造计量数据）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNEL_RAG,
    KNOWLEDGE_ROUTE_DEGRADED,
    KNOWLEDGE_ROUTE_EMPTY,
    KNOWLEDGE_ROUTE_OK,
    KNOWLEDGE_SOURCE_RELEASE,
    KnowledgeEntityRead,
    KnowledgeEvidenceRead,
)
from app.domain.registry_filter import RegistryFilter
from app.services.knowledge.matching import clipText, textOrNone
from app.services.knowledge.messages import (
    MSG_RAG_DISABLED,
    MSG_RAG_EMBEDDING_UNAVAILABLE,
    MSG_RAG_SEARCH_UNAVAILABLE,
    MSG_RELEASE_UNAVAILABLE,
    MSG_SCOPE_NOT_VISIBLE,
)
from app.services.knowledge.release_context import ResolvedRelease
from app.services.knowledge.route_input import RouteInput
from app.services.knowledge.route_outcome import RouteOutcome
from app.services.semantic_registry_rag_gate import (
    RegistrySearchResult,
    registryRagEnabled,
    searchRegistryEmbeddings,
)

logger = logging.getLogger(__name__)

#: 片段文本上限（向量命中的描述可能很长）。
MAX_SNIPPET_LENGTH = 240

#: 查询向量生成器（可注入；默认走 embedding 服务）。
EmbeddingGenerator = Callable[[str], Awaitable[list[float]]]
#: 向量召回出口（可注入；默认走 P3a 闸门）。
RegistrySearcher = Callable[..., RegistrySearchResult]

#: 向量行的类型 → 证据种类（与图谱路 / 指标路的种类词一致）。
_EVIDENCE_KIND_BY_TYPE = {
    "class": "CLASS",
    "property": "PROPERTY",
    "metric": "METRIC",
    "kpi": "KPI",
}


async def _defaultEmbeddingGenerator(text: str) -> list[float]:
    """默认查询向量生成（顺带记 token 用量日志；落库计量见 L-P4-9）。"""
    from app.services.embedding_service import EmbeddingService

    vectors, promptTokens, modelName = await EmbeddingService().embedWithUsage([text])
    logger.info(
        "知识检索：查询向量生成完成（model=%s promptTokens=%s）", modelName, promptTokens
    )
    return list(vectors[0])


def _entityOf(hit: dict[str, Any]) -> KnowledgeEntityRead:
    """向量行 → 命中实体。"""
    type_ = textOrNone(hit.get("type")) or "unknown"
    code = hit.get("ontology_id")
    return KnowledgeEntityRead(
        type=type_,
        code=str(code) if code is not None else "",
        name=textOrNone(hit.get("name")),
    )


def _evidenceOf(
    hit: dict[str, Any],
    *,
    release: ResolvedRelease,
) -> KnowledgeEvidenceRead:
    """向量行 → 证据（带发布版本 ⇒ 引用可回溯）。"""
    type_ = textOrNone(hit.get("type")) or "unknown"
    name = textOrNone(hit.get("name")) or ""
    alias = textOrNone(hit.get("alias"))
    description = textOrNone(hit.get("description"))
    distance = hit.get("distance")
    distanceText = f"{float(distance):.4f}" if isinstance(distance, (int, float)) else None
    snippet = description or alias or name
    if distanceText:
        snippet = f"{snippet}（相似度距离 {distanceText}）" if snippet else distanceText
    return KnowledgeEvidenceRead(
        channel=KNOWLEDGE_CHANNEL_RAG,
        kind=_EVIDENCE_KIND_BY_TYPE.get(type_, type_.upper()),
        ref=name or str(hit.get("ontology_id") or ""),
        title=alias or name or str(hit.get("ontology_id") or ""),
        snippet=clipText(snippet, MAX_SNIPPET_LENGTH) if snippet else None,
        release_id=release.release_id,
        release_version=release.version,
        source=KNOWLEDGE_SOURCE_RELEASE,
    )


async def runRagRoute(
    inp: RouteInput,
    *,
    embeddingGenerator: EmbeddingGenerator | None = None,
    searcher: RegistrySearcher | None = None,
) -> RouteOutcome:
    """RAG 路召回（按发布 + 按权限收窄）。

    Args:
        inp: 编排层给的统一输入（问题 / 域 / 发布面 / 权限口径 / 上限）。
        embeddingGenerator: 查询向量生成器（测试注入点）。
        searcher: 向量召回出口（测试注入点）。

    Returns:
        路由结果（`OK` / `EMPTY` / `DEGRADED`）。
    """
    release = inp.release
    if release.degraded or release.manifest is None:
        return RouteOutcome(
            channel=KNOWLEDGE_CHANNEL_RAG,
            status=KNOWLEDGE_ROUTE_DEGRADED,
            degraded_reason=release.reason or MSG_RELEASE_UNAVAILABLE,
        )

    if inp.domain_code not in inp.scope.domain_codes:
        # 正常由编排层先拦（域不可见 ⇒ 空结果 + 警告）；这里是路由层的兜底。
        logger.warning(
            "RAG 路：请求域不在可见域集合内（domain=%s），本路拒绝", inp.domain_code
        )
        return RouteOutcome(
            channel=KNOWLEDGE_CHANNEL_RAG,
            status=KNOWLEDGE_ROUTE_DEGRADED,
            degraded_reason=MSG_SCOPE_NOT_VISIBLE,
        )

    if not registryRagEnabled():
        # fail-closed：本路永远带作用域 ⇒ 没有收窄能力就不许召回。
        logger.warning("RAG 路：语义注册表 RAG 能力未启用，本轮降级（不回落到不过滤的召回）")
        return RouteOutcome(
            channel=KNOWLEDGE_CHANNEL_RAG,
            status=KNOWLEDGE_ROUTE_DEGRADED,
            degraded_reason=MSG_RAG_DISABLED,
        )

    generate = embeddingGenerator or _defaultEmbeddingGenerator
    try:
        embedding = await generate(inp.question)
    except Exception as exc:  # noqa: BLE001 —— 外部服务失败不许炸整轮查询
        logger.warning("RAG 路：查询向量生成失败，本轮降级：%s", exc, exc_info=True)
        return RouteOutcome(
            channel=KNOWLEDGE_CHANNEL_RAG,
            status=KNOWLEDGE_ROUTE_DEGRADED,
            degraded_reason=MSG_RAG_EMBEDDING_UNAVAILABLE,
        )

    registryFilter = RegistryFilter(
        domainCodes=(inp.domain_code,),
        orgCodes=inp.scope.org_codes,
        entityTypes=inp.scope.entity_types,
        releaseId=release.release_id,
    )
    search = searcher or searchRegistryEmbeddings
    try:
        result = await asyncio.to_thread(
            search, embedding, inp.top_k, None, registryFilter
        )
    except Exception as exc:  # noqa: BLE001 —— 同上
        logger.warning("RAG 路：向量召回失败，本轮降级：%s", exc, exc_info=True)
        return RouteOutcome(
            channel=KNOWLEDGE_CHANNEL_RAG,
            status=KNOWLEDGE_ROUTE_DEGRADED,
            degraded_reason=MSG_RAG_SEARCH_UNAVAILABLE,
        )

    if result.degradedReason:
        # 闸门给的降级原因**原样透出**（调用方要能分辨「没召回到」与「这条路挂了」）。
        logger.warning("RAG 路：闸门降级（reason=%s），本轮降级", result.degradedReason)
        return RouteOutcome(
            channel=KNOWLEDGE_CHANNEL_RAG,
            status=KNOWLEDGE_ROUTE_DEGRADED,
            degraded_reason=result.degradedReason,
        )

    hits = list(result.hits)[: inp.top_k]
    if not hits:
        return RouteOutcome(channel=KNOWLEDGE_CHANNEL_RAG, status=KNOWLEDGE_ROUTE_EMPTY)

    evidence = tuple(_evidenceOf(hit, release=release) for hit in hits)
    entities = tuple(_entityOf(hit) for hit in hits)
    return RouteOutcome(
        channel=KNOWLEDGE_CHANNEL_RAG,
        status=KNOWLEDGE_ROUTE_OK,
        evidence=evidence,
        entities=entities,
    )


__all__ = [
    "EmbeddingGenerator",
    "RegistrySearcher",
    "runRagRoute",
]

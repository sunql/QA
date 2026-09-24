"""Graph Insights LLM 解读（Phase 3.2）。

两类解读：
- 意外连接（跨社区 / 跨维度）：解释「为什么这条已确认关系出人意料」
- 桥接节点（连接 3+ 社区）：解释「为什么它是桥，删掉它会失去什么」

知识缺口不调 LLM（它只是告警：孤立 / 稀疏 / 无维度，LLM 解读留到 Deep
Research 阶段统一处理，YAGNI）。

缓存策略：
- (kind, key) 唯一索引 —— 同一拓扑重算读 cache 不再调 LLM。
- 拓扑变化时（key 变化）旧 cache 自动失效（不会重建，但会被覆盖/删除
  在重算时统一处理）。

失败语义（与 claim_extractor 同）：
- LLM 失败 / 输出无效 → 落库时 explanation 留空字符串，is_call_ok=False
- 不抛错、不阻断整体扫描
- 计量走 ``mechanism=INSIGHT``，purpose=wiki_graph_insight
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_graph_insight import WikiGraphInsight
from app.services.knowledge_graph.insights import (
    BridgeNode,
    SurprisingConnection,
)

_PROMPT_DIR = Path(__file__).parent / "prompts"
_SURPRISING_PROMPT_PATH = _PROMPT_DIR / "insight_surprising_v1.txt"
_BRIDGE_PROMPT_PATH = _PROMPT_DIR / "insight_bridge_v1.txt"
_MECHANISM = "INSIGHT"
_MAX_EXPLANATION_CHARS = 200  # prompt 限 80 字，留 2x 缓冲防 LLM 超长


@dataclass(frozen=True)
class InsightExplanation:
    """单条解读结果。``explanation`` 为空 = 解读失败。"""

    explanation: str
    modelId: int | None
    ok: bool


def _loadPrompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _buildSurprisingUserPrompt(conn: SurprisingConnection) -> str:
    return (
        f"关系：{conn.sourceTitle} -[{conn.relationType}]-> {conn.targetTitle}\n"
        f"两端社区：{conn.sourceCommunity} vs {conn.targetCommunity}\n"
        f"两端维度：{conn.sourceDimension} vs {conn.targetDimension}\n"
    )


def _buildBridgeUserPrompt(bridge: BridgeNode) -> str:
    communities = ", ".join(bridge.communities)
    return (
        f"节点：{bridge.title}\n"
        f"连接社区：{communities}\n"
        f"度数：{bridge.degree}\n"
    )


def _extractExplanation(payload: dict[str, Any]) -> str:
    """从 LLM JSON 抽出 explanation，并按上限截断。"""
    raw = payload.get("explanation")
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if len(text) > _MAX_EXPLANATION_CHARS:
        return text[:_MAX_EXPLANATION_CHARS]
    return text


class GraphInsightExplainer:
    """生成意外连接 / 桥接节点的 LLM 解读。"""

    def __init__(self, invoker: Any) -> None:
        """``invoker`` 复用 LearningLLMInvoker（统一 token 计量入口）。"""
        self._invoker = invoker

    async def explainSurprisingConnection(
        self, conn: SurprisingConnection, *, modelId: int
    ) -> InsightExplanation:
        try:
            parsed, _ = await self._invoker.completeJson(
                systemPrompt=_loadPrompt(_SURPRISING_PROMPT_PATH),
                userPrompt=_buildSurprisingUserPrompt(conn),
                mechanism=_MECHANISM,
                purpose="wiki_graph_insight_surprising",
                maxTokens=512,
            )
        except Exception:
            return InsightExplanation("", modelId, False)
        text = _extractExplanation(parsed if isinstance(parsed, dict) else {})
        return InsightExplanation(text, modelId, bool(text))

    async def explainBridgeNode(
        self, bridge: BridgeNode, *, modelId: int
    ) -> InsightExplanation:
        try:
            parsed, _ = await self._invoker.completeJson(
                systemPrompt=_loadPrompt(_BRIDGE_PROMPT_PATH),
                userPrompt=_buildBridgeUserPrompt(bridge),
                mechanism=_MECHANISM,
                purpose="wiki_graph_insight_bridge",
                maxTokens=512,
            )
        except Exception:
            return InsightExplanation("", modelId, False)
        text = _extractExplanation(parsed if isinstance(parsed, dict) else {})
        return InsightExplanation(text, modelId, bool(text))


async def loadCachedExplanation(
    session: AsyncSession, *, kind: str, key: str
) -> str:
    """读已落库的解读（按 kind + key 唯一索引）。无则返回空串。"""
    row = (
        await session.execute(
            select(WikiGraphInsight.explanation).where(
                WikiGraphInsight.kind == kind,
                WikiGraphInsight.key == key,
            )
        )
    ).first()
    return row[0] if row else ""

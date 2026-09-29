"""Phase 5.5 SPARSE_COMMUNITY gap 的 LLM 主题建议（机制 TOPIC）。

设计要点：
- 输入：社区内若干 page 的标题列表（**只用标题、不用正文**），
  关系图谱是「这些条目彼此连接」，主题只取决于它们讲什么、不取决于怎么说。
- 输出：一段 ≤ 20 字的主题字符串，供前端 Modal 预览；用户确认后
  由 ``PATCH /communities/{communityKey}`` 落库到 ``knowledge_community.topic``。
- 失败语义：LLM 不可用 / JSON 解析失败 → 返回 ``ok=False, topic=""``，
  不抛 —— 与 ``claim_extractor`` / ``insight_explainer`` 一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import KnowledgeCommunity, KnowledgeCommunityMember, WikiPage

if TYPE_CHECKING:  # 仅类型标注，避免 service 间循环导入
    from app.services.learning.llm_invoker import LearningLLMInvoker


_PROMPT_PATH = Path(__file__).parent / "prompts" / "community_topic_v1.txt"
_MECHANISM = "TOPIC"

# 最多取 30 个标题喂给 LLM：超过这个量主题分类就不再细化，反而稀释信号。
# 30 已是 SPARSE_COMMUNITY 社区的典型规模（5-20 个成员居多）。
_MAX_TITLES = 30

# 主题长度硬上限（与 prompt 「4-20 字」+ 边界保护对齐）
_MAX_TOPIC_CHARS = 20


@dataclass(frozen=True)
class TopicSuggestion:
    """一次主题建议的结果（不可变）。"""

    topic: str
    pageCount: int
    ok: bool


def _loadPrompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _buildUserPrompt(titles: list[str]) -> str:
    bulletTitles = "\n".join(f"- {title}" for title in titles)
    return (
        f"社区内知识条目标题（共 {len(titles)} 条）：\n{bulletTitles}\n"
        f"请给出凝练主题（4-20 字）："
    )


def _extractTopic(payload: dict) -> str:
    """从 LLM JSON 抽出 topic 字符串并按上限截断。"""
    raw = payload.get("topic")
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    # 去除常见标点（prompt 要求「不要标点」，但允许一道兜底）
    text = text.rstrip("。.,，;:；:、!?")
    if not text:
        return ""
    return text[:_MAX_TOPIC_CHARS]


async def loadCommunityTitles(
    session: AsyncSession, communityKey: str, *, limit: int = _MAX_TITLES
) -> list[str]:
    """加载社区内 page 标题（按 communityKey 解析 → members → pages）。"""
    community = (
        await session.execute(
            select(KnowledgeCommunity).where(
                KnowledgeCommunity.community_key == communityKey
            )
        )
    ).scalar_one_or_none()
    if community is None:
        return []

    rows = (
        await session.execute(
            select(WikiPage.title)
            .join(KnowledgeCommunityMember, KnowledgeCommunityMember.page_id == WikiPage.page_id)
            .where(KnowledgeCommunityMember.community_id == community.id)
            .limit(limit)
        )
    ).all()
    return [title for (title,) in rows if title]


class CommunityTopicSuggester:
    """为社区生成主题建议（一次只问一句，输出短 JSON）。"""

    def __init__(self, invoker: "LearningLLMInvoker") -> None:
        self._invoker = invoker

    async def suggest(
        self, session: AsyncSession, communityKey: str
    ) -> TopicSuggestion:
        titles = await loadCommunityTitles(session, communityKey)
        if not titles:
            return TopicSuggestion(topic="", pageCount=0, ok=False)

        try:
            parsed, _ = await self._invoker.completeJson(
                systemPrompt=_loadPrompt(),
                userPrompt=_buildUserPrompt(titles),
                mechanism=_MECHANISM,
                purpose="wiki_graph_community_topic",
                maxTokens=200,
            )
        except Exception:  # noqa: BLE001 - 与 insight_explainer 同语义
            return TopicSuggestion(topic="", pageCount=len(titles), ok=False)

        topic = _extractTopic(parsed if isinstance(parsed, dict) else {})
        return TopicSuggestion(
            topic=topic, pageCount=len(titles), ok=bool(topic)
        )


__all__ = ["CommunityTopicSuggester", "TopicSuggestion", "loadCommunityTitles"]

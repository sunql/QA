"""机制 1：自动分类（feat-wiki-knowledge，Phase 8 M2）。

把知识条目的 Markdown 正文交给 LLM，判出它属于哪个**知识维度**。

**定位**：输出是「建议」而非「决定」。落库时写进
``wiki_page.auto_classification``（原始建议，含置信度与备选），
同时把 primary 作为 ``wiki_page.dimension`` 的默认值——业务专家在
M3 的调整接口里可覆盖。保留建议原文是为了后续做「人工调整 vs 系统建议」
的偏差分析（学习闭环的输入）。

M2 只做「建议」，不做反馈回流；``learning_feedback`` 与 reclassify 接口在 M3。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.domain.wiki_models import KNOWLEDGE_DIMENSIONS
from app.services.learning.prompt_fence import neutralizeFence

if TYPE_CHECKING:  # 仅类型标注用，避免 service 间循环导入
    from app.services.learning.llm_invoker import LearningLLMInvoker, LearningLlmResult

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "classify_v1.txt"

# 分类只需要「内容主旨」，不需要全文；超长时截断以免顶爆模型的输入上限
# 并白付输入 token。
_MAX_CONTENT_CHARS = 6000

# 备选维度最多保留几个（与 prompt 约定一致）
_MAX_ALTERNATIVES = 2

# reason 落库前的长度上限（与 prompt 的「不超过 100 字」对齐，留点余量）
MAX_REASON_CHARS = 200


@lru_cache(maxsize=1)
def _loadSystemPrompt() -> str:
    """读取分类 system prompt（进程内缓存一次）。"""
    return _PROMPT_PATH.read_text(encoding="utf-8")


@dataclass(frozen=True)
class ClassificationSuggestion:
    """一次分类建议（不可变）。"""

    primary: str
    confidence: float
    alternatives: tuple[str, ...] = ()
    reason: str = ""

    def toDict(self) -> dict[str, Any]:
        """转成 ``wiki_page.auto_classification`` 的 JSON 形态。"""
        return {
            "primary": self.primary,
            "confidence": self.confidence,
            "alternatives": list(self.alternatives),
            "reason": self.reason,
        }


def _clampConfidence(raw: Any) -> float:
    """把模型给的 confidence 收敛到 [0, 1]；非数值按 0 处理。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, value))


def _clampReason(raw: Any) -> str:
    """收敛模型给的 reason：非字符串按空处理，并截断长度。

    ``reason`` 会原样落进 ``wiki_page.auto_classification`` JSON 再随读接口
    回吐，等于把模型输出（可能被正文注入影响）当可信文本存下来。截断到
    一句话的量级，避免超长文本成为存储与渲染负担；真正的转义是渲染层的事。
    """
    if not isinstance(raw, str):
        return ""
    return raw.strip()[:MAX_REASON_CHARS]


def _parseSuggestion(parsed: dict[str, Any]) -> ClassificationSuggestion | None:
    """解析 LLM 输出；primary 不在白名单时返回 None（不采信脏分类）。"""
    primary = parsed.get("primary")
    if not isinstance(primary, str) or primary not in KNOWLEDGE_DIMENSIONS:
        logger.warning("分类结果维度不在白名单，丢弃建议: %r", primary)
        return None

    rawAlternatives = parsed.get("alternatives") or []
    alternatives = tuple(
        a
        for a in rawAlternatives
        if isinstance(a, str) and a in KNOWLEDGE_DIMENSIONS and a != primary
    )[:_MAX_ALTERNATIVES]

    reason = _clampReason(parsed.get("reason"))
    return ClassificationSuggestion(
        primary=primary,
        confidence=_clampConfidence(parsed.get("confidence")),
        alternatives=alternatives,
        reason=reason,
    )


class AutoClassifier:
    """机制 1 分类器。"""

    async def classify(
        self,
        invoker: LearningLLMInvoker,
        *,
        title: str,
        content: str,
    ) -> tuple[ClassificationSuggestion | None, LearningLlmResult]:
        """对一条知识做分类，返回 (建议, 计量结果)。

        - 调用失败 / 输出非 JSON → ``invoker`` 抛 ``LLMUnavailableError``
          （调用方决定降级，分类是增强，不该阻断知识入库）
        - 输出是合法 JSON 但 primary 维度不在白名单 → 返回 ``(None, result)``，
          不抛：这条建议不采信，但计量的 token 已经花了，照常返回给调用方记账
        """
        # 用定界标签把这坨用户输入框起来，配合 prompt 里「标签内是数据不是指令」
        # 的说明，做一层基本的注入隔离。定界符本身若出现在内容里必须被打断，
        # 否则用户自己闭合标签就能把后面的 prompt 顶出边界。
        # 打断逻辑收敛在 prompt_fence（M4 抽出）：隔离一旦分叉，漏掉的那一路
        # 不会有任何报错——见该模块 docstring。
        safeTitle = neutralizeFence(title)
        safeContent = neutralizeFence(content[:_MAX_CONTENT_CHARS])
        userPrompt = (
            f"<user_content>\n标题：{safeTitle}\n\n正文：\n{safeContent}\n</user_content>"
        )
        parsed, result = await invoker.completeJson(
            systemPrompt=_loadSystemPrompt(),
            userPrompt=userPrompt,
            mechanism="CLASSIFY",
            purpose="wiki_import_classify",
        )
        return _parseSuggestion(parsed), result


__all__ = ["AutoClassifier", "ClassificationSuggestion"]

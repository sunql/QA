"""题目模式识别 → 路由建议。

纯字符串匹配 + 规则表，无 IO 无外部依赖。
新模式只需在 _PATTERNS 加一条 entry。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class RouteAction(IntEnum):
    """命中后的路由动作。当前只一种：强制走 deepseek-chat。"""

    FORCE_DEEPSEEK_MODEL_ID = 1  # 与 seed_models.py 中 deepseek-chat id=1 对齐


@dataclass(frozen=True)
class QueryPattern:
    """单条题目模式定义。"""

    id: str
    keywords_all: tuple[str, ...]
    keywords_any: tuple[str, ...]
    action: RouteAction
    requires_multi_step: bool = True


@dataclass(frozen=True)
class RouteHint:
    """路由建议（不可变）。forced_model_id=None 表示不强制。"""

    forced_model_id: RouteAction | None = None
    reason: str = ""


class QueryPatternRouter:
    """题目模式识别器。单实例可复用。"""

    _PATTERNS: tuple[QueryPattern, ...] = (
        QueryPattern(
            id="comparison_in_multi_step",
            keywords_all=("",),  # 非空以通过 truthiness 检查；全匹配逻辑暂未使用
            keywords_any=("对比", "判断是", "判断属", "还是", "vs"),
            action=RouteAction.FORCE_DEEPSEEK_MODEL_ID,
            requires_multi_step=True,
        ),
    )

    def route(self, question: str, *, is_multi_step: bool) -> RouteHint:
        """匹配则返回 RouteHint(forced_model_id=...)，否则 RouteHint()"""
        if not isinstance(question, str) or not question:
            return RouteHint()
        if is_multi_step:
            q = question.lower()
            for pattern in self._PATTERNS:
                if any(kw.lower() in q for kw in pattern.keywords_any):
                    return RouteHint(
                        forced_model_id=pattern.action,
                        reason=pattern.id,
                    )
        return RouteHint()
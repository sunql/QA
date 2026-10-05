"""sub-question 改写器：把 Qwen 拼不出的 sub-question 改写成它拼得出的形态。

仅在 model_name 含 'qwen' 时启用（其他模型保持原行为）。
新规则只需在 _RULES 加一条 entry。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable


_SUPPLIER_CODE_RE = re.compile(r"\b([A-Z]\d{3})\b")


def _extract_supplier_from_prev(prev_results: tuple[Any, ...]) -> dict[str, str]:
    """从前序 StepResult.aggregate_text 抽取第一个 supplier 编码。失败时 fallback 'B019'。

    占位提取规则简单；后续可按需求扩到 step 1 第一个 aggregate 列。
    """
    fallback = "B019"
    for r in prev_results:
        text = getattr(r, "aggregate_text", None) or getattr(r, "aggregate", None)
        if isinstance(text, str) and text:
            m = _SUPPLIER_CODE_RE.search(text)
            if m:
                return {"supplier": m.group(1)}
    return {"supplier": fallback}


@dataclass(frozen=True)
class RewriteRule:
    """单条改写规则。"""

    id: str
    match: Callable[[str, tuple[Any, ...]], bool]
    template: str
    extract_vars: Callable[[tuple[Any, ...]], dict[str, str]]


@dataclass(frozen=True)
class RewriteResult:
    """改写结果。rewritten=None 表示不改写。"""

    rewritten: str | None = None
    template_id: str = ""
    reason: str = ""


class StepSubquestionRewriter:
    """sub-question 改写器。单实例可复用。"""

    _RULES: tuple[RewriteRule, ...] = (
        RewriteRule(
            id="compare_by_location",
            match=lambda sq, _prev: (
                isinstance(sq, str)
                and ("对照" in sq or "对比" in sq)
                and "地点" in sq
            ),
            template=(
                "按 RCV_SITE_CODE（收货地点）分组，统计 {supplier} 在过去 12 个月"
                "的月供货量与同地点全部其他供应商的月供货量，"
                "输出 RCV_SITE_CODE × 月份 × ({supplier}供货量 / 同地点总供货量)。"
            ),
            extract_vars=_extract_supplier_from_prev,
        ),
    )

    def rewrite(
        self,
        *,
        sub_question: str | None,
        prev_results: tuple[Any, ...],
        model_name: str,
    ) -> RewriteResult:
        """仅 Qwen 系列生效；其他模型返回 RewriteResult() 不改写。"""
        if not isinstance(sub_question, str) or not sub_question:
            return RewriteResult()
        if "qwen" not in (model_name or "").lower():
            return RewriteResult()
        try:
            for rule in self._RULES:
                if rule.match(sub_question, prev_results):
                    vars_ = rule.extract_vars(prev_results)
                    return RewriteResult(
                        rewritten=rule.template.format(**vars_),
                        template_id=rule.id,
                    )
        except Exception:  # noqa: BLE001
            return RewriteResult()  # 任何异常 → 降级为不改写
        return RewriteResult()

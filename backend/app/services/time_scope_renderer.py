"""``{time_scope}`` 占位符 → SQL 时间谓词的确定性渲染（P4a，纯函数，无 IO）。

设计（docs/superpowers/specs/2026-11-13-kpi-time-parameterization-design.md §5.2）：
- 用 resolver 算好的半开区间 [start, end) 渲染 **ANSI DATE 字面量**（spec 裁决的
  「等价确定性表达式」）：确定性、可整段子句断言、跨方言同构。
- Oracle 先行（D4）：其余方言抛 ``TimeScopeRenderUnsupportedError``，上层转
  degraded reason，**绝不静默降级 Tier B 让 LLM 猜时间**。
- 谓词是行级过滤，由 formula 作者把占位符写在 WHERE 位置 ⇒ ratio 型公式的
  CASE/NULLIF 天然在聚合前接收过滤（外层包裹做不到这一点，D2 选占位符的原因）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.services.time_scope_resolver import TimeScope, parseTimeScope

TIME_SCOPE_PLACEHOLDER = "{time_scope}"
REASON_TIME_SCOPE_PARSE_FAILED = "time_scope_parse_failed"
REASON_TIME_SCOPE_RENDER_UNSUPPORTED = "time_scope_render_unsupported_dialect"

_SUPPORTED_DIALECTS: frozenset[str] = frozenset({"oracle"})


class TimeScopeRenderUnsupportedError(Exception):
    """方言不支持 ``{time_scope}`` 渲染（fail-closed，设计 D4）。"""


def renderTimeScopePredicate(scope: TimeScope, timeColumn: str, dialect: str) -> str:
    """半开区间 [start, end) → ANSI DATE 字面量谓词；方言不支持则抛错。"""
    normalized = (dialect or "").strip().lower()
    if normalized not in _SUPPORTED_DIALECTS:
        raise TimeScopeRenderUnsupportedError(
            f"dialect {normalized!r} 尚不支持 {{time_scope}} 渲染（P4a 仅 oracle）"
        )
    return (
        f"{timeColumn} >= DATE '{scope.start.isoformat()}' "
        f"AND {timeColumn} < DATE '{scope.end.isoformat()}'"
    )


@dataclass(frozen=True)
class TimeParamFormula:
    """占位符组装结果：ok=False 时 reason 必非空、formula 必为 None。"""

    ok: bool
    formula: str | None = None
    scopeLabel: str | None = None
    reason: str | None = None


def prepareTimeParameterizedFormula(
    *,
    formula: str,
    question: str,
    timeColumn: str,
    dialect: str,
    today: date | None = None,
) -> TimeParamFormula:
    """解析问句时间 ⇒ 渲染谓词 ⇒ 替换 formula 占位符（三步全部确定性）。

    占位符不在 formula ⇒ 原样放行（路由层保证不会走到；幂等防御）。
    """
    if TIME_SCOPE_PLACEHOLDER not in formula:
        return TimeParamFormula(ok=True, formula=formula)

    scope = parseTimeScope(question, today=today)
    if scope is None:
        return TimeParamFormula(ok=False, reason=REASON_TIME_SCOPE_PARSE_FAILED)

    try:
        predicate = renderTimeScopePredicate(scope, timeColumn, dialect)
    except TimeScopeRenderUnsupportedError:
        return TimeParamFormula(ok=False, reason=REASON_TIME_SCOPE_RENDER_UNSUPPORTED)

    return TimeParamFormula(
        ok=True,
        formula=formula.replace(TIME_SCOPE_PLACEHOLDER, f"({predicate})"),
        scopeLabel=scope.label,
    )

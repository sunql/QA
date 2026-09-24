"""评估报告对比 service（feat-dq-evaluation-report，Phase 8a）。

纯函数 ``compare``：给定左右两份 snapshot（dict），返回
``EvaluationReportCompareRead``。无副作用、不落库 —— 调用方拿两份
``EvaluationReportService.getReport`` 的 snapshot 后丢进来即可。

判定规则：
- overall：right.score - left.score（任一为 None 则 delta=None）
- dimension：按维度名（completeness/validity/...）逐个算 delta
- rule：按 ``rule_id`` 聚合两份的 pass_rate，5 状态之一：
    * NEW: 只在 right
    * REMOVED: 只在 left
    * IMPROVED: delta > 1.0
    * REGRESSED: delta < -1.0
    * UNCHANGED: |delta| ≤ 1.0
  threshold=1.0 百分比点，避免抖动噪音。
"""

from __future__ import annotations

from typing import Any

from app.domain.schemas import (
    DimensionDeltaRead,
    EvaluationReportCompareRead,
    RuleDeltaRead,
)


# 6 维度固定顺序；与 snapshot.dimensions key 对齐。
DIMENSION_KEYS: tuple[str, ...] = (
    "completeness",
    "validity",
    "uniqueness",
    "consistency",
    "timeliness",
    "referential",
)

STATUS_IMPROVED_THRESHOLD = 1.0  # 百分比点


def _safe_diff(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return right - left


def _overall_score(snapshot: dict[str, Any]) -> float | None:
    overall = snapshot.get("overall") or {}
    score = overall.get("score")
    if isinstance(score, (int, float)):
        return float(score)
    return None


def _dimension_score(
    snapshot: dict[str, Any],
    key: str,
) -> float | None:
    dims = snapshot.get("dimensions") or {}
    val = dims.get(key)
    if isinstance(val, (int, float)):
        return float(val)
    return None


def _collect_rules_by_id(
    snapshot: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    """展平 ``snapshot.tables[].rules[]`` → ``{rule_id: {rule_code, pass_rate}}``。

    同一 rule 出现在多 table 时取首个命中（同 ID 通常意味着同规则复用，pass_rate 应一致；
    若不一致以首个为准 + warning 日志由调用方负责）。
    """
    out: dict[int, dict[str, Any]] = {}
    for table in snapshot.get("tables") or []:
        for rule in table.get("rules") or []:
            rid = rule.get("rule_id")
            if not isinstance(rid, int):
                continue
            if rid in out:
                continue
            out[rid] = {
                "rule_code": rule.get("rule_code") or "",
                "pass_rate": rule.get("pass_rate"),
            }
    return out


def _classify_status(
    in_left: bool,
    in_right: bool,
    delta: float | None,
) -> str:
    if in_left and not in_right:
        return "REMOVED"
    if in_right and not in_left:
        return "NEW"
    if delta is None:
        return "UNCHANGED"
    if delta > STATUS_IMPROVED_THRESHOLD:
        return "IMPROVED"
    if delta < -STATUS_IMPROVED_THRESHOLD:
        return "REGRESSED"
    return "UNCHANGED"


def compare(
    left_id: int,
    right_id: int,
    left_snapshot: dict[str, Any],
    right_snapshot: dict[str, Any],
) -> EvaluationReportCompareRead:
    """对比两份 snapshot。纯函数，不读 DB。"""
    overall_left = _overall_score(left_snapshot)
    overall_right = _overall_score(right_snapshot)

    # dimensions
    dimension_deltas: list[DimensionDeltaRead] = []
    for key in DIMENSION_KEYS:
        l = _dimension_score(left_snapshot, key)
        r = _dimension_score(right_snapshot, key)
        dimension_deltas.append(
            DimensionDeltaRead(
                name=key,
                left=l,
                right=r,
                delta=_safe_diff(l, r),
            )
        )

    # rules
    left_rules = _collect_rules_by_id(left_snapshot)
    right_rules = _collect_rules_by_id(right_snapshot)
    all_ids = sorted(set(left_rules) | set(right_rules))

    rule_deltas: list[RuleDeltaRead] = []
    for rid in all_ids:
        l = left_rules.get(rid)
        r = right_rules.get(rid)
        l_rate = l["pass_rate"] if l else None
        r_rate = r["pass_rate"] if r else None
        delta = _safe_diff(
            float(l_rate) if l_rate is not None else None,
            float(r_rate) if r_rate is not None else None,
        )
        rule_deltas.append(
            RuleDeltaRead(
                rule_id=rid,
                rule_code=(l or r)["rule_code"] if (l or r) else "",
                pass_rate_left=float(l_rate) if l_rate is not None else None,
                pass_rate_right=float(r_rate) if r_rate is not None else None,
                delta=delta,
                status_change=_classify_status(
                    in_left=rid in left_rules,
                    in_right=rid in right_rules,
                    delta=delta,
                ),
            )
        )

    rules_in_left_only = [rid for rid in all_ids if rid in left_rules and rid not in right_rules]
    rules_in_right_only = [rid for rid in all_ids if rid in right_rules and rid not in left_rules]

    return EvaluationReportCompareRead(
        left_id=left_id,
        right_id=right_id,
        overall_left=overall_left,
        overall_right=overall_right,
        overall_delta=_safe_diff(overall_left, overall_right),
        dimension_deltas=dimension_deltas,
        rule_deltas=rule_deltas,
        rules_in_left_only=rules_in_left_only,
        rules_in_right_only=rules_in_right_only,
    )


class EvaluationReportCompareService:
    """Service 形式封装：注入到 API 路由便于依赖注入测试。"""

    def compare_by_ids(
        self,
        left_id: int,
        right_id: int,
        left_snapshot: dict[str, Any],
        right_snapshot: dict[str, Any],
    ) -> EvaluationReportCompareRead:
        return compare(left_id, right_id, left_snapshot, right_snapshot)


def get_evaluation_report_compare_service() -> EvaluationReportCompareService:
    return EvaluationReportCompareService()
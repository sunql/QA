"""场景槽位取值校验（发起编排前）——防线 B。

生产缺陷（2026-11-28）：到货跟踪场景「周期期序」填 1 ⇒ 查了**所有**供应商。
根因：工具报错后 runner 记 FAILED 但不终止 run ⇒ LLM 重规划 ⇒ 槽位丢失。

本模块守的是**放大器**：非法槽位值必须在进 LLM 之前被 422 拦下。

- 通用规则：number 槽位按 slot_schema 声明的 minimum / maximum 校验；
- 场景专属规则：delivery_tracking 场景 periodIndex 必须与 anchorDay 成对。
"""

from __future__ import annotations

import pytest

from app.domain.exceptions import ValidationError
from app.services.copilot_scenario_slot_validation import (
    DELIVERY_TRACKING_AGENT_CODE,
    validateScenarioSlotValues,
)

#: 到货跟踪场景的槽位定义（与 seed_copilot_scenarios.py 同构）。
DELIVERY_SLOT_SCHEMA: list[dict] = [
    {
        "key": "monthsBack",
        "type": "number",
        "label": "时间窗",
        "minimum": 1,
        "maximum": 36,
    },
    {
        "key": "anchorDay",
        "type": "number",
        "label": "月度周期锚点日",
        "minimum": 1,
        "maximum": 28,
    },
    {
        "key": "periodIndex",
        "type": "number",
        "label": "周期期序",
        "minimum": -12,
        "maximum": 0,
        "hint": "0=本期，-1=上期",
    },
    {"key": "supplierKeys", "type": "entityMulti", "label": "供应商"},
]


def _validate(
    slots: dict,
    slotSchema: list[dict] | None = None,
    agentCode: str = DELIVERY_TRACKING_AGENT_CODE,
) -> None:
    validateScenarioSlotValues(
        slots=slots,
        slotSchema=slotSchema if slotSchema is not None else DELIVERY_SLOT_SCHEMA,
        agentCode=agentCode,
    )


# ── 通用规则：number 槽位的范围约束 ────────────────────────────────────────


def test_value_above_maximum_is_rejected_with_label_and_range() -> None:
    """越界 ⇒ ValidationError，消息点名槽位 label 与允许范围。"""
    with pytest.raises(ValidationError) as exc:
        _validate({"periodIndex": 1})

    message = str(exc.value)
    assert "周期期序" in message
    assert "-12..0" in message
    assert "收到 1" in message


def test_value_below_minimum_is_rejected() -> None:
    """低于下界同样拦（周期期序往前 12 期以外无意义）。"""
    with pytest.raises(ValidationError) as exc:
        _validate({"periodIndex": -13})

    message = str(exc.value)
    assert "周期期序" in message
    assert "-12..0" in message


def test_value_inside_range_passes() -> None:
    """范围内的取值（本期 0 / 上期 -1）不被拦。"""
    _validate({"periodIndex": 0, "anchorDay": 24})
    _validate({"periodIndex": -1, "anchorDay": 24})
    _validate({"periodIndex": -12, "anchorDay": 24})


def test_number_slot_without_min_max_is_not_checked() -> None:
    """未声明 min/max 的 number 槽位 ⇒ 不拦（留给既有逻辑）。"""
    schema: list[dict] = [
        {"key": "someCount", "type": "number", "label": "某个计数"},
    ]
    _validate({"someCount": 9999}, slotSchema=schema)


def test_non_number_slot_is_not_checked() -> None:
    """非 number 类型的槽位 ⇒ 不参与范围校验。"""
    schema: list[dict] = [
        {
            "key": "supplierKeys",
            "type": "entityMulti",
            "label": "供应商",
            "minimum": -12,
            "maximum": 0,
        },
    ]
    _validate({"supplierKeys": ["B109"]}, slotSchema=schema)


def test_missing_slot_value_is_not_checked() -> None:
    """槽位没给值 ⇒ 不校验（必填与否由既有逻辑管）。"""
    _validate({"supplierKeys": ["B109"]})


def test_non_numeric_value_is_not_checked() -> None:
    """非数值取值 ⇒ 不做范围校验（交给既有逻辑/工具侧）。"""
    schema: list[dict] = [
        {"key": "someCount", "type": "number", "label": "某个计数", "minimum": 0, "maximum": 10},
    ]
    _validate({"someCount": "abc"}, slotSchema=schema, agentCode="PROCUREMENT_SOURCING")


# ── 场景专属规则：periodIndex / anchorDay 成对 ─────────────────────────────


def test_delivery_tracking_period_index_without_anchor_day_is_rejected() -> None:
    """给了周期期序却没给锚点日 ⇒ 拦（工具侧同款规则，提前到发起前）。"""
    with pytest.raises(ValidationError) as exc:
        _validate({"periodIndex": 0})

    message = str(exc.value)
    assert "周期期序" in message
    assert "月度周期锚点日" in message


def test_delivery_tracking_period_index_zero_with_anchor_day_passes() -> None:
    """本期（0）+ 锚点日 ⇒ 通过。"""
    _validate({"periodIndex": 0, "anchorDay": 24})


def test_delivery_tracking_period_index_minus_one_with_anchor_day_passes() -> None:
    """上期（-1）+ 锚点日 ⇒ 通过。"""
    _validate({"periodIndex": -1, "anchorDay": 24})


def test_delivery_tracking_anchor_day_without_period_index_passes() -> None:
    """只给锚点日 ⇒ 通过（periodIndex 可选，缺省 0=本期）。"""
    _validate({"anchorDay": 24})


def test_pair_check_does_not_apply_to_other_agents() -> None:
    """成对校验只针对 delivery_tracking 场景（其它 Agent 不受影响）。"""
    _validate({"periodIndex": 0}, agentCode="PROCUREMENT_SOURCING")

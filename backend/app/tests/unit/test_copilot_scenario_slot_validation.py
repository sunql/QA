"""场景槽位取值校验（发起编排前）——防线 B。

生产缺陷（2026-11-28）：到货跟踪场景「周期期序」填 1 ⇒ 查了**所有**供应商。
根因：工具报错后 runner 记 FAILED 但不终止 run ⇒ LLM 重规划 ⇒ 槽位丢失。

本模块守的是**放大器**：非法槽位值必须在进 LLM 之前被 422 拦下。

- 配置层通用规则：number 槽位按 slot_schema 声明的 minimum / maximum 校验；
- 契约层硬约束：periodIndex ≤ 0、anchorDay 1..28（真值在代码里 ⇒ 生产
  库没打 seed 补丁也生效）；
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

#: **已部署库的旧形态**：槽位在，但没有 minimum / maximum（seed 不覆盖 ⇒
#: 新增字段拿不到）。配置层读不到范围 ⇒ 必须靠契约层兜住。
DELIVERY_SLOT_SCHEMA_WITHOUT_RANGE: list[dict] = [
    {
        "key": "anchorDay",
        "type": "number",
        "label": "月度周期锚点日",
    },
    {
        "key": "periodIndex",
        "type": "number",
        "label": "周期期序",
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


# ── 契约层硬约束（真值在代码里 ⇒ 不依赖场景配置）─────────────────────────


def test_contract_blocks_positive_period_index_without_configured_range() -> None:
    """**DB 里没有 min/max 时，periodIndex=1 仍被拦**（契约层兜底）。

    这是 seed 不覆盖已存在场景行时的最后一道防线：配置层读不到范围，
    契约层（periodIndex ≤ 0）照样拦 ⇒ 生产不会漏放。
    """
    with pytest.raises(ValidationError) as exc:
        _validate(
            {"periodIndex": 1},
            slotSchema=DELIVERY_SLOT_SCHEMA_WITHOUT_RANGE,
        )

    message = str(exc.value)
    assert "周期期序" in message
    assert "正数" in message


def test_contract_allows_zero_and_negative_period_index_without_configured_range() -> None:
    """没有配置范围时，0 / 负数仍合法（契约只禁正数）。"""
    _validate(
        {"periodIndex": 0, "anchorDay": 24},
        slotSchema=DELIVERY_SLOT_SCHEMA_WITHOUT_RANGE,
    )
    _validate(
        {"periodIndex": -1, "anchorDay": 24},
        slotSchema=DELIVERY_SLOT_SCHEMA_WITHOUT_RANGE,
    )


def test_contract_blocks_anchor_day_outside_tool_range() -> None:
    """锚点日 29 超出工具契约（1..28）⇒ 拦（哪怕配置没写范围）。"""
    with pytest.raises(ValidationError) as exc:
        _validate(
            {"anchorDay": 29},
            slotSchema=DELIVERY_SLOT_SCHEMA_WITHOUT_RANGE,
        )

    assert "月度周期锚点日" in str(exc.value)


def test_contract_does_not_apply_to_other_agents() -> None:
    """契约层只针对 delivery_tracking 场景（其它 Agent 不受影响）。"""
    _validate(
        {"periodIndex": 1},
        slotSchema=DELIVERY_SLOT_SCHEMA_WITHOUT_RANGE,
        agentCode="PROCUREMENT_SOURCING",
    )


def test_contract_ignores_scenarios_without_the_slot() -> None:
    """场景没声明 periodIndex 槽位 ⇒ 契约层不误报。"""
    schema: list[dict] = [
        {"key": "supplierKeys", "type": "entityMulti", "label": "供应商"},
    ]
    _validate({"periodIndex": 1}, slotSchema=schema)


# ── 成对校验的适用范围（不误报）────────────────────────────────────────────


def test_pair_check_skipped_when_anchor_day_slot_not_declared() -> None:
    """场景绑了 delivery_tracking 但**没声明 anchorDay 槽位** ⇒ 不做成对校验。

    否则会对「本就无此槽位」的场景误报「必须与 anchorDay 一起使用」。
    """
    schema: list[dict] = [
        {"key": "periodIndex", "type": "number", "label": "周期期序"},
    ]
    _validate({"periodIndex": 0}, slotSchema=schema)


def test_pair_check_skipped_when_period_index_slot_not_declared() -> None:
    """场景没声明 periodIndex 槽位 ⇒ 不做成对校验。"""
    schema: list[dict] = [
        {"key": "anchorDay", "type": "number", "label": "月度周期锚点日"},
    ]
    _validate({"anchorDay": 24}, slotSchema=schema)


# ── min / max 类型硬化（管理员写成字符串也不能 500）────────────────────────


def test_minimum_and_maximum_given_as_strings_are_compared_numerically() -> None:
    """配置里 min/max 写成字符串（"0"）⇒ 仍按数值比较（不 TypeError）。"""
    schema: list[dict] = [
        {
            "key": "periodIndex",
            "type": "number",
            "label": "周期期序",
            "minimum": "-12",
            "maximum": "0",
        },
    ]
    with pytest.raises(ValidationError) as exc:
        _validate({"periodIndex": 1}, slotSchema=schema)

    assert "周期期序" in str(exc.value)

    _validate({"periodIndex": -1}, slotSchema=schema)


def test_non_numeric_minimum_is_skipped_without_error() -> None:
    """min/max 写成非数值 ⇒ 跳过该侧比较（不 500、不误报）。"""
    schema: list[dict] = [
        {
            "key": "someCount",
            "type": "number",
            "label": "某个计数",
            "minimum": "abc",
            "maximum": 10,
        },
    ]
    _validate({"someCount": 5}, slotSchema=schema, agentCode="PROCUREMENT_SOURCING")
    with pytest.raises(ValidationError):
        _validate({"someCount": 99}, slotSchema=schema, agentCode="PROCUREMENT_SOURCING")

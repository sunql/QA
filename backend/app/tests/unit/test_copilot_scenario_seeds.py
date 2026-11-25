"""场景种子配置 lint（P4）：配置即资产，坏配置在测试期拦截而不是运行时。

- result_renderer / slot type 必须在白名单内（与保存校验同口径）；
- codePattern 必须是合法 regex；
- chips 是「直接发送」的预设槽位组合 ⇒ 必填槽位必须有值（否则 422）。
"""

from __future__ import annotations

import pytest

from app.domain.copilot_scenario_models import (
    RESULT_RENDERER_WHITELIST,
    SLOT_TYPE_WHITELIST,
)
from app.services.copilot_scenario_render import compileCodePattern
from scripts.seed_copilot_scenarios import SEEDS

if not SEEDS:  # pragma: no cover —— 种子清单为空说明被误改，直接失败
    pytest.fail("SEEDS 为空：场景种子被清空？")


def test_seeds_pass_whitelists_and_patterns() -> None:
    for seed in SEEDS:
        assert seed["result_renderer"] in RESULT_RENDERER_WHITELIST, seed["scenario_code"]
        # 场景分支不强制 PROCUREMENT_ 前缀（Agent 由配置钉定；如
        # SUPPLIER_RISK_AGENT），但必须是 seed_agents 登记过的采购域 Agent
        assert str(seed["agent_code"]).strip(), seed["scenario_code"]
        for slot in seed["slot_schema"]:
            assert slot["type"] in SLOT_TYPE_WHITELIST, (seed["scenario_code"], slot)
            assert str(slot.get("key", "")).strip(), (seed["scenario_code"], slot)
            assert str(slot.get("label", "")).strip(), (seed["scenario_code"], slot)
            if slot.get("codePattern"):
                compileCodePattern(str(slot["codePattern"]))


def test_chips_satisfy_required_slots() -> None:
    for seed in SEEDS:
        required = {
            slot["key"] for slot in seed["slot_schema"] if slot.get("required")
        }
        for chip in seed["chips"]:
            for key in required:
                assert chip.get(key) not in (None, "", []), (
                    f"{seed['scenario_code']} chip「{chip['label']}」缺必填槽位 {key}"
                )


# ── delivery_tracking 场景（P5 到货跟踪）───────────────────────────────────


def _deliveryTrackingSeed() -> dict:
    return next(s for s in SEEDS if s["scenario_code"] == "delivery_tracking")


def test_delivery_tracking_scenario_has_no_datasource_slot() -> None:
    """THBI 硬规则：场景不提供数据源切换槽位。"""
    seed = _deliveryTrackingSeed()
    keys = {slot["key"] for slot in seed["slot_schema"]}
    assert "datasourceId" not in keys
    assert seed["result_renderer"] == "delivery_tracking"
    assert seed["agent_code"] == "DELIVERY_TRACKING_AGENT"


def test_delivery_tracking_scenario_has_no_material_slot() -> None:
    """编排方裁决移除 materialKeys：用户原始需求 1-4 不含按物料过滤。

    工具侧也未实现该过滤；若有人照旧 brief 加回该槽位，这里是拦截点。
    """
    seed = _deliveryTrackingSeed()
    keys = {slot["key"] for slot in seed["slot_schema"]}
    assert "materialKeys" not in keys


def test_delivery_tracking_scenario_has_anchor_slots() -> None:
    """24 日锚点槽位齐（anchorDay / periodIndex）。"""
    seed = _deliveryTrackingSeed()
    keys = {slot["key"] for slot in seed["slot_schema"]}
    assert {"monthsBack", "anchorDay", "periodIndex", "supplierKeys"} <= keys


def test_delivery_tracking_agent_seeded() -> None:
    """Agent 已登记且默认绑定到货跟踪工具。"""
    from scripts.seed_agents import _AGENT_DEFAULT_BINDINGS, AGENT_SEEDS

    assert "DELIVERY_TRACKING_AGENT" in _AGENT_DEFAULT_BINDINGS
    assert _AGENT_DEFAULT_BINDINGS["DELIVERY_TRACKING_AGENT"] == "delivery_tracking_query"
    codes = {a["agent_code"] for a in AGENT_SEEDS}
    assert "DELIVERY_TRACKING_AGENT" in codes


def test_result_renderer_whitelist_includes_delivery_tracking() -> None:
    from app.domain.copilot_scenario_models import RESULT_RENDERER_WHITELIST

    assert "delivery_tracking" in RESULT_RENDERER_WHITELIST


def test_delivery_tracking_scenario_agent_binding_is_consistent() -> None:
    """场景钉的 Agent 必须真的绑定 delivery_tracking_query 工具。"""
    from scripts.seed_agents import _AGENT_DEFAULT_BINDINGS

    seed = _deliveryTrackingSeed()
    agent_code = seed["agent_code"]
    assert agent_code in _AGENT_DEFAULT_BINDINGS
    assert _AGENT_DEFAULT_BINDINGS[agent_code] == "delivery_tracking_query"

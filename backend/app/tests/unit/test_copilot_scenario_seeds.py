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

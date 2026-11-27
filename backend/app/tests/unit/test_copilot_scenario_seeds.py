"""场景种子配置 lint（P4）：配置即资产，坏配置在测试期拦截而不是运行时。

- result_renderer / slot type 必须在白名单内（与保存校验同口径）；
- codePattern 必须是合法 regex；
- chips 是「直接发送」的预设槽位组合 ⇒ 必填槽位必须有值（否则 422）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.domain.copilot_scenario_models import (
    RESULT_RENDERER_WHITELIST,
    SLOT_TYPE_WHITELIST,
    CopilotScenario,
)
from app.services.copilot_scenario_render import compileCodePattern
from scripts.seed_copilot_scenarios import SEEDS, seed_copilot_scenarios

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


# ── pending_delivery 场景（THBI 硬规则：无数据源切换）──────────────────────


def _pendingDeliverySeed() -> dict:
    return next(s for s in SEEDS if s["scenario_code"] == "pending_delivery")


def test_pending_delivery_scenario_has_no_datasource_slot() -> None:
    """THBI 硬规则：既有 pending_delivery 场景也不再提供数据源槽位。"""
    seed = _pendingDeliverySeed()
    keys = {slot["key"] for slot in seed["slot_schema"]}
    assert "datasourceId" not in keys


def test_pending_delivery_slot_keys_are_tool_input_properties() -> None:
    """场景槽位 key ⊆ pending_delivery_query 的 input_schema（同到货跟踪交叉守卫）。"""
    from scripts.seed_agent_tool_configs import TOOL_SEEDS

    seed = _pendingDeliverySeed()
    tool = next(t for t in TOOL_SEEDS if t["name"] == "pending_delivery_query")
    properties = set(tool["input_schema"]["properties"])
    extra = {slot["key"] for slot in seed["slot_schema"]} - properties
    assert not extra, f"场景槽位不在工具 input_schema 中：{sorted(extra)}"


# ── delivery_tracking 场景（P5 到货跟踪）───────────────────────────────────


def _deliveryTrackingSeed() -> dict:
    return next(s for s in SEEDS if s["scenario_code"] == "delivery_tracking")


def test_delivery_tracking_scenario_has_no_datasource_slot() -> None:
    """THBI 硬规则：场景不提供数据源切换槽位。"""
    seed = _deliveryTrackingSeed()
    keys = {slot["key"] for slot in seed["slot_schema"]}
    assert "datasourceId" not in keys


def test_delivery_tracking_scenario_pins_renderer_and_agent() -> None:
    seed = _deliveryTrackingSeed()
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


def test_delivery_tracking_scenario_has_hidden_date_slots() -> None:
    """时间区间模式写入的 dateFrom/dateTo 必须存在且隐藏（否则区间静默失效）。"""
    seed = _deliveryTrackingSeed()
    keyed = {slot["key"]: slot for slot in seed["slot_schema"]}
    for key in ("dateFrom", "dateTo"):
        assert key in keyed, f"缺少隐藏日期槽位 {key}"
        assert keyed[key].get("hidden") is True, key
        assert keyed[key]["type"] == "text", key


def test_delivery_tracking_chips_are_expected() -> None:
    """4 条推荐 chip 的具体取值（近 12/3 月 + 本期/上期 24 日）。"""
    seed = _deliveryTrackingSeed()
    assert len(seed["chips"]) == 4
    assert [c.get("monthsBack") for c in seed["chips"]] == [12, 3, None, None]
    assert [c.get("anchorDay") for c in seed["chips"]] == [None, None, 24, 24]
    assert [c.get("periodIndex") for c in seed["chips"]] == [None, None, 0, -1]


def test_delivery_tracking_slot_keys_are_tool_input_properties() -> None:
    """场景槽位 key ⊆ delivery_tracking_query 的 input_schema 属性。

    工具声明 ``additionalProperties: false``——多出一个槽位 key 就意味着
    后端渲染的参数必然被工具拒绝。这是场景↔工具最有价值的一道交叉守卫。
    """
    from scripts.seed_agent_tool_configs import TOOL_SEEDS

    seed = _deliveryTrackingSeed()
    tool = next(t for t in TOOL_SEEDS if t["name"] == "delivery_tracking_query")
    properties = set(tool["input_schema"]["properties"])
    extra = {slot["key"] for slot in seed["slot_schema"]} - properties
    assert not extra, f"场景槽位不在工具 input_schema 中：{sorted(extra)}"


def test_delivery_tracking_agent_seeded() -> None:
    """Agent 已登记且默认绑定到货跟踪工具。"""
    from scripts.seed_agents import _AGENT_DEFAULT_BINDINGS, AGENT_SEEDS

    assert "DELIVERY_TRACKING_AGENT" in _AGENT_DEFAULT_BINDINGS
    assert _AGENT_DEFAULT_BINDINGS["DELIVERY_TRACKING_AGENT"] == "delivery_tracking_query"
    codes = {a["agent_code"] for a in AGENT_SEEDS}
    assert "DELIVERY_TRACKING_AGENT" in codes


def test_delivery_tracking_agent_profile_seeded() -> None:
    """编排 profile：绑定 delivery_tracking_query，预算上限符合约定。"""
    from scripts.seed_procurement_agent_profiles import PROFILE_SEEDS

    instructions, tools, maxIterations, maxToolCalls = PROFILE_SEEDS[
        "DELIVERY_TRACKING_AGENT"
    ]
    assert "delivery_tracking_query" in tools
    assert maxIterations == 12
    assert maxToolCalls == 18
    assert "delivery_tracking_query" in instructions
    assert "不要" in instructions  # 结构化数据由前端渲染，文字不复述数字


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


# ── 过时槽位定向清理（已部署库的 datasourceId 残留）─────────────────────────


def _legacyScenario(*, scenarioCode: str, slotKeys: tuple[str, ...]) -> CopilotScenario:
    """构造一条「历史版本」场景行（slot_schema 含指定键，其余字段借种子里同构值）。"""
    base = next(s for s in SEEDS if s["scenario_code"] == "pending_delivery")
    return CopilotScenario(
        **{
            **base,
            "scenario_code": scenarioCode,
            "slot_schema": [
                {"key": key, "type": "number", "label": key} for key in slotKeys
            ],
        }
    )


async def _insertScenario(factory, scenario: CopilotScenario) -> None:
    async with factory() as session:
        session.add(scenario)
        await session.commit()


async def _readSlotKeys(factory, code: str) -> list[str] | None:
    async with factory() as session:
        row = (
            await session.execute(
                select(CopilotScenario).where(CopilotScenario.scenario_code == code)
            )
        ).scalars().first()
        if row is None:
            return None
        return [slot["key"] for slot in row.slot_schema]


@pytest.mark.asyncio
async def test_seed_cleanup_removes_only_retired_slot_and_is_idempotent(
    seedEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    """只删显式退役的 datasourceId；管理员自加槽位保留；第二次运行无变化。"""
    factory, _ = seedEngine
    await _insertScenario(
        factory,
        _legacyScenario(
            scenarioCode="pending_delivery",
            slotKeys=("monthsBack", "datasourceId", "customSlot"),
        ),
    )

    await seed_copilot_scenarios(factory)
    firstRun = capsys.readouterr().out
    assert "cleaned pending_delivery: removed slot keys ['datasourceId']" in firstRun
    keys = await _readSlotKeys(factory, "pending_delivery")
    assert keys is not None
    assert "datasourceId" not in keys, "退役键必须被清理"
    assert "customSlot" in keys, "只删退役键，管理员新增槽位不得误伤"

    await seed_copilot_scenarios(factory)
    secondRun = capsys.readouterr().out
    assert "cleaned" not in secondRun, "无变化时不得写库/打印（幂等）"
    assert await _readSlotKeys(factory, "pending_delivery") == keys


@pytest.mark.asyncio
async def test_seed_cleanup_does_not_touch_unseeded_scenario(
    seedEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    """只处理 SEEDS 覆盖到的 scenario_code ⇒ 其它行原样保留。"""
    factory, _ = seedEngine
    await _insertScenario(
        factory,
        _legacyScenario(
            scenarioCode="admin_scen", slotKeys=("monthsBack", "datasourceId")
        ),
    )

    await seed_copilot_scenarios(factory)
    out = capsys.readouterr().out
    assert "cleaned admin_scen" not in out
    assert await _readSlotKeys(factory, "admin_scen") == ["monthsBack", "datasourceId"]


# ── 槽位取值范围（2026-11-28 生产缺陷：周期期序填正数 ⇒ 查了所有供应商）─────


def test_delivery_tracking_period_index_declares_range() -> None:
    """周期期序必须声明 minimum=-12 / maximum=0（0=本期、-1=上期，正数非法）。

    不声明 ⇒ 前端 InputNumber 无限制 ⇒ 用户能填 1 ⇒ 工具报错 → runner 不终止
    run → LLM 重规划丢槽位 ⇒ 查了所有供应商。
    """
    seed = _deliveryTrackingSeed()
    keyed = {slot["key"]: slot for slot in seed["slot_schema"]}
    assert keyed["periodIndex"]["minimum"] == -12
    assert keyed["periodIndex"]["maximum"] == 0


def test_delivery_tracking_anchor_day_range_matches_tool() -> None:
    """锚点日范围与工具契约一致（delivery_tracking_query._MAX_ANCHOR_DAY）。"""
    from app.services.delivery_tracking_query import _MAX_ANCHOR_DAY

    seed = _deliveryTrackingSeed()
    keyed = {slot["key"]: slot for slot in seed["slot_schema"]}
    assert keyed["anchorDay"]["minimum"] == 1
    assert keyed["anchorDay"]["maximum"] == _MAX_ANCHOR_DAY


def test_delivery_tracking_months_back_range_matches_tool() -> None:
    """时间窗范围与工具契约一致（delivery_tracking_query._MIN/_MAX_MONTHS_BACK）。"""
    from app.services.delivery_tracking_query import (
        _MAX_MONTHS_BACK,
        _MIN_MONTHS_BACK,
    )

    seed = _deliveryTrackingSeed()
    keyed = {slot["key"]: slot for slot in seed["slot_schema"]}
    assert keyed["monthsBack"]["minimum"] == _MIN_MONTHS_BACK
    assert keyed["monthsBack"]["maximum"] == _MAX_MONTHS_BACK


def test_pending_delivery_slot_ranges_declared() -> None:
    """待到货场景也声明范围（前端此前把 min=0 写死在控件里 ⇒ 收进配置）。"""
    seed = _pendingDeliverySeed()
    keyed = {slot["key"]: slot for slot in seed["slot_schema"]}
    assert keyed["minOverdueDays"]["minimum"] == 0
    assert keyed["monthsBack"]["minimum"] == 1
    assert keyed["monthsBack"]["maximum"] == 36

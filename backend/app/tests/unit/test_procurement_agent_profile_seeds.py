"""采购场景 Agent 的 profile 种子契约。

两条不变量（都是**行为**层面的，不是文本形状）：

1. 每个 profile 绑定的工具都必须存在于 `seed_agent_tool_configs.TOOL_SEEDS` ——
   否则播种后运行期会「工具解析失败 ⇒ run FAILED」；
2. 供应商检索 Agent 必须绑 `material_search` —— 否则「外购标准件」这类**品类词**
   无法先解析成物料，`supplier_search` 只能反问（2026-11-23 生产实测：
   run 停在 `PAUSED/NEEDS_INPUT`，step 为 `ASK_USER「缺少可检索的物料编码或供应商关键词」`）。
"""

from __future__ import annotations

from scripts.seed_agent_tool_configs import TOOL_SEEDS
from scripts.seed_procurement_agent_profiles import PROFILE_SEEDS

SEEDED_TOOL_NAMES = {seed["name"] for seed in TOOL_SEEDS}


class TestProcurementAgentProfileSeeds:
    def test_every_bound_tool_exists_in_tool_seeds(self) -> None:
        """绑了未播种的工具 ⇒ 运行期工具解析必失败。"""
        missing: dict[str, list[str]] = {}
        for agentCode, (_instructions, toolNames, _it, _tc) in PROFILE_SEEDS.items():
            unknown = sorted(set(toolNames) - SEEDED_TOOL_NAMES)
            if unknown:
                missing[agentCode] = unknown
        assert not missing, f"以下 profile 绑定了未播种的工具：{missing}"

    def test_profiles_count_floor(self) -> None:
        """数量下限：防止 PROFILE_SEEDS 结构改名后守卫空集通过。"""
        assert len(PROFILE_SEEDS) >= 6

    def test_supplier_search_agent_can_resolve_material_first(self) -> None:
        """供应商检索 Agent 必须能先把物料/品类解析成实体。"""
        _instructions, toolNames, _it, _tc = PROFILE_SEEDS[
            "PROCUREMENT_SUPPLIER_SEARCH_AGENT"
        ]
        assert "material_search" in toolNames
        assert "supplier_search" in toolNames
        assert "supplier_360" in toolNames

    def test_supplier_search_instructions_mention_material_first(self) -> None:
        """指令里要写清「先定位物料再查供应商」，否则模型仍可能直接反问。"""
        instructions, _tools, _it, _tc = PROFILE_SEEDS[
            "PROCUREMENT_SUPPLIER_SEARCH_AGENT"
        ]
        assert "material_search" in instructions

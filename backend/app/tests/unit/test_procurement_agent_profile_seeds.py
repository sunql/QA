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

    def test_profile_tools_are_all_in_orchestration_readonly_whitelist(self) -> None:
        """**跨文件不变式**：profile 绑的每个工具都必须在编排只读白名单里。

        为什么需要这条（2026-11-25 本地端到端实测踩到）：
        `READ_ONLY_HANDLER_REFS`（`agent_orchestration_validation`）是编排能否
        绑定/调用某工具的唯一闸门。新工具只加到 `TOOL_SEEDS` + `BUILTIN_HANDLERS`
        而漏了白名单时，`seed_procurement_agent_profiles` 直接 `ValidationError`，
        整个 Agent 拿不到 profile ⇒ Copilot 返回 **409「尚无编排配置」**。

        该失败越过当时全部单测（各自看各自清单），只在真实启动+建 profile 时暴露。
        这条不变式把三个清单钉在一起，防止同类问题（当时已第 4 次）。
        """
        from app.services.agent_orchestration_validation import (
            READ_ONLY_HANDLER_REFS,
        )

        offenders: dict[str, list[str]] = {}
        for agentCode, (_instructions, toolNames, _it, _tc) in PROFILE_SEEDS.items():
            notWhitelisted = sorted(set(toolNames) - READ_ONLY_HANDLER_REFS)
            if notWhitelisted:
                offenders[agentCode] = notWhitelisted
        assert not offenders, (
            "以下 profile 绑定了不在编排只读白名单里的工具"
            f"（会导致该 Agent 拿不到 profile、Copilot 409）：{offenders}"
        )

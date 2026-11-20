"""M3-3 采购 Agent 场景目录（`GET /procurement/agents/catalog`）。

判据要点：
1. **能力面非实现面**：目录条目绝不携带 handlerRef/handler 字段；
2. **可运行性自证**：runnable = tool_name 非空 + config enabled；
   占位 Agent（PROCUREMENT_COPILOT 未绑工具）⇒ runnable=False 且 tool=None；
3. **目录口径**：仅 `PROCUREMENT_` 前缀的场景 Agent（全局 Agent 不进目录）；
4. **种子一致性**：`_AGENT_DEFAULT_BINDINGS` 的采购新绑定 ⇒ 工具必须在
   TOOL_SEEDS（config 元数据）与 BUILTIN_HANDLERS（handler 引擎）中同时存在
   ——三者缺一 ⇒ seed 出来的 Agent 不可运行（fail-closed 但目录会误导）；
5. **双接线守卫**：main.py 与 _testapp.py 同块注册。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.services.agent_catalog import buildProcurementCatalog


def _row(code: str, name: str, tool: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        agent_code=code, agent_name=name, description=f"{name} 描述",
        tool_name=tool, status="active",
    )


def _config(name: str, *, enabled: bool = True, obj: str = "SUPPLIER") -> SimpleNamespace:
    return SimpleNamespace(
        name=name, description=f"{name} 工具能力", data_object=obj,
        data_layers=("DIM",), enabled=enabled,
    )


def test_catalog_builds_entries_with_tool_capability() -> None:
    rows = [_row("PROCUREMENT_SUPPLIER_SEARCH_AGENT", "采购供应商检索 Agent", "supplier_search")]
    configs = {"supplier_search": _config("supplier_search")}
    entries = buildProcurementCatalog(rows, configs)
    assert len(entries) == 1
    e = entries[0]
    assert e.agent_code == "PROCUREMENT_SUPPLIER_SEARCH_AGENT"
    assert e.runnable is True
    assert e.tool is not None and e.tool.name == "supplier_search"
    dumped = e.model_dump(by_alias=True)
    assert "agentCode" in dumped and "runnable" in dumped
    assert "handlerRef" not in dumped and "handler" not in dumped, (
        "能力面非实现面：目录绝不携带 handlerRef"
    )
    assert "handler" not in (dumped["tool"] or {})


def test_catalog_unbound_agent_is_not_runnable() -> None:
    rows = [_row("PROCUREMENT_COPILOT", "采购 AI 副驾")]
    entries = buildProcurementCatalog(rows, {})
    assert entries[0].runnable is False and entries[0].tool is None


def test_catalog_disabled_tool_is_not_runnable() -> None:
    rows = [_row("PROCUREMENT_X_AGENT", "X", "supplier_search")]
    configs = {"supplier_search": _config("supplier_search", enabled=False)}
    entries = buildProcurementCatalog(rows, configs)
    assert entries[0].runnable is False
    assert entries[0].tool is not None, "config 存在 ⇒ 能力说明仍展示"


def test_catalog_unknown_tool_is_not_runnable() -> None:
    rows = [_row("PROCUREMENT_Y_AGENT", "Y", "ghost_tool")]
    entries = buildProcurementCatalog(rows, {})
    assert entries[0].runnable is False and entries[0].tool is None


# ── 种子一致性 ────────────────────────────────────────────────────────────


def test_procurement_seed_bindings_consistent_with_tools_and_handlers() -> None:
    """采购新绑定的工具必须同时存在于 TOOL_SEEDS 与 BUILTIN_HANDLERS。"""
    from scripts.seed_agent_tool_configs import TOOL_SEEDS
    from scripts.seed_agents import _AGENT_DEFAULT_BINDINGS

    toolNames = {s["name"] for s in TOOL_SEEDS}
    for agentCode, toolName in _AGENT_DEFAULT_BINDINGS.items():
        if not agentCode.startswith("PROCUREMENT_"):
            continue
        assert toolName in toolNames, (
            f"{agentCode} 绑定的 {toolName} 不在 TOOL_SEEDS ⇒ config 元数据缺失"
        )
        from app.services.agent_tools import BUILTIN_HANDLERS

        assert toolName in BUILTIN_HANDLERS, (
            f"{agentCode} 绑定的 {toolName} 不在 BUILTIN_HANDLERS ⇒ handler 引擎缺失"
        )


# ── 双接线守卫 ────────────────────────────────────────────────────────────


def test_agent_catalog_registered_in_both_wirings() -> None:
    backendRoot = Path(__file__).resolve().parents[3]
    for rel in ("app/main.py", "app/tests/_testapp.py"):
        code = (backendRoot / rel).read_text(encoding="utf-8")
        assert "procurement_agents" in code, f"{rel} 未导入新路由模块"
        assert "procurement_agents.router" in code, (
            f"{rel} 未注册 agents/catalog 路由（漏一边 ⇒ 那一侧 404）"
        )

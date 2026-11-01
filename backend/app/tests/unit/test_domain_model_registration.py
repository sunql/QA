"""四个模型模块必须进 `Base.metadata`。

漂移检测的两个入口看到的 ORM 视图必须一致：
- 应用进程（`app/main.py` lifespan）→ `_checkDriftAsync`
- 独立 CLI（`python -m app.infrastructure.schema_drift`）→ 同一实现

CLI 只 import `app.domain.models`，所以「谁没被 models.py 导入」谁就在 CLI 下凭空消失，
被误报成 `extra_table`（0084 的 `mcp_call_log` 就是这样长期被报成多余表）。
"""

from __future__ import annotations

from app.domain.models import Base


def test_mcp_call_log_is_registered() -> None:
    """0084 建表 + ORM 模型齐全，只差 import。"""
    assert "mcp_call_log" in Base.metadata.tables


def test_research_tables_are_registered() -> None:
    """0111–0138 的研究线表。"""
    for name in ("research_session", "research_task", "research_report"):
        assert name in Base.metadata.tables, name


def test_multi_step_tables_are_registered() -> None:
    """0114/0115 多步持久化表。"""
    for name in ("multi_step_run", "multi_step_step"):
        assert name in Base.metadata.tables, name


def test_wiki_graph_insight_is_registered() -> None:
    """0067 的 wiki 图谱洞察表。"""
    assert "wiki_graph_insight" in Base.metadata.tables


def test_registration_does_not_change_existing_table_count() -> None:
    """反向守卫：注册只增不减，且不得把既有表挤掉。"""
    assert "users" in Base.metadata.tables
    assert "knowledge_claim" in Base.metadata.tables

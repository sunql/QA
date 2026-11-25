"""DIM_FACILITY 源绑定种子：幂等 + 必填字段齐（2026-11-25）。"""

from __future__ import annotations

from scripts import seed_procurement_bindings as mod


def test_facility_binding_spec_has_required_fields() -> None:
    """绑定规格：THBI 数据源 + BASE 角色 + 已确认（不是候选）。"""
    spec = mod.FACILITY_BINDING
    assert spec["datasourceId"] == 1
    assert spec["schemaName"] == "THBI"
    assert spec["sourceTable"] == "DIM_FACILITY"
    assert spec["bindingRole"] == "BASE"
    assert spec["isCandidate"] is False
    assert spec["confirmedBy"]


def test_facility_binding_spec_is_idempotent_keyed_by_class() -> None:
    """幂等键 = 类名（同一类只建一条绑定）。"""
    assert mod.FACILITY_BINDING["className"] == "DIM_FACILITY"


def test_facility_binding_spec_matches_resolver_hard_rules() -> None:
    """种子规格必须与解析器的硬闸常量一致（数据源 / schema / 角色）。

    为什么：解析器只认 `THBI_DATASOURCE_ID` + `THBI_SCHEMA_NAME` + `BASE` 角色
    ⇒ 种子写错任何一个常量 ⇒ 绑定写进去了但解析器永远读不到（静默失效）。
    ⇒ 用**同一份常量**钉住，而不是各写一份字面量。
    """
    from app.services.ontology_binding_resolver import (
        BINDING_ROLE_BASE,
        THBI_DATASOURCE_ID,
        THBI_SCHEMA_NAME,
    )

    spec = mod.FACILITY_BINDING
    assert spec["datasourceId"] == THBI_DATASOURCE_ID
    assert spec["schemaName"] == THBI_SCHEMA_NAME
    assert spec["bindingRole"] == BINDING_ROLE_BASE


def test_seed_entrypoint_is_exported() -> None:
    """脚本对外只暴露一个幂等入口（供 CLI 与集成测试复用）。"""
    assert callable(mod.seed_facility_binding)

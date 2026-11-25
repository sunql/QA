"""DIM_FACILITY 源绑定种子：规格契约（2026-11-25）。

⚠️ 交付函数的**行为**（建行 → 确认 → 首次 1 / 再次 0 → 解析器可解析）由真实 PG 用例
`app/tests/integration/test_ontology_binding_resolver_api.py::
test_seed_facility_binding_creates_confirms_and_is_idempotent` 覆盖；
本文件只钉**配置契约**（廉价、无 IO），补足行为测试看不到的「规格自身是否正确」。
"""

from __future__ import annotations

from scripts import seed_procurement_bindings as mod


def test_facility_binding_spec_has_required_fields() -> None:
    """绑定规格：THBI 数据源 + BASE 角色 + 审计确认人。"""
    spec = mod.FACILITY_BINDING
    assert spec["datasourceId"] == 1
    assert spec["schemaName"] == "THBI"
    assert spec["sourceTable"] == "DIM_FACILITY"
    assert spec["bindingRole"] == "BASE"
    assert spec["confirmedBy"]


def test_facility_binding_spec_keyed_by_class_name() -> None:
    """唯一键以类名为锚（同一类只建一条生效绑定）。"""
    assert mod.FACILITY_BINDING["className"] == "DIM_FACILITY"


def test_facility_binding_spec_has_no_dead_fields() -> None:
    """规格里**每个键都必须驱动行为**。

    回归来历（评审 Important #1）：曾同时声明 `confirmedBy` 与独立字面量 `ACTOR`，
    且 `isCandidate` 无人读取 —— 单元测试断言这两个字段，而实际行为走另一份字面量
    ⇒ 测试可以一直绿，审计 actor 与「已确认、非候选」的意图却可静默漂移。
    现在模块内**只有** `confirmedBy` 一份 actor 字面量，且由集成测试断言它真的
    落进了库里的 `confirmed_by`（`test_seed_facility_binding_creates_confirms_...`）。
    """
    assert set(mod.FACILITY_BINDING) == {
        "className",
        "datasourceId",
        "schemaName",
        "sourceTable",
        "bindingRole",
        "confirmedBy",
    }


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

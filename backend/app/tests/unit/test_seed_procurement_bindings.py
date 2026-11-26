"""DIM_FACILITY 源绑定种子：规格契约（2026-11-25）。

⚠️ 交付函数的**行为**（建行 → 确认 → 首次 1 / 再次 0 → 解析器可解析）由真实 PG 用例
`app/tests/integration/test_ontology_binding_resolver_api.py::
test_seed_facility_binding_creates_confirms_and_is_idempotent` 覆盖；
本文件只钉**配置契约**（廉价、无 IO），补足行为测试看不到的「规格自身是否正确」。
"""

from __future__ import annotations

from pathlib import Path

from scripts import seed_procurement_bindings as mod


def test_facility_binding_spec_has_required_fields() -> None:
    """绑定规格：THBI 数据源 + BASE 角色 + 已确认终态 + 审计确认人。"""
    spec = mod.FACILITY_BINDING
    assert spec["datasourceId"] == 1
    assert spec["schemaName"] == "THBI"
    assert spec["sourceTable"] == "DIM_FACILITY"
    assert spec["bindingRole"] == "BASE"
    assert spec["isCandidate"] is False
    assert spec["confirmedBy"]


def test_facility_binding_spec_keyed_by_class_name() -> None:
    """唯一键以类名为锚（同一类只建一条生效绑定）。"""
    assert mod.FACILITY_BINDING["className"] == "DIM_FACILITY"


def test_facility_binding_spec_keeps_full_contract() -> None:
    """规格键集是**下游契约**，不得擅自增删（brief Step 1 钉的 7 个键）。

    回归来历（两轮评审）：曾删掉 `isCandidate` 把键集缩到 6 个 —— 那是未获批的
    契约变更；正确做法是**保留键并让它驱动行为**（`_assertTerminalState` 拿实际
    落库状态与它比对，集成用例
    `test_seed_facility_binding_enforces_spec_terminal_state` 证明它真被消费）。
    本测试钉住键集，防止再次以「清死字段」为名缩小契约。
    """
    assert set(mod.FACILITY_BINDING) == {
        "className",
        "datasourceId",
        "schemaName",
        "sourceTable",
        "bindingRole",
        "isCandidate",
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


def test_binding_seed_is_wired_into_startup_chain() -> None:
    """C1：启动 seed 链必须调用**全量**绑定种子（不是只有 CLI 入口）。

    CLI 只在手动跑时生效；部署到缺绑定行的库时，启动链不跑 ⇒ 待到货
    基地过滤 / `/sites` 解析不到物理表。失败必须显式告警（不许静默）。

    **2026-11-26 收紧**：原先只断言 `seed_facility_binding`（只补 DIM_FACILITY）。
    生产实测绑定表总共只有 1 行 —— 两个 DWD 类从未录入 ⇒ 发版后待到货/到货跟踪
    直接 fail-closed。故启动链改用 `seed_procurement_bindings`（覆盖 `BINDING_SPECS`
    全部类），断言随之改名：守卫的对象从「基地字典」升级为「工具依赖的全部类」。
    """
    mainSource = (
        Path(__file__).resolve().parents[3] / "app" / "main.py"
    ).read_text(encoding="utf-8")

    assert "seed_procurement_bindings" in mainSource, (
        "启动链未调用全量绑定种子（漏 => 缺绑定的类在启动后仍 fail-closed）"
    )
    # 失败必须有显式日志（异常不得被裸吞）
    assert "logger.exception" in mainSource


def test_binding_specs_cover_every_class_the_tools_resolve() -> None:
    """**交叉不变式**：工具里 `resolveBinding` 用到的类，必须在 `BINDING_SPECS` 里。

    为什么（2026-11-26 生产实测）：工具硬编码了两个 DWD 类名，而种子只覆盖
    `DIM_FACILITY` ⇒ 「仓库里没有脚本能补这两个类」⇒ 生产绑定表只有 1 行，
    发版后待到货/到货跟踪直接不可用（解析器 fail-closed）。

    这条守卫把「工具要读哪些类」与「种子会建哪些类」钉在一起：新增一个
    `resolveBinding(className=...)` 却忘了加规格，这里立刻红。
    """
    from app.services import delivery_tracking_query as tracking
    from app.services import pending_delivery_query as pending
    from scripts import seed_procurement_bindings as mod

    specClasses = {str(spec["className"]) for spec in mod.BINDING_SPECS}
    toolClasses = {
        tracking.ORDER_CLASS_NAME,
        tracking.RECEIPT_CLASS_NAME,
        pending.ORDER_CLASS_NAME,
        pending.RECEIPT_CLASS_NAME,
        pending.FACILITY_CLASS_NAME,
    }
    missing = toolClasses - specClasses
    assert not missing, (
        f"以下类被工具 resolveBinding 使用但没有任何绑定规格（生产会 fail-closed）：{missing}"
    )

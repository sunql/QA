"""设计稿 §5.5 的 6 个校验码（G2）：每个码**正/反例各一**。

钉住四件事：

1. **码目录完整**：CLS-001 / CLS-002 / REL-001 / AGT-001 / GRF-001 / MET-002
   都在 `evaluateManifestChecks` 的产出里（缺一个 ⇒ 设计稿覆盖度又掉回去）；
2. **正例 PASS 行也要说清「查了什么」**：PASS 不是空行（与 M1-1.2 的口径一致）；
3. **活库事实的 `None` 语义**：没查过 ⇒ 不出行（AGT-001）/ 只判快照可见部分（REL-001），
   绝不写一句没依据的「全部正常」；
4. **逐 KPI 的 MET-002 不被 `_dedupe` 合并**：同 code 不同 targetKey 必须各留一行。

样本刻意做「最小合法形状」：直接调 `evaluateDesignChecks`（纯函数），
不经过 Schema 闸 —— 各码的判据要能被**单独**触发。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.semantic_release_check_types import (
    RESULT_FAIL,
    RESULT_PASS,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    TARGET_KPI,
    TARGET_RELEASE,
)
from app.services.semantic_release_checks import evaluateManifestChecks
from app.services.semantic_release_design_checks import (
    CHECK_AGENT_INTEGRITY,
    CHECK_BUSINESS_OBJECT_INTEGRITY,
    CHECK_CLASS_INTEGRITY,
    CHECK_GRAPH_ALLOWLIST,
    CHECK_KPI_DATASOURCE,
    CHECK_RELATION_INTEGRITY,
    evaluateDesignChecks,
)
from app.services.semantic_release_domain_checks import ReleaseCheckContext
from app.tests._knowledge_manifest import sampleManifest


def _byCode(checks: tuple[Any, ...], code: str) -> list[Any]:
    return [check for check in checks if check.code == code]


def _cls(name: str, *, parent: str | None = None, objectType: str | None = "Master",
         isPrimaryKey: bool = False) -> dict[str, Any]:
    return {
        "className": name,
        "objectType": objectType,
        "parentClassName": parent,
        "properties": [
            {"propertyName": f"{name}_id", "isPrimaryKey": isPrimaryKey}
        ],
    }


def _bo(code: str, header: str, domain: str = "PROCUREMENT",
        graphLabel: str | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "headerClassName": header,
        "primaryDomainCode": domain,
        "attachedDomainCodes": [],
        "graphLabel": graphLabel,
    }


def _manifest(**sections: list[dict[str, Any]]) -> dict[str, Any]:
    return {"domains": [{"code": "PROCUREMENT", "enabled": True}], **sections}


def _run(manifest: dict[str, Any], **context: Any) -> tuple[Any, ...]:
    return evaluateDesignChecks(manifest, context=ReleaseCheckContext(**context))


# ---------------------------------------------------------------------------
# CLS-001 类定义
# ---------------------------------------------------------------------------


def test_cls001_passes_on_a_clean_class_graph() -> None:
    manifests = _manifest(
        classes=[_cls("Supplier"), _cls("PurchaseOrder", parent="Supplier")]
    )
    checks = _byCode(_run(manifests), CHECK_CLASS_INTEGRITY)

    assert len(checks) == 1
    assert checks[0].result == RESULT_PASS
    assert checks[0].severity == SEVERITY_ERROR
    assert "2 个类" in checks[0].detail


@pytest.mark.parametrize(
    ("classes", "needle"),
    [
        pytest.param(
            [_cls("Supplier"), _cls("Supplier")], "类名重复", id="duplicate-name"
        ),
        pytest.param(
            [_cls("Supplier", parent="Ghost")], "父类 Ghost 不存在", id="missing-parent"
        ),
        pytest.param(
            [_cls("Supplier", objectType="Magic")], "objectType", id="bad-object-type"
        ),
        pytest.param(
            [_cls("A", parent="B"), _cls("B", parent="A")], "继承环", id="two-cycle"
        ),
        pytest.param([_cls("A", parent="A")], "继承环", id="self-parent"),
    ],
)
def test_cls001_fails_on_broken_classes(classes: list[dict[str, Any]], needle: str) -> None:
    check = _byCode(_run(_manifest(classes=classes)), CHECK_CLASS_INTEGRITY)[0]

    assert check.result == RESULT_FAIL
    assert check.severity == SEVERITY_ERROR
    assert needle in check.detail


# ---------------------------------------------------------------------------
# CLS-002 业务对象
# ---------------------------------------------------------------------------


def test_cls002_passes_when_header_and_domain_exist() -> None:
    manifest = _manifest(classes=[_cls("Supplier")], businessObjects=[_bo("S", "Supplier")])
    check = _byCode(_run(manifest), CHECK_BUSINESS_OBJECT_INTEGRITY)[0]

    assert check.result == RESULT_PASS
    assert "1 个业务对象" in check.detail


@pytest.mark.parametrize(
    ("manifest", "needle"),
    [
        pytest.param(
            _manifest(classes=[_cls("Supplier")], businessObjects=[_bo("S", "Ghost")]),
            "头类 'Ghost' 不存在",
            id="missing-header",
        ),
        pytest.param(
            _manifest(classes=[_cls("Supplier")], businessObjects=[_bo("S", "Supplier", domain="NOPE")]),
            "主域 'NOPE' 不存在",
            id="missing-domain",
        ),
        pytest.param(
            {
                "domains": [{"code": "PROCUREMENT", "enabled": False}],
                "classes": [_cls("Supplier")],
                "businessObjects": [_bo("S", "Supplier")],
            },
            "已停用",
            id="disabled-domain",
        ),
    ],
)
def test_cls002_fails_on_broken_business_objects(
    manifest: dict[str, Any], needle: str
) -> None:
    check = _byCode(_run(manifest), CHECK_BUSINESS_OBJECT_INTEGRITY)[0]

    assert check.result == RESULT_FAIL
    assert needle in check.detail


# ---------------------------------------------------------------------------
# REL-001 关系
# ---------------------------------------------------------------------------


def _join(source: str, target: str) -> dict[str, Any]:
    return {"joinKey": f"{source}_{target}", "sourceClassName": source,
            "targetClassName": target}


def test_rel001_passes_and_states_whether_live_facts_were_checked() -> None:
    manifest = _manifest(classes=[_cls("A"), _cls("B")], joins=[_join("A", "B")])
    unchecked = _byCode(_run(manifest), CHECK_RELATION_INTEGRITY)[0]
    checked = _byCode(
        _run(manifest, relationViolations=()), CHECK_RELATION_INTEGRITY
    )[0]

    assert unchecked.result == RESULT_PASS
    assert "未查" in unchecked.detail
    assert checked.result == RESULT_PASS
    assert "无非法类型" in checked.detail


@pytest.mark.parametrize(
    ("run_kwargs", "needle"),
    [
        pytest.param({}, "targetClassName='Ghost'", id="manifest-endpoint-missing"),
        pytest.param(
            {"relationViolations": ("语义关系 A-[BAD]->B 的类型不在代码白名单",)},
            "不在代码白名单",
            id="live-bad-relation-type",
        ),
        pytest.param(
            {"relationViolations": ("语义关系 A-[SUPPLIES]->B 的端点类已失效（墓碑）",)},
            "墓碑",
            id="live-tombstoned-endpoint",
        ),
    ],
)
def test_rel001_fails_on_broken_relations(
    run_kwargs: dict[str, Any], needle: str
) -> None:
    manifest = _manifest(classes=[_cls("A"), _cls("B")], joins=[_join("A", "Ghost")])
    check = _byCode(_run(manifest, **run_kwargs), CHECK_RELATION_INTEGRITY)[0]

    assert check.result == RESULT_FAIL
    assert needle in check.detail


# ---------------------------------------------------------------------------
# AGT-001 Agent
# ---------------------------------------------------------------------------


def test_agt001_is_absent_when_live_facts_were_not_loaded() -> None:
    """没查过活库 ⇒ 不出行（「Agent 全部正常」是没依据的结论）。"""
    assert _byCode(_run(_manifest()), CHECK_AGENT_INTEGRITY) == []


def test_agt001_passes_on_an_empty_violation_list() -> None:
    check = _byCode(_run(_manifest(), agentViolations=()), CHECK_AGENT_INTEGRITY)[0]

    assert check.result == RESULT_PASS
    assert check.severity == SEVERITY_WARNING
    assert check.targetType == TARGET_RELEASE


def test_agt001_fails_with_the_offending_agents() -> None:
    violations = ("Agent A1 绑定的工具 x 未注册", "Agent A2 未绑定工具（不可运行）")
    check = _byCode(
        _run(_manifest(), agentViolations=violations), CHECK_AGENT_INTEGRITY
    )[0]

    assert check.result == RESULT_FAIL
    assert "Agent A1" in check.detail and "Agent A2" in check.detail


# ---------------------------------------------------------------------------
# GRF-001 图谱白名单
# ---------------------------------------------------------------------------


def test_grf001_passes_for_whitelisted_label_with_stable_key() -> None:
    manifest = _manifest(
        classes=[_cls("Supplier", isPrimaryKey=True)],
        businessObjects=[_bo("SUPPLIER", "Supplier", graphLabel="Supplier")],
    )
    check = _byCode(_run(manifest), CHECK_GRAPH_ALLOWLIST)[0]

    assert check.result == RESULT_PASS
    assert "1 个入图业务对象" in check.detail


def test_grf001_fails_for_label_outside_the_neo4j_allowlist() -> None:
    manifest = _manifest(
        classes=[_cls("DWD_X", isPrimaryKey=True)],
        businessObjects=[_bo("X", "DWD_X", graphLabel="DWD_X")],
    )
    check = _byCode(_run(manifest), CHECK_GRAPH_ALLOWLIST)[0]

    assert check.result == RESULT_FAIL
    assert check.severity == SEVERITY_WARNING
    assert "不在 Neo4j 业务实体白名单" in check.detail


def test_grf001_fails_when_the_header_class_has_no_primary_key() -> None:
    manifest = _manifest(
        classes=[_cls("Supplier", isPrimaryKey=False)],
        businessObjects=[_bo("SUPPLIER", "Supplier", graphLabel="Supplier")],
    )
    check = _byCode(_run(manifest), CHECK_GRAPH_ALLOWLIST)[0]

    assert check.result == RESULT_FAIL
    assert "无法稳定定位" in check.detail


def test_grf001_does_not_judge_keys_on_a_class_without_properties() -> None:
    """类根本没声明属性 ⇒ 主键不可判（属属性治理），不报图定位问题。"""
    manifest = _manifest(
        classes=[{"className": "Supplier", "objectType": "Master",
                  "parentClassName": None, "properties": []}],
        businessObjects=[_bo("SUPPLIER", "Supplier", graphLabel="Supplier")],
    )
    check = _byCode(_run(manifest), CHECK_GRAPH_ALLOWLIST)[0]

    assert check.result == RESULT_PASS


def test_grf001_ignores_objects_that_do_not_enter_the_graph() -> None:
    """`graphLabel` 为空 = 不打算入图 ⇒ 不该被图规则（白名单/主键）约束。"""
    manifest = _manifest(
        classes=[_cls("Supplier")],
        businessObjects=[_bo("SUPPLIER", "Supplier", graphLabel=None)],
    )
    check = _byCode(_run(manifest), CHECK_GRAPH_ALLOWLIST)[0]

    assert check.result == RESULT_PASS


# ---------------------------------------------------------------------------
# MET-002 KPI 数据源
# ---------------------------------------------------------------------------


def _kpi(
    code: str,
    *,
    status: str = "PUBLISHED",
    datasourceName: str | None = None,
    metricName: str | None = None,
) -> dict[str, Any]:
    return {
        "kpiCode": code,
        "status": status,
        "datasourceName": datasourceName,
        "metricName": metricName,
    }


def _metric(name: str, *, status: str = "PUBLISHED") -> dict[str, Any]:
    return {"metricName": name, "status": status}


def test_met002_passes_for_a_bound_published_kpi() -> None:
    manifest = _manifest(
        metrics=[_metric("M1")],
        kpis=[_kpi("K1", datasourceName="THBI Oracle", metricName="M1")],
    )
    check = _byCode(
        _run(manifest, activeDatasourceNames=frozenset({"THBI Oracle"})),
        CHECK_KPI_DATASOURCE,
    )[0]

    assert check.result == RESULT_PASS
    assert check.targetType == TARGET_KPI
    assert check.targetKey == "K1"


def test_met002_ignores_draft_kpis() -> None:
    """草稿 KPI 允许先于技术实现存在（设计 §5.6 弱关联）⇒ 不报治理债。"""
    manifest = _manifest(kpis=[_kpi("K1", status="DRAFT")])
    checks = _byCode(_run(manifest), CHECK_KPI_DATASOURCE)

    assert len(checks) == 1
    assert checks[0].result == RESULT_PASS
    assert "不适用" in checks[0].detail


@pytest.mark.parametrize(
    ("manifest", "context", "needle"),
    [
        pytest.param(
            _manifest(kpis=[_kpi("K1")]), {}, "未绑定数据源", id="unbound"
        ),
        pytest.param(
            _manifest(kpis=[_kpi("K1", datasourceName="Gone")]),
            {"activeDatasourceNames": frozenset({"THBI Oracle"})},
            "不存在或已停用",
            id="inactive-or-unknown-datasource",
        ),
        pytest.param(
            _manifest(
                metrics=[_metric("M1", status="DRAFT")],
                kpis=[_kpi("K1", datasourceName="THBI Oracle", metricName="M1")],
            ),
            {"activeDatasourceNames": frozenset({"THBI Oracle"})},
            "仍为 DRAFT",
            id="linked-metric-not-published",
        ),
    ],
)
def test_met002_fails_on_unusable_kpis(
    manifest: dict[str, Any], context: dict[str, Any], needle: str
) -> None:
    check = _byCode(_run(manifest, **context), CHECK_KPI_DATASOURCE)[0]

    assert check.result == RESULT_FAIL
    assert check.severity == SEVERITY_WARNING
    assert needle in check.detail


def test_met002_emits_one_row_per_published_kpi() -> None:
    """逐 KPI 落表 ⇒ `_dedupe` 不得把同 code 的不同 targetKey 合并掉。"""
    manifest = _manifest(
        kpis=[
            _kpi("K1", datasourceName="THBI Oracle"),
            _kpi("K2", datasourceName="THBI Oracle"),
            _kpi("K3", status="DRAFT"),
        ]
    )
    checks = evaluateManifestChecks(
        manifest,
        context=ReleaseCheckContext(
            activeDatasourceNames=frozenset({"THBI Oracle"})
        ),
    )
    kpiChecks = _byCode(checks, CHECK_KPI_DATASOURCE)

    assert [check.targetKey for check in kpiChecks] == ["K1", "K2"]
    assert all(check.result == RESULT_PASS for check in kpiChecks)


# ---------------------------------------------------------------------------
# 与总目录的接线
# ---------------------------------------------------------------------------


def test_all_six_design_codes_are_wired_into_the_release_report() -> None:
    checks = evaluateManifestChecks(
        sampleManifest(),
        context=ReleaseCheckContext(
            activeDatasourceNames=frozenset(),
            relationViolations=(),
            agentViolations=(),
        ),
    )
    codes = {check.code for check in checks}

    assert {
        CHECK_CLASS_INTEGRITY,
        CHECK_BUSINESS_OBJECT_INTEGRITY,
        CHECK_RELATION_INTEGRITY,
        CHECK_AGENT_INTEGRITY,
        CHECK_GRAPH_ALLOWLIST,
        CHECK_KPI_DATASOURCE,
    } <= codes

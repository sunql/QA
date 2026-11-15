"""语义发布图谱镜像的纯逻辑单测（P3b D22）。

只测纯函数（不连 Neo4j / PG）：
- `buildMirrorPlan`：manifest + 名字解析结果 ⇒ 镜像节点 / 镜像边 / 未解析清单；
- `buildEntityResolution`：PG 行 ⇒ name → unified_id 解析表（含同名多行歧义）。

设计依据：`Harness/changes/feat-semantic-release-graph-mirror/daily/D22.md`。
"""

from __future__ import annotations

from app.services.semantic_graph_mirror import (
    EntityResolution,
    buildEntityResolution,
    buildMirrorPlan,
)


def _manifest() -> dict:
    """最小可用 manifest（只含镜像需要的字段）。"""
    return {
        "classes": [
            {
                "className": "采购订单",
                "classAlias": "PO",
                "description": "采购订单",
                "sourceTable": "po",
                "parentClassName": "单据",
                "properties": [
                    {
                        "propertyName": "订单号",
                        "propertyAlias": "单号",
                        "dataType": "STRING",
                        "sourceColumn": "po_no",
                        "isPrimaryKey": True,
                        "isForeignKey": False,
                    }
                ],
            },
            {
                "className": "单据",
                "classAlias": None,
                "description": None,
                "sourceTable": None,
                "parentClassName": None,
                "properties": [],
            },
            {
                "className": "供应商",
                "classAlias": None,
                "description": None,
                "sourceTable": None,
                "parentClassName": None,
                "properties": [],
            },
        ],
        "joins": [
            {
                "joinKey": "po_supplier",
                "sourceClassName": "采购订单",
                "targetClassName": "供应商",
            }
        ],
        "metrics": [
            {
                "metricName": "采购总额",
                "metricAlias": None,
                "formula": "SUM(amount)",
                "aggFunction": "SUM",
                "targetClassName": "采购订单",
            }
        ],
        "kpis": [
            {
                "kpiCode": "KPI-1",
                "kpiName": "采购总额 KPI",
                "status": "PUBLISHED",
                "owner": "张三",
                "unit": "元",
                "metricName": "采购总额",
            }
        ],
    }


def _resolution() -> EntityResolution:
    return EntityResolution(
        classIds={
            "采购订单": "obj:CLASS:1",
            "单据": "obj:CLASS:2",
            "供应商": "obj:CLASS:3",
        },
        propertyIds={("采购订单", "订单号"): "obj:PROPERTY:10"},
        metricIds={"采购总额": "obj:METRIC:20"},
        kpiIds={"KPI-1": "obj:KPI:30"},
    )


def _plan(manifest: dict | None = None, resolution: EntityResolution | None = None):
    return buildMirrorPlan(
        manifest if manifest is not None else _manifest(),
        resolution if resolution is not None else _resolution(),
        releaseId=7,
        releaseVersion="v1.2.0",
    )


def _node(plan, entityType: str, unifiedId: str):
    for node in plan.nodes:
        if node.entityType == entityType and node.unifiedId == unifiedId:
            return node
    raise AssertionError(f"缺少节点 {entityType}:{unifiedId}")


def _edge(plan, relType: str, sourceUid: str, targetUid: str):
    for edge in plan.edges:
        if (
            edge.relType == relType
            and edge.sourceUid == sourceUid
            and edge.targetUid == targetUid
        ):
            return edge
    raise AssertionError(f"缺少边 {relType}:{sourceUid}->{targetUid}")


# =============================================================================
# 节点
# =============================================================================


def test_类节点字段取自manifest() -> None:
    plan = _plan()
    node = _node(plan, "class", "obj:CLASS:1")
    # 字段顺序 = 契约顺序（与主图节点字段同序），不是字母序
    assert node.fields == (
        ("name", "采购订单"),
        ("alias", "PO"),
        ("description", "采购订单"),
        ("sourceTable", "po"),
    )


def test_属性节点字段取自manifest() -> None:
    plan = _plan()
    node = _node(plan, "property", "obj:PROPERTY:10")
    assert node.fields == (
        ("name", "订单号"),
        ("alias", "单号"),
        ("dataType", "STRING"),
        ("sourceColumn", "po_no"),
        ("isPrimaryKey", True),
        ("isForeignKey", False),
    )


def test_指标与KPI节点字段取自manifest() -> None:
    plan = _plan()
    metric = _node(plan, "metric", "obj:METRIC:20")
    assert metric.fields == (
        ("name", "采购总额"),
        ("alias", None),
        ("formula", "SUM(amount)"),
        ("aggFunction", "SUM"),
    )
    kpi = _node(plan, "kpi", "obj:KPI:30")
    assert kpi.fields == (
        ("code", "KPI-1"),
        ("name", "采购总额 KPI"),
        ("status", "PUBLISHED"),
        ("owner", "张三"),
        ("unit", "元"),
    )


def test_镜像节点携带发布标识() -> None:
    plan = _plan()
    row = _node(plan, "class", "obj:CLASS:1").toRow(
        releaseId=7, releaseVersion="v1.2.0"
    )
    assert row["unified_id"] == "obj:CLASS:1"
    assert row["release_id"] == 7
    assert row["release_version"] == "v1.2.0"
    # mirror_of 指向主图同一实体（unified_id 相同），便于镜像 → 主图回查
    assert row["mirror_of"] == "obj:CLASS:1"


def test_镜像节点只用发布标签不碰主图() -> None:
    from app.domain.semantic_graph_mirror_spec import RELEASE_MIRROR_LABELS

    plan = _plan()
    assert set(RELEASE_MIRROR_LABELS.values()) == {
        "ReleaseClass",
        "ReleaseProperty",
        "ReleaseMetric",
        "ReleaseKpiCatalog",
    }
    for node in plan.nodes:
        label = RELEASE_MIRROR_LABELS[node.entityType]
        assert label.startswith("Release")
        assert label not in {"Class", "Property", "Metric", "KpiCatalog"}


# =============================================================================
# 边
# =============================================================================


def test_类到属性生成HAS_PROPERTY边() -> None:
    plan = _plan()
    edge = _edge(plan, "HAS_PROPERTY", "obj:CLASS:1", "obj:PROPERTY:10")
    assert edge.sourceType == "class"
    assert edge.targetType == "property"


def test_父子类生成SUBCLASS_OF边() -> None:
    plan = _plan()
    edge = _edge(plan, "SUBCLASS_OF", "obj:CLASS:1", "obj:CLASS:2")
    assert (edge.sourceType, edge.targetType) == ("class", "class")


def test_JOIN生成边() -> None:
    plan = _plan()
    edge = _edge(plan, "JOIN", "obj:CLASS:1", "obj:CLASS:3")
    assert (edge.sourceType, edge.targetType) == ("class", "class")


def test_指标生成DERIVED_FROM边() -> None:
    plan = _plan()
    edge = _edge(plan, "DERIVED_FROM", "obj:METRIC:20", "obj:CLASS:1")
    assert (edge.sourceType, edge.targetType) == ("metric", "class")


def test_KPI生成MEASURED_BY边() -> None:
    plan = _plan()
    edge = _edge(plan, "MEASURED_BY", "obj:KPI:30", "obj:METRIC:20")
    assert (edge.sourceType, edge.targetType) == ("kpi", "metric")


def test_边类型都在白名单内() -> None:
    from app.domain.semantic_graph_mirror_spec import RELEASE_MIRROR_REL_TYPES

    plan = _plan()
    for edge in plan.edges:
        assert edge.relType in RELEASE_MIRROR_REL_TYPES


# =============================================================================
# 未解析
# =============================================================================


def test_类未解析时不产出节点且记入未解析清单() -> None:
    resolution = EntityResolution(
        classIds={"单据": "obj:CLASS:2", "供应商": "obj:CLASS:3"},
        propertyIds={},
        metricIds={"采购总额": "obj:METRIC:20"},
        kpiIds={"KPI-1": "obj:KPI:30"},
    )
    plan = _plan(resolution=resolution)
    assert all(node.unifiedId != "obj:CLASS:1" for node in plan.nodes)
    assert any(
        item.entityType == "class" and item.name == "采购订单" for item in plan.unresolved
    )


def test_类未解析时其属性级联记为未解析() -> None:
    resolution = EntityResolution(
        classIds={"单据": "obj:CLASS:2", "供应商": "obj:CLASS:3"},
        propertyIds={("采购订单", "订单号"): "obj:PROPERTY:10"},
        metricIds={"采购总额": "obj:METRIC:20"},
        kpiIds={"KPI-1": "obj:KPI:30"},
    )
    plan = _plan(resolution=resolution)
    assert all(node.unifiedId != "obj:PROPERTY:10" for node in plan.nodes)
    assert any(
        item.entityType == "property" and item.name == "采购订单.订单号"
        for item in plan.unresolved
    )


def test_属性未解析时不产出属性节点与边() -> None:
    resolution = EntityResolution(
        classIds={
            "采购订单": "obj:CLASS:1",
            "单据": "obj:CLASS:2",
            "供应商": "obj:CLASS:3",
        },
        propertyIds={},
        metricIds={"采购总额": "obj:METRIC:20"},
        kpiIds={"KPI-1": "obj:KPI:30"},
    )
    plan = _plan(resolution=resolution)
    assert all(node.entityType != "property" for node in plan.nodes)
    assert all(edge.relType != "HAS_PROPERTY" for edge in plan.edges)
    assert any(
        item.entityType == "property" and item.name == "采购订单.订单号"
        for item in plan.unresolved
    )


def test_边端点未解析时记入未解析清单() -> None:
    resolution = EntityResolution(
        classIds={"采购订单": "obj:CLASS:1", "单据": "obj:CLASS:2"},
        propertyIds={("采购订单", "订单号"): "obj:PROPERTY:10"},
        metricIds={"采购总额": "obj:METRIC:20"},
        kpiIds={"KPI-1": "obj:KPI:30"},
    )
    plan = _plan(resolution=resolution)
    assert all(edge.relType != "JOIN" for edge in plan.edges)
    assert any(
        item.kind == "edge" and item.relType == "JOIN" for item in plan.unresolved
    )


def test_同名多行的实体算歧义未解析() -> None:
    resolution = EntityResolution(
        classIds={"采购订单": None, "单据": "obj:CLASS:2", "供应商": "obj:CLASS:3"},
        propertyIds={},
        metricIds={"采购总额": "obj:METRIC:20"},
        kpiIds={"KPI-1": "obj:KPI:30"},
    )
    plan = _plan(resolution=resolution)
    assert any(
        item.entityType == "class" and "歧义" in item.reason for item in plan.unresolved
    )


def test_指标未解析时不产出节点且记入未解析清单() -> None:
    resolution = EntityResolution(
        classIds={"采购订单": "obj:CLASS:1", "单据": "obj:CLASS:2", "供应商": "obj:CLASS:3"},
        propertyIds={("采购订单", "订单号"): "obj:PROPERTY:10"},
        metricIds={},
        kpiIds={"KPI-1": "obj:KPI:30"},
    )
    plan = _plan(resolution=resolution)
    assert all(node.entityType != "metric" for node in plan.nodes)
    assert any(
        item.entityType == "metric" and item.name == "采购总额"
        for item in plan.unresolved
    )


def test_指标的目标类未解析时记入未解析边() -> None:
    """指标本身能解析，但目标类解析不到 ⇒ 指标进镜像、DERIVED_FROM 边不写。"""
    resolution = EntityResolution(
        classIds={"单据": "obj:CLASS:2", "供应商": "obj:CLASS:3"},
        propertyIds={},
        metricIds={"采购总额": "obj:METRIC:20"},
        kpiIds={"KPI-1": "obj:KPI:30"},
    )
    plan = _plan(resolution=resolution)
    assert any(node.entityType == "metric" for node in plan.nodes)
    assert all(edge.relType != "DERIVED_FROM" for edge in plan.edges)
    assert any(
        item.kind == "edge" and item.relType == "DERIVED_FROM"
        for item in plan.unresolved
    )


def test_KPI未解析时不产出节点且记入未解析清单() -> None:
    resolution = EntityResolution(
        classIds={"采购订单": "obj:CLASS:1", "单据": "obj:CLASS:2", "供应商": "obj:CLASS:3"},
        propertyIds={("采购订单", "订单号"): "obj:PROPERTY:10"},
        metricIds={"采购总额": "obj:METRIC:20"},
        kpiIds={},
    )
    plan = _plan(resolution=resolution)
    assert all(node.entityType != "kpi" for node in plan.nodes)
    assert any(item.entityType == "kpi" and item.name == "KPI-1" for item in plan.unresolved)


def test_KPI引用的指标未解析时记入未解析边() -> None:
    resolution = EntityResolution(
        classIds={"采购订单": "obj:CLASS:1", "单据": "obj:CLASS:2", "供应商": "obj:CLASS:3"},
        propertyIds={("采购订单", "订单号"): "obj:PROPERTY:10"},
        metricIds={},
        kpiIds={"KPI-1": "obj:KPI:30"},
    )
    plan = _plan(resolution=resolution)
    assert any(node.entityType == "kpi" for node in plan.nodes)
    assert all(edge.relType != "MEASURED_BY" for edge in plan.edges)
    assert any(
        item.kind == "edge" and item.relType == "MEASURED_BY"
        for item in plan.unresolved
    )


def test_同一实体在manifest里出现两次只产出一个节点() -> None:
    """重复实体去重：节点身份是 (类型, unified_id)，重复写入不产生第二个节点。"""
    manifest = _manifest()
    manifest["classes"].append(dict(manifest["classes"][0]))
    plan = _plan(manifest=manifest)
    classUids = [node.unifiedId for node in plan.nodes if node.entityType == "class"]
    assert classUids.count("obj:CLASS:1") == 1


# =============================================================================
# 确定性
# =============================================================================


def test_计划顺序稳定与输入顺序无关() -> None:
    manifest = _manifest()
    resolution = _resolution()
    first = _plan(manifest, resolution)
    shuffled = _manifest()
    shuffled["classes"] = list(reversed(shuffled["classes"]))
    second = _plan(shuffled, resolution)
    assert first.nodes == second.nodes
    assert first.edges == second.edges


def test_空manifest产出空计划() -> None:
    plan = buildMirrorPlan(
        {"classes": [], "joins": [], "metrics": [], "kpis": []},
        EntityResolution(),
        releaseId=1,
        releaseVersion="v1",
    )
    assert plan.nodes == ()
    assert plan.edges == ()
    assert plan.unresolved == ()


def test_节点与边按类型分组便于分批写入() -> None:
    plan = _plan()
    rowsByType = plan.nodeRowsByType()
    assert set(rowsByType) == {"class", "property", "metric", "kpi"}
    assert all(row["release_id"] == 7 for rows in rowsByType.values() for row in rows)
    edgeRows = plan.edgeRows()
    assert len(edgeRows) == len(plan.edges)
    assert edgeRows[0]["relType"] in {
        "HAS_PROPERTY",
        "SUBCLASS_OF",
        "JOIN",
        "DERIVED_FROM",
        "MEASURED_BY",
    }


# =============================================================================
# 名字 → unified_id 解析表构建（纯函数）
# =============================================================================


def test_解析表按名字索引() -> None:
    resolution = buildEntityResolution(
        classRows=[("采购订单", "obj:CLASS:1")],
        propertyRows=[("采购订单", "订单号", "obj:PROPERTY:10")],
        metricRows=[("采购总额", "obj:METRIC:20")],
        kpiRows=[("KPI-1", "obj:KPI:30")],
    )
    assert resolution.classIds["采购订单"] == "obj:CLASS:1"
    assert resolution.propertyIds[("采购订单", "订单号")] == "obj:PROPERTY:10"
    assert resolution.metricIds["采购总额"] == "obj:METRIC:20"
    assert resolution.kpiIds["KPI-1"] == "obj:KPI:30"


def test_同名多行标记为歧义() -> None:
    resolution = buildEntityResolution(
        classRows=[("采购订单", "obj:CLASS:1"), ("采购订单", "obj:CLASS:9")],
        propertyRows=[],
        metricRows=[],
        kpiRows=[],
    )
    assert resolution.classIds["采购订单"] is None


def test_同名同ID的重复行不算歧义() -> None:
    resolution = buildEntityResolution(
        classRows=[("采购订单", "obj:CLASS:1"), ("采购订单", "obj:CLASS:1")],
        propertyRows=[],
        metricRows=[],
        kpiRows=[],
    )
    assert resolution.classIds["采购订单"] == "obj:CLASS:1"


# =============================================================================
# 未解析原因要能区分「本体里没有」与「没登记统一 ID」
# =============================================================================


def test_未登记统一ID的原因与本体不存在不同() -> None:
    # 实测踩到：header 类在 ontology_class 里有，但 id_mapping 没登记 ⇒
    # 报告若一律说「本体中不存在该实体」，运维会去查本体表（查得到）⇒ 白忙。
    resolution = EntityResolution(
        classIds={"单据": "obj:CLASS:2", "供应商": "obj:CLASS:3"},
        propertyIds={},
        metricIds={"采购总额": "obj:METRIC:20"},
        kpiIds={"KPI-1": "obj:KPI:30"},
        unmapped={"class": frozenset({"采购订单"})},
    )
    plan = _plan(resolution=resolution)
    item = next(
        item for item in plan.unresolved if item.entityType == "class" and item.name == "采购订单"
    )
    assert "id_mapping" in item.reason


def test_本体中不存在的实体原因仍然是不存在() -> None:
    resolution = EntityResolution(
        classIds={"单据": "obj:CLASS:2", "供应商": "obj:CLASS:3"},
        propertyIds={},
        metricIds={"采购总额": "obj:METRIC:20"},
        kpiIds={"KPI-1": "obj:KPI:30"},
    )
    plan = _plan(resolution=resolution)
    item = next(
        item for item in plan.unresolved if item.entityType == "class" and item.name == "采购订单"
    )
    assert "不存在" in item.reason
    assert "id_mapping" not in item.reason


def test_解析表把空unified_id记为未登记() -> None:
    # 解析行里 unified_id 为空 ⇒ 实体存在但没登记 id_mapping
    resolution = buildEntityResolution(
        classRows=[("采购订单", None)],
        propertyRows=[],
        metricRows=[],
        kpiRows=[],
    )
    assert "采购订单" not in resolution.classIds
    assert "采购订单" in resolution.unmapped["class"]


def test_同名多行仍然是歧义而不是未登记() -> None:
    resolution = buildEntityResolution(
        classRows=[("采购订单", "obj:CLASS:1"), ("采购订单", "obj:CLASS:9")],
        propertyRows=[],
        metricRows=[],
        kpiRows=[],
    )
    assert resolution.classIds["采购订单"] is None
    assert "采购订单" not in resolution.unmapped.get("class", frozenset())


# =============================================================================
# REFERENCES 边（属性 → 被引用类）
#
# 为什么单列：manifest 的 `properties[].refClassName` 自带引用关系，主图也
# 物化了 `(:Property)-[:REFERENCES]->(:Class)`。镜像**漏了**它 ⇒ 镜像 ≠ 主图
# 本体子图 ⇒ D25 切读侧会静默少一截引用/外键导航路径。
# =============================================================================


def _manifestWithReference(refClassName: str | None) -> dict:
    manifest = _manifest()
    manifest["classes"][0]["properties"][0]["refClassName"] = refClassName
    return manifest


def test_属性引用类解析成功后产出REFERENCES边() -> None:
    plan = _plan(manifest=_manifestWithReference("供应商"))
    edge = _edge(plan, "REFERENCES", "obj:PROPERTY:10", "obj:CLASS:3")
    assert edge.sourceType == "property"
    assert edge.targetType == "class"


def test_属性引用类未解析时记入未解析边() -> None:
    resolution = EntityResolution(
        classIds={"采购订单": "obj:CLASS:1", "单据": "obj:CLASS:2"},
        propertyIds={("采购订单", "订单号"): "obj:PROPERTY:10"},
        metricIds={"采购总额": "obj:METRIC:20"},
        kpiIds={"KPI-1": "obj:KPI:30"},
    )
    plan = _plan(manifest=_manifestWithReference("供应商"), resolution=resolution)
    assert all(edge.relType != "REFERENCES" for edge in plan.edges)
    assert any(
        item.kind == "edge" and item.relType == "REFERENCES" for item in plan.unresolved
    )


def test_引用类为空时不产出REFERENCES边() -> None:
    """悬空引用在编译器里就编成 null ⇒ 镜像里既没有边，也没有未解析噪音。"""
    plan = _plan(manifest=_manifestWithReference(None))
    assert all(edge.relType != "REFERENCES" for edge in plan.edges)
    assert plan.unresolved == ()


def test_未解析记录把关系类型与实体类型分开记() -> None:
    """`entityType` 是实体类型、`relType` 是关系类型（别再混用一个字段）。"""
    resolution = EntityResolution(
        classIds={"采购订单": "obj:CLASS:1", "单据": "obj:CLASS:2"},
        propertyIds={("采购订单", "订单号"): "obj:PROPERTY:10"},
        metricIds={"采购总额": "obj:METRIC:20"},
        kpiIds={"KPI-1": "obj:KPI:30"},
    )
    plan = _plan(resolution=resolution)
    edgeItem = next(item for item in plan.unresolved if item.kind == "edge")
    assert edgeItem.relType == "JOIN"
    assert edgeItem.entityType == "class"

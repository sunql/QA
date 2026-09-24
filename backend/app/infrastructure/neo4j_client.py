"""Neo4j 图数据库客户端。

负责：连接管理、节点 CRUD（CQL）、关系维护。

连接参数从 app.config.getSettings().neo4j* 读取。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

from neo4j import GraphDatabase, Driver

from app.config import getSettings
from app.domain.enums import ClassRelationType

logger = logging.getLogger(__name__)

# 仅允许删除这些已知 label（deleteNode 通过字符串拼接 label，必须白名单防注入）
_ALLOWED_LABELS = frozenset({"Class", "Property", "Metric"})

# =============================================================================
# 业务实体子图（Phase 6.2 feat-semantic-relations）
# =============================================================================

# 业务实体节点 label：节点统一携带 BusinessEntity 主 label + 具体类型副 label，
# 与本体图（Class/Property/Metric）物理隔离 —— 本体同步/删除路径永不触碰业务节点。
# CQL label 不可参数化，必须白名单防注入（同 _ALLOWED_LABELS 模式）。
BUSINESS_ENTITY_LABELS = frozenset(
    {
        "Supplier",
        "ItemMaster",        # 原 Material（Phase 4.4 统一为 class_name）
        "PurchaseOrder",
        "Receipt",            # 原 GoodsReceipt
        "IncomingInspection",
        "Contract",           # 由文档目录提供
        # 删除：Material / GoodsReceipt / NCR（Phase 4.4 NCR 不入图）
    }
)

# 业务关系类型（Sheet 16 采购业务流转语义）：
# Supplier-SUPPLIES->Material / PurchaseOrder-CONTAINS->Material /
# PurchaseOrder-GENERATES->GoodsReceipt / GoodsReceipt-INSPECTED_BY->IncomingInspection /
# IncomingInspection-GENERATED->NCR / Supplier-SIGNED->Contract
# CQL 关系类型同样不可参数化，白名单防注入。
BUSINESS_RELATION_TYPES = frozenset(
    {
        "SUPPLIES",
        "CONTAINS",
        "GENERATES",
        "INSPECTED_BY",
        "SIGNED",
    }
)

# 多跳遍历上限（Phase 6.3）：防止无界 variable-length path 拖垮图库。
# 业务链路语义上限为 5 跳（Supplier->Material<-PO->GR->IQC->NCR）。
_MAX_TRAVERSAL_HOPS = 5


# 本体「类 × 类」语义关系类型（Phase 5.6 关系重构）：直接由 ClassRelationType 枚举派生，
# 枚举是词表单点事实（service 侧 _CLASS_RELATION_VALUES 同源），不会手写漂移。
# 由用户在本体页「语义关系」Tab 显式声明，Neo4j (:Class)-[:{TYPE}]->(:Class) 为镜像。
# 与 JOIN 边区分：JOIN 是 ontology_join（按列配对 NL2SQL）的入图边，走 linkClassJoin，
# 不在此白名单内。CQL 关系类型不可参数化，白名单防注入。
CLASS_RELATION_TYPES = frozenset(rel.value for rel in ClassRelationType)


def _assertBusinessLabel(label: str) -> None:
    """业务节点 label 白名单校验（CQL 拼接前置防御）。"""
    if label not in BUSINESS_ENTITY_LABELS:
        raise ValueError(f"Invalid business entity label: {label!r}")


def _assertBusinessRelation(relType: str) -> None:
    """业务关系类型白名单校验（CQL 拼接前置防御）。"""
    if relType not in BUSINESS_RELATION_TYPES:
        raise ValueError(f"Invalid business relation type: {relType!r}")


def _assertClassRelation(relType: str) -> None:
    """类级语义关系类型白名单校验（CQL 拼接前置防御）。"""
    if relType not in CLASS_RELATION_TYPES:
        raise ValueError(f"Invalid class relation type: {relType!r}")


def _sanitizeUri(uri: str) -> str:
    """去除 URI 中可能内嵌的凭据，避免密码写入日志。"""
    parsed = urlparse(uri)
    if parsed.username is None and parsed.password is None:
        return uri
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _getDriver() -> Driver:
    settings = getSettings()
    return GraphDatabase.driver(
        settings.neo4jUri,
        auth=(settings.neo4jUser, settings.neo4jPassword),
        # 过滤 INFORMATION 级通知（如 MATCH 双模式触发的 cartesian product 提示），
        # 保留 WARNING/ERROR，避免 seed 同步时刷屏
        notifications_min_severity="WARNING",
    )


_DRIVER: Driver | None = None


def getDriver() -> Driver:
    global _DRIVER  # noqa: PLW0603
    if _DRIVER is None:
        _DRIVER = _getDriver()
        logger.info("Neo4j driver created: %s", _sanitizeUri(getSettings().neo4jUri))
    return _DRIVER


def closeDriver() -> None:
    global _DRIVER  # noqa: PLW0603
    if _DRIVER is not None:
        _DRIVER.close()
        _DRIVER = None
        logger.info("Neo4j driver closed")


# =============================================================================
# Node operations
# =============================================================================


def getClassIds() -> set[int]:
    """读全部 Class 节点 id（对账用：PG 真源 diff 出缺失集）。"""
    driver = getDriver()
    with driver.session() as session:
        return {
            int(record["id"])
            for record in session.run("MATCH (c:Class) RETURN c.id AS id")
        }


def getPropertyIds() -> set[int]:
    """读全部 Property 节点 id（对账用）。"""
    driver = getDriver()
    with driver.session() as session:
        return {
            int(record["id"])
            for record in session.run("MATCH (p:Property) RETURN p.id AS id")
        }


def getJoinPairs() -> set[tuple[int, int]]:
    """读全部 JOIN 边 (sourceId, targetId)（对账用；MERGE 折叠后天然去重）。"""
    driver = getDriver()
    with driver.session() as session:
        return {
            (int(record["sourceId"]), int(record["targetId"]))
            for record in session.run(
                "MATCH (a:Class)-[:JOIN]->(b:Class) "
                "RETURN a.id AS sourceId, b.id AS targetId"
            )
        }


def getRelationTriples() -> set[tuple[int, int, str]]:
    """读全部语义关系边 (sourceId, targetId, relType)（对账用）。"""
    driver = getDriver()
    with driver.session() as session:
        return {
            (int(record["sourceId"]), int(record["targetId"]), record["relType"])
            for record in session.run(
                "MATCH (a:Class)-[r]->(b:Class) "
                "WHERE type(r) <> 'JOIN' "
                "RETURN a.id AS sourceId, b.id AS targetId, type(r) AS relType"
            )
        }


def deleteNode(label: str, nodeId: int) -> int:
    """删除指定 label 和 id 的节点（同时删除关联关系）。返回删除的节点数。

    label 必须来自 _ALLOWED_LABELS 白名单（CQL 标签不可参数化，需防御性校验）。
    """
    if label not in _ALLOWED_LABELS:
        raise ValueError(f"Invalid label: {label}")
    driver = getDriver()
    cql = f"""
        MATCH (n:{label} {{id: $id}})
        DETACH DELETE n
        RETURN count(n) AS deleted
    """
    with driver.session() as session:
        [record] = session.run(cql, id=nodeId)
        return int(record["deleted"])


# =============================================================================
# Upsert operations（幂等：建则建、有则改；seed 与 update 共用）
# =============================================================================


def upsertClassNode(
    id: int, name: str, alias: str | None, description: str | None, sourceTable: str | None
) -> None:
    """按 id 幂等创建/更新 Class 节点属性。"""
    driver = getDriver()
    cql = """
        MERGE (c:Class {id: $id})
        SET c.name = $name, c.alias = $alias,
            c.description = $description, c.sourceTable = $sourceTable
        RETURN c
    """
    with driver.session() as session:
        session.run(
            cql, id=id, name=name, alias=alias, description=description, sourceTable=sourceTable
        )


def upsertPropertyNode(
    id: int,
    name: str,
    alias: str | None,
    dataType: str,
    sourceColumn: str | None,
    isPrimaryKey: bool,
    isForeignKey: bool,
) -> None:
    """按 id 幂等创建/更新 Property 节点属性。"""
    driver = getDriver()
    cql = """
        MERGE (p:Property {id: $id})
        SET p.name = $name, p.alias = $alias,
            p.dataType = $dataType, p.sourceColumn = $sourceColumn,
            p.isPrimaryKey = $isPrimaryKey, p.isForeignKey = $isForeignKey
        RETURN p
    """
    with driver.session() as session:
        session.run(
            cql,
            id=id, name=name, alias=alias, dataType=dataType,
            sourceColumn=sourceColumn, isPrimaryKey=isPrimaryKey, isForeignKey=isForeignKey,
        )


def upsertMetricNode(
    id: int,
    name: str,
    alias: str | None,
    formula: str,
    aggFunction: str,
) -> None:
    """按 id 幂等创建/更新 Metric 节点属性。"""
    driver = getDriver()
    cql = """
        MERGE (m:Metric {id: $id})
        SET m.name = $name, m.alias = $alias,
            m.formula = $formula, m.aggFunction = $aggFunction
        RETURN m
    """
    with driver.session() as session:
        session.run(cql, id=id, name=name, alias=alias, formula=formula, aggFunction=aggFunction)


# =============================================================================
# Relationship operations
# =============================================================================


def linkClassHasProperty(classId: int, propertyId: int) -> None:
    """Class -[:HAS_PROPERTY]-> Property"""
    driver = getDriver()
    cql = """
        MATCH (c:Class {id: $classId}), (p:Property {id: $propertyId})
        MERGE (c)-[:HAS_PROPERTY]->(p)
    """
    with driver.session() as session:
        session.run(cql, classId=classId, propertyId=propertyId)


def linkPropertyReferences(propertyId: int, refClassId: int) -> None:
    """Property -[:REFERENCES]-> Class"""
    driver = getDriver()
    cql = """
        MATCH (p:Property {id: $propertyId}), (c:Class {id: $refClassId})
        MERGE (p)-[:REFERENCES]->(c)
    """
    with driver.session() as session:
        session.run(cql, propertyId=propertyId, refClassId=refClassId)


def linkMetricDerivedFrom(metricId: int, classId: int) -> None:
    """Metric -[:DERIVED_FROM]-> Class"""
    driver = getDriver()
    cql = """
        MATCH (m:Metric {id: $metricId}), (c:Class {id: $classId})
        MERGE (m)-[:DERIVED_FROM]->(c)
    """
    with driver.session() as session:
        session.run(cql, metricId=metricId, classId=classId)


def reconcilePropertyReferences(propertyId: int, refClassId: int | None) -> None:
    """重建 Property -[:REFERENCES]-> Class 关系为最新状态。

    refClassId 为 None 时仅删除旧关系（外键取消）。
    """
    driver = getDriver()
    with driver.session() as session:
        session.run(
            "MATCH (p:Property {id: $id})-[r:REFERENCES]->() DELETE r", id=propertyId
        )
        if refClassId:
            session.run(
                "MATCH (p:Property {id: $pid}), (c:Class {id: $cid}) MERGE (p)-[:REFERENCES]->(c)",
                pid=propertyId,
                cid=refClassId,
            )


def reconcileMetricDerivedFrom(metricId: int, targetClassId: int | None) -> None:
    """重建 Metric -[:DERIVED_FROM]-> Class 关系为最新状态。

    targetClassId 为 None 时仅删除旧关系（指标不再关联类）。
    """
    driver = getDriver()
    with driver.session() as session:
        session.run(
            "MATCH (m:Metric {id: $id})-[r:DERIVED_FROM]->() DELETE r", id=metricId
        )
        if targetClassId:
            session.run(
                "MATCH (m:Metric {id: $mid}), (c:Class {id: $cid}) MERGE (m)-[:DERIVED_FROM]->(c)",
                mid=metricId,
                cid=targetClassId,
            )


def reconcileClassSubclassOf(classId: int, parentClassId: int | None) -> None:
    """重建 Class -[:SUBCLASS_OF]-> Class 继承关系为最新状态。

    parentClassId 为 None 时仅删除旧继承边（取消继承）；
    否则先删除旧边再 MERGE 新边，保证 parent_class_id 单值语义。
    """
    driver = getDriver()
    with driver.session() as session:
        session.run(
            "MATCH (c:Class {id: $id})-[r:SUBCLASS_OF]->() DELETE r", id=classId
        )
        if parentClassId:
            session.run(
                "MATCH (c:Class {id: $cid}), (p:Class {id: $pid}) "
                "MERGE (c)-[:SUBCLASS_OF]->(p)",
                cid=classId,
                pid=parentClassId,
            )


def detectInheritanceCycle(classId: int, newParentId: int) -> bool:
    """检测将 classId 的父类设为 newParentId 是否会形成继承环。

    判据：newParentId 已是 classId 的后代（存在
    ``(newParentId)-[:SUBCLASS_OF*]->(classId)`` 路径），此时再建立
    ``classId -> newParentId`` 的边会闭合为环。``classId == newParentId`` 视为自环。
    """
    if classId == newParentId:
        return True
    driver = getDriver()
    cql = """
        MATCH (candidate:Class {id: $newParentId})-[:SUBCLASS_OF*]->(ancestor:Class {id: $classId})
        RETURN count(ancestor) > 0 AS hasCycle
    """
    with driver.session() as session:
        [record] = session.run(cql, classId=classId, newParentId=newParentId)
        return bool(record["hasCycle"])


def linkClassJoin(sourceClassId: int, targetClassId: int) -> None:
    """Class -[:JOIN]-> Class：关联目录入图（由 ontology_join 同步，幂等 MERGE）。

    JOIN 边与语义关系（linkClassRelation）分开维护：JOIN 由 ontology_join 行驱动，
    方向为 join 的 source → target；语义关系由 ontology_relation 行驱动。
    """
    driver = getDriver()
    cql = """
        MATCH (a:Class {id: $sourceId}), (b:Class {id: $targetId})
        MERGE (a)-[:JOIN]->(b)
    """
    with driver.session() as session:
        session.run(cql, sourceId=sourceClassId, targetId=targetClassId)


def deleteClassJoin(sourceClassId: int, targetClassId: int) -> None:
    """删除 Class -[:JOIN]-> Class 边（对应 ontology_join 行删除）。"""
    driver = getDriver()
    cql = """
        MATCH (a:Class {id: $sourceId})-[r:JOIN]->(b:Class {id: $targetId})
        DELETE r
    """
    with driver.session() as session:
        session.run(cql, sourceId=sourceClassId, targetId=targetClassId)


def linkClassRelation(sourceClassId: int, targetClassId: int, relType: str) -> None:
    """Class -[:relType]-> Class：语义关系镜像（relType 白名单防注入，幂等 MERGE）。"""
    _assertClassRelation(relType)
    driver = getDriver()
    cql = f"""
        MATCH (a:Class {{id: $sourceId}}), (b:Class {{id: $targetId}})
        MERGE (a)-[:{relType}]->(b)
    """
    with driver.session() as session:
        session.run(cql, sourceId=sourceClassId, targetId=targetClassId)


def deleteClassRelation(sourceClassId: int, targetClassId: int, relType: str) -> None:
    """删除 Class -[:relType]-> Class 边（对应 ontology_relation 行删除）。"""
    _assertClassRelation(relType)
    driver = getDriver()
    cql = f"""
        MATCH (a:Class {{id: $sourceId}})-[r:{relType}]->(b:Class {{id: $targetId}})
        DELETE r
    """
    with driver.session() as session:
        session.run(cql, sourceId=sourceClassId, targetId=targetClassId)


def syncOntologyNodes(
    classes: list[dict[str, Any]],
    properties: list[dict[str, Any]],
) -> dict[str, int]:
    """把 PG 本体全量 upsert 入 Neo4j（幂等「本体入图」对账，供批量关系引擎 syncGraph）。

    输入约定（由调用方从 PG 组装，字段名与节点属性一致）：
    - classes:   {"id","name","alias","description","sourceTable"}
    - properties:{"id","classId","name","alias","dataType","sourceColumn",
                  "isPrimaryKey","isForeignKey","refClassId"(可空)}
    同一连接内分 4 段 UNWIND + MERGE，分别 upsert (:Class)/(:Property) 节点、
    (:Class)-[:HAS_PROPERTY]->(:Property) 与 (:Property)-[:REFERENCES]->(:Class)
    （refClassId 非空才建）。幂等：重复调用仅对已存在节点 SET、不新增。

    返回本次处理行数计数 {"classes","properties","has_property_edges","reference_edges"}；
    失败由调用方 fail-open（_logNeo4jFailure），本函数不吞异常。
    """
    driver = getDriver()
    classRows = [c for c in classes if c.get("id") is not None]
    propRows = [
        p for p in properties if p.get("id") is not None and p.get("classId") is not None
    ]
    refRows = [
        {"propertyId": p["id"], "refClassId": p["refClassId"]}
        for p in propRows
        if p.get("refClassId") is not None
    ]
    statements = [
        (
            """
            UNWIND $rows AS r
            MERGE (c:Class {id: r.id})
            SET c.name = r.name, c.alias = r.alias,
                c.description = r.description, c.sourceTable = r.sourceTable
            """,
            classRows,
        ),
        (
            """
            UNWIND $rows AS r
            MERGE (p:Property {id: r.id})
            SET p.name = r.name, p.alias = r.alias, p.dataType = r.dataType,
                p.sourceColumn = r.sourceColumn,
                p.isPrimaryKey = r.isPrimaryKey, p.isForeignKey = r.isForeignKey
            """,
            propRows,
        ),
        (
            """
            UNWIND $rows AS r
            MATCH (c:Class {id: r.classId}), (p:Property {id: r.id})
            MERGE (c)-[:HAS_PROPERTY]->(p)
            """,
            propRows,
        ),
        (
            """
            UNWIND $rows AS r
            MATCH (p:Property {id: r.propertyId}), (c:Class {id: r.refClassId})
            MERGE (p)-[:REFERENCES]->(c)
            """,
            refRows,
        ),
    ]
    with driver.session() as session:
        for cql, rows in statements:
            session.run(cql, rows=rows)
    return {
        "classes": len(classRows),
        "properties": len(propRows),
        "has_property_edges": len(propRows),
        "reference_edges": len(refRows),
    }


# =============================================================================
# Graph query helpers
# =============================================================================


def getClassWithProperties(classId: int) -> dict[str, Any] | None:
    """查询 Class 及其直接关联的 Property 列表。"""
    driver = getDriver()
    cql = """
        MATCH (c:Class {id: $id})-[:HAS_PROPERTY]->(p:Property)
        RETURN c, collect(p) AS properties
    """
    with driver.session() as session:
        results = list(session.run(cql, id=classId))
        if not results:
            return None
        record = results[0]
        return {
            "class": dict(record["c"]),
            "properties": [dict(p) for p in record["properties"]],
        }


def getAllClassNodes() -> list[dict[str, Any]]:
    """返回所有 Class 节点。"""
    driver = getDriver()
    cql = "MATCH (c:Class) RETURN c ORDER BY c.name"
    with driver.session() as session:
        return [dict(r["c"]) for r in session.run(cql)]


def listNodesByLabel(label: str) -> list[dict[str, Any]]:
    """返回指定 label 的所有节点（Class | Property | Metric），按 name 排序。"""
    if label not in _ALLOWED_LABELS:
        raise ValueError(f"Invalid label: {label!r}")
    driver = getDriver()
    cql = f"MATCH (n:{label}) RETURN n ORDER BY n.name"
    with driver.session() as session:
        return [dict(r["n"]) for r in session.run(cql)]


def getNodeRelationships(label: str, nodeId: int) -> list[dict[str, Any]]:
    """返回指定节点的出边（任意方向），含 relType / targetId / targetName / targetLabel。"""
    if label not in _ALLOWED_LABELS:
        raise ValueError(f"Invalid label: {label!r}")
    driver = getDriver()
    cql = f"""
        MATCH (n:{label} {{id: $id}})-[r]-(related)
        RETURN type(r) AS relType,
               related.id AS targetId,
               related.name AS targetName,
               labels(related)[0] AS targetLabel
    """
    with driver.session() as session:
        return [dict(r) for r in session.run(cql, id=nodeId)]


# =============================================================================
# Business entity graph operations（Phase 6.2，幂等 MERGE）
# =============================================================================


def upsertBusinessEntityNode(
    label: str, key: str, code: str, name: str | None, source: str
) -> None:
    """按 key 幂等创建/更新业务实体节点（BusinessEntity + 具体类型双 label）。

    - key：实体唯一键（entity_mapping 的 enterprise_key 字符串化 / Contract 的 document_id）
    - code：业务编码（SUP000001 / RM-STEEL-001 / PO202608001 …）
    - source：来源标记（entity_mapping | sheet16_demo | document_catalog），便于溯源
    """
    _assertBusinessLabel(label)
    driver = getDriver()
    cql = f"""
        MERGE (b:BusinessEntity:{label} {{key: $key}})
        SET b.code = $code, b.name = $name, b.entityType = $label, b.source = $source
        RETURN b
    """
    with driver.session() as session:
        session.run(
            cql, key=key, code=code, name=name, label=label, source=source
        )


def linkBusinessRelation(
    relType: str,
    fromLabel: str,
    fromKey: str,
    toLabel: str,
    toKey: str,
    properties: dict[str, Any] | None = None,
) -> None:
    """幂等创建业务实体间关系边（MERGE，含可选边属性）。

    两端节点须已存在（MATCH 而非 MERGE，防止边写入悄悄创建孤立节点）。
    relType / fromLabel / toLabel 均经白名单校验后拼入 CQL。
    """
    _assertBusinessRelation(relType)
    _assertBusinessLabel(fromLabel)
    _assertBusinessLabel(toLabel)
    driver = getDriver()
    props = properties or {}
    cql = f"""
        MATCH (a:BusinessEntity:{fromLabel} {{key: $fromKey}}),
              (b:BusinessEntity:{toLabel} {{key: $toKey}})
        MERGE (a)-[r:{relType}]->(b)
        SET r += $props
    """
    with driver.session() as session:
        session.run(
            cql, fromKey=fromKey, toKey=toKey, props=props
        )


def deleteBusinessGraph() -> int:
    """清空业务实体子图（含全部边）。本体节点（Class/Property/Metric）不受影响。

    供 seed 重放与测试隔离使用；BusinessEntity 主 label 保证删除范围封闭。
    """
    driver = getDriver()
    cql = """
        MATCH (b:BusinessEntity)
        DETACH DELETE b
        RETURN count(b) AS deleted
    """
    with driver.session() as session:
        [record] = session.run(cql)
        return int(record["deleted"])


def countBusinessNodes() -> int:
    """业务实体节点总数（含 Contract 等文档实体）。"""
    driver = getDriver()
    with driver.session() as session:
        [record] = session.run("MATCH (b:BusinessEntity) RETURN count(b) AS cnt")
        return int(record["cnt"])


def countBusinessRelations() -> int:
    """业务实体间关系边总数（任意 BusinessEntity 节点间的边）。"""
    driver = getDriver()
    cql = """
        MATCH (:BusinessEntity)-[r]->(:BusinessEntity)
        RETURN count(r) AS cnt
    """
    with driver.session() as session:
        [record] = session.run(cql)
        return int(record["cnt"])


def getBusinessGraphSnapshot() -> dict[str, Any]:
    """业务关系图快照：节点（label/key/code/name）+ 边（relType/from/to）。

    供 seed 校验、前端图渲染（后续 Phase）与 6.3 图遍历 API 预研使用。
    """
    driver = getDriver()
    nodesCql = """
        MATCH (b:BusinessEntity)
        RETURN b.key AS key, b.code AS code, b.name AS name,
               b.entityType AS entityType, b.source AS source,
               labels(b) AS labels
        ORDER BY b.entityType, b.code
    """
    edgesCql = """
        MATCH (a:BusinessEntity)-[r]->(b:BusinessEntity)
        RETURN a.key AS fromKey, a.entityType AS fromType,
               type(r) AS relType,
               b.key AS toKey, b.entityType AS toType
        ORDER BY type(r), a.key
    """
    with driver.session() as session:
        nodes = [dict(r) for r in session.run(nodesCql)]
        edges = [dict(r) for r in session.run(edgesCql)]
        return {"nodes": nodes, "edges": edges}


def isNeo4jAvailable() -> bool:
    """探测 Neo4j 连接是否可用（供集成测试按需跳过，避免 CI 无图库时阻断）。"""
    try:
        driver = getDriver()
        with driver.session() as session:
            session.run("RETURN 1")
        return True
    except Exception:  # noqa: BLE001 - 探测语义：任何连接异常都视为不可用
        logger.warning("Neo4j unavailable, business graph tests will be skipped")
        return False


def traverseBusinessGraph(
    startLabel: str,
    startKey: str,
    maxHops: int,
) -> list[dict[str, Any]]:
    """从起始业务实体做多跳遍历（方向不限），返回逐跳展开的可达链。

    每行一条 (深度, 末边起点, 末边关系, 终点)，from→relType→to 是真实边：
    - depth：1..maxHops（起始节点自身不返回）
    - from*：**末边（rels[-1]）的真实前驱节点**（= nodes(path)[-2]）。
      depth=1 时即起始节点；depth≥2 时为路径中间节点（如 S→M→PO 行的
      from=Material、rel=CONTAINS、to=PurchaseOrder）。
    - to*：可达终点节点 key / code / name / entityType
    - relType：末边关系类型（白名单内的 6 种业务关系）

    用 variable-length path ``[*1..N]``（由 maxHops 拼入 CQL 前强制
    ``1 <= maxHops <= _MAX_TRAVERSAL_HOPS``，防无界遍历）。
    遍历会展开所有方向（-），符合业务推理语义（如从物料反查供应商）。
    """
    _assertBusinessLabel(startLabel)
    if not 1 <= maxHops <= _MAX_TRAVERSAL_HOPS:
        raise ValueError(
            f"maxHops must be in [1, {_MAX_TRAVERSAL_HOPS}], got {maxHops}"
        )
    driver = getDriver()
    cql = f"""
        MATCH path = (start:BusinessEntity:{startLabel} {{key: $key}})
              -[rels*1..{maxHops}]-(end:BusinessEntity)
        WHERE start <> end
        WITH start, end, rels, nodes(path) AS pathNodes, size(rels) AS depth
        ORDER BY depth, end.key
        RETURN depth,
               pathNodes[-2].key AS fromKey, pathNodes[-2].code AS fromCode,
               pathNodes[-2].name AS fromName, pathNodes[-2].entityType AS fromType,
               type(rels[-1]) AS relType,
               end.key AS toKey, end.code AS toCode,
               end.name AS toName, end.entityType AS toType
    """
    with driver.session() as session:
        return [
            {
                "depth": r["depth"],
                "fromKey": r["fromKey"],
                "fromCode": r["fromCode"],
                "fromName": r["fromName"],
                "fromType": r["fromType"],
                "relType": r["relType"],
                "toKey": r["toKey"],
                "toCode": r["toCode"],
                "toName": r["toName"],
                "toType": r["toType"],
            }
            for r in session.run(cql, key=startKey)
        ]


def getBusinessNode(label: str, key: str) -> dict[str, Any] | None:
    """查询单个业务实体节点属性（不存在返回 None）。"""
    _assertBusinessLabel(label)
    driver = getDriver()
    cql = f"""
        MATCH (b:BusinessEntity:{label} {{key: $key}})
        RETURN b.key AS key, b.code AS code, b.name AS name,
               b.entityType AS entityType, b.source AS source, labels(b) AS labels
    """
    with driver.session() as session:
        records = list(session.run(cql, key=key))
        return dict(records[0]) if records else None

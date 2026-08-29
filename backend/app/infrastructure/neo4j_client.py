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

logger = logging.getLogger(__name__)

# 仅允许删除这些已知 label（deleteNode 通过字符串拼接 label，必须白名单防注入）
_ALLOWED_LABELS = frozenset({"Class", "Property", "Metric"})


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

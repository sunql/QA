"""发布镜像的 Neo4j 写路径（P3b D22）。

**与主图写路径彻底分开**（独立模块、独立白名单、独立 CQL 模板）：

- 镜像节点 label 只有 `ReleaseClass` / `ReleaseProperty` / `ReleaseMetric` /
  `ReleaseKpiCatalog` ⇒ 主图消费方 `MATCH (c:Class)` **不命中镜像** ⇒ 主图零影响；
- 镜像边的关系类型只有白名单里的几种 ⇒ 语义关系边（`ontology_relation` 驱动）
  不可能被写进镜像；
- 节点身份 = (label, unified_id, release_id) ⇒ 幂等；
- 边的发布作用域由**端点**承载 ⇒ 边不带属性、不带键，也不需要删边重建。

**为什么守卫这么严**：Neo4j 侧**没有**写目标闸门、**没有**自动备份（Milvus 侧有
`scripts/milvus_write_target_guard.py`，Neo4j 侧现在才有本模块配套的
`neo4j_write_target_guard.py`）⇒ 白名单 + 写目标闸门是仅有的防线。
label / 关系类型在 CQL 里不可参数化，只能拼字符串 ⇒ 一律走固定映射表 + 白名单。
"""

from __future__ import annotations

import logging
from typing import Any

from app.domain.id_mapping import UNIFIED_ID_PREFIX, UNIFIED_ID_SEPARATOR
from app.domain.semantic_graph_mirror_spec import (
    RELEASE_MIRROR_ENTITY_BUSINESS_OBJECTS,
    RELEASE_MIRROR_ENTITY_TYPES,
    RELEASE_MIRROR_LABELS,
    RELEASE_MIRROR_NODE_FIELDS,
    RELEASE_MIRROR_REL_ENDPOINT_TYPES,
    RELEASE_MIRROR_REL_TYPES,
    RELEASE_MIRROR_RELEASE_FIELDS,
)
from app.infrastructure.neo4j_client import getDriver
from app.infrastructure.neo4j_write_target_guard import assertSafeNeo4jTarget

logger = logging.getLogger(__name__)

# 镜像写批大小（UNWIND 每批参数行数）。本体图体量小（千级），
# 但批量参数行数仍需上限，防单事务过大拖垮图库。
_MIRROR_BATCH_SIZE = 5_000


def _effectiveTargetUri() -> str:
    """当前生效的 Neo4j URI（写目标闸门按它判定）。"""
    from app.config import getSettings

    return getSettings().neo4jUri


def _assertEntityType(entityType: str) -> None:
    """实体类型白名单校验（CQL 拼接前置防御）。"""
    if not isinstance(entityType, str) or entityType not in RELEASE_MIRROR_ENTITY_TYPES:
        raise ValueError(f"Invalid release mirror entity type: {entityType!r}")


def _assertRelType(relType: str) -> None:
    """镜像关系类型白名单校验（CQL 拼接前置防御）。"""
    if not isinstance(relType, str) or relType not in RELEASE_MIRROR_REL_TYPES:
        raise ValueError(f"Invalid release mirror relation type: {relType!r}")


def _assertUnifiedId(value: Any, entityType: str | None = None) -> None:
    """校验 unified_id：非空，且（给了实体类型时）业务对象段必须对得上。

    防「把 PROPERTY 的 id 写成 ReleaseClass」这类**语义错位**——图上看着正常，
    对账时才炸。
    """
    unifiedId = value
    if not isinstance(unifiedId, str) or not unifiedId.strip():
        raise ValueError(f"镜像行缺 unified_id: {unifiedId!r}")
    if entityType is None:
        return
    expected = RELEASE_MIRROR_ENTITY_BUSINESS_OBJECTS[entityType]
    parts = unifiedId.split(UNIFIED_ID_SEPARATOR)
    if (
        len(parts) != 3
        or parts[0] != UNIFIED_ID_PREFIX
        or parts[1].upper() != expected
    ):
        raise ValueError(
            f"镜像行 unified_id={unifiedId!r} 与实体类型 {entityType!r} 不匹配"
            f"（期望 obj:{expected}:…）"
        )


def _assertReleaseFields(row: dict[str, Any]) -> None:
    """镜像行必须带齐发布标识字段（release_id / release_version / mirror_of）。"""
    missing = [name for name in RELEASE_MIRROR_RELEASE_FIELDS if name not in row]
    if missing:
        raise ValueError(f"镜像行缺发布标识字段: {missing}")


def _assertEdgeShape(edge: dict[str, Any]) -> None:
    """校验一条镜像边：relType / 两端类型 / 两端 uid。

    端点类型必须与契约里的 (relType → 源/目标类型) 一致 ⇒ 防「HAS_PROPERTY
    连到两个类上」这种写出去才发现的错。
    """
    relType = edge.get("relType", "")
    _assertRelType(relType)
    sourceType = edge.get("sourceType", "")
    targetType = edge.get("targetType", "")
    _assertEntityType(sourceType)
    _assertEntityType(targetType)
    expectedSource, expectedTarget = RELEASE_MIRROR_REL_ENDPOINT_TYPES[relType]
    if (sourceType, targetType) != (expectedSource, expectedTarget):
        raise ValueError(
            f"镜像边 {relType} 的端点类型应为 ({expectedSource}, {expectedTarget})，"
            f"实际 ({sourceType}, {targetType})"
        )
    if not str(edge.get("sourceUid") or "").strip():
        raise ValueError(f"镜像边缺 sourceUid: {edge!r}")
    if not str(edge.get("targetUid") or "").strip():
        raise ValueError(f"镜像边缺 targetUid: {edge!r}")


def upsertReleaseMirrorNodes(
    entityType: str,
    releaseId: int,
    releaseVersion: str,
    rows: list[dict[str, Any]],
    *,
    allowProdWrite: bool = False,
) -> int:
    """批量幂等写镜像节点（UNWIND + MERGE），返回触及节点数。

    - entityType：必须在契约白名单内（`class` / `property` / `metric` / `kpi`）；
    - rows：每行必须含非空 `unified_id`（且业务对象段对得上）、带齐发布标识字段，
      且 `release_id` 必须与参数一致（不一致 ⇒ 拒写，防「把这一版的节点写到
      别的发布里去」）；
    - 幂等：MERGE 键 = (unified_id, release_id) ⇒ 重复物化不产生重复节点；
    - **写目标闸门**：默认只准写测试实例（Neo4j 无备份 ⇒ 误写不可逆）。
    """
    _assertEntityType(entityType)
    for row in rows:
        _assertUnifiedId(row.get("unified_id"), entityType)
        _assertReleaseFields(row)
        if row.get("release_id") != releaseId:
            raise ValueError(
                f"镜像行 release_id={row.get('release_id')!r} 与参数 {releaseId!r} 不一致"
            )
    # 输入校验通过后才看写目标（校验与环境无关 ⇒ 报错信息不被环境盖掉）
    assertSafeNeo4jTarget(_effectiveTargetUri(), allowProdWrite=allowProdWrite)
    if not rows:
        return 0

    label = RELEASE_MIRROR_LABELS[entityType]
    fieldNames = RELEASE_MIRROR_NODE_FIELDS[entityType]
    setClause = ",\n            ".join(f"n.{name} = row.{name}" for name in fieldNames)
    cql = f"""
        UNWIND $rows AS row
        MERGE (n:{label} {{unified_id: row.unified_id, release_id: $releaseId}})
        SET n.release_version = $releaseVersion,
            n.mirror_of = row.mirror_of,
            {setClause}
        RETURN count(n) AS cnt
    """
    driver = getDriver()
    written = 0
    with driver.session() as session:
        for start in range(0, len(rows), _MIRROR_BATCH_SIZE):
            summary = session.run(
                cql,
                rows=rows[start : start + _MIRROR_BATCH_SIZE],
                releaseId=releaseId,
                releaseVersion=releaseVersion,
            ).single()
            written += int(summary["cnt"]) if summary else 0
    return written


def upsertReleaseMirrorEdges(
    releaseId: int,
    edges: list[dict[str, Any]],
    *,
    allowProdWrite: bool = False,
) -> int:
    """批量幂等写镜像边，按 (relType, 源类型, 目标类型) 分组，返回触及边数。

    端点用 **MATCH**（不是 MERGE）⇒ 端点不存在时**不会**凭空造出孤立节点；
    命中的边数少于请求数 ⇒ 抛错（端点缺失必须可见，不能静默少写）。
    """
    for edge in edges:
        _assertEdgeShape(edge)
    # 输入校验通过后才看写目标（校验与环境无关 ⇒ 报错信息不被环境盖掉）
    assertSafeNeo4jTarget(_effectiveTargetUri(), allowProdWrite=allowProdWrite)
    if not edges:
        return 0

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for edge in edges:
        key = (edge["relType"], edge["sourceType"], edge["targetType"])
        groups.setdefault(key, []).append(edge)

    driver = getDriver()
    written = 0
    with driver.session() as session:
        for (relType, sourceType, targetType), groupRows in groups.items():
            sourceLabel = RELEASE_MIRROR_LABELS[sourceType]
            targetLabel = RELEASE_MIRROR_LABELS[targetType]
            cql = f"""
                UNWIND $rows AS row
                MATCH (a:{sourceLabel} {{unified_id: row.sourceUid, release_id: $releaseId}})
                MATCH (b:{targetLabel} {{unified_id: row.targetUid, release_id: $releaseId}})
                MERGE (a)-[r:{relType}]->(b)
                RETURN count(r) AS cnt
            """
            for start in range(0, len(groupRows), _MIRROR_BATCH_SIZE):
                batch = groupRows[start : start + _MIRROR_BATCH_SIZE]
                summary = session.run(
                    cql, rows=batch, releaseId=releaseId
                ).single()
                matched = int(summary["cnt"]) if summary else 0
                if matched < len(batch):
                    raise ValueError(
                        f"镜像边端点缺失：{relType} 本批请求 {len(batch)} 条，"
                        f"实际命中 {matched} 条（端点节点未写入？）"
                    )
                written += matched
    return written


def countReleaseMirrorNodes(releaseId: int) -> dict[str, int]:
    """按实体类型统计某次发布的镜像节点数（对账用）。"""
    driver = getDriver()
    counts: dict[str, int] = {}
    with driver.session() as session:
        for entityType in RELEASE_MIRROR_ENTITY_TYPES:
            label = RELEASE_MIRROR_LABELS[entityType]
            record = session.run(
                f"MATCH (n:{label} {{release_id: $releaseId}}) RETURN count(n) AS cnt",
                releaseId=releaseId,
            ).single()
            counts[entityType] = int(record["cnt"]) if record else 0
    return counts


def countReleaseMirrorEdges(releaseId: int) -> dict[str, int]:
    """按关系类型统计某次发布的镜像边数（对账用）。

    两端都必须是该发布的**镜像节点**（边的发布作用域由端点承载）⇒ 统计口径与
    写入口径一致。CQL 里**带上两端 label**（关系类型 → 端点类型由契约固定）
    ⇒ 不扫描整图，也不会误统计「碰巧带 release_id 属性」的其它节点。
    """
    driver = getDriver()
    counts: dict[str, int] = {}
    with driver.session() as session:
        for relType in sorted(RELEASE_MIRROR_REL_TYPES):
            sourceType, targetType = RELEASE_MIRROR_REL_ENDPOINT_TYPES[relType]
            sourceLabel = RELEASE_MIRROR_LABELS[sourceType]
            targetLabel = RELEASE_MIRROR_LABELS[targetType]
            record = session.run(
                f"""
                MATCH (a:{sourceLabel})-[r:{relType}]->(b:{targetLabel})
                WHERE a.release_id = $releaseId AND b.release_id = $releaseId
                RETURN count(r) AS cnt
                """,
                releaseId=releaseId,
            ).single()
            counts[relType] = int(record["cnt"]) if record else 0
    return counts


def pruneReleaseMirror(
    releaseId: int,
    keepUnifiedIds: set[str],
    *,
    confirm: bool,
    allowProdWrite: bool = False,
) -> int:
    """清理某次发布里**计划外**的镜像节点与镜像边，返回删除的节点数。

    - 必须显式 `confirm=True`（Neo4j 无备份 ⇒ 删操作默认拒绝）；
    - 节点：删该 release_id 下、不在 keep 集合里的镜像节点（DETACH DELETE 一并清边）；
    - 边：**两端都还在 keep 里**的边也要单独清——否则两次物化之间被删掉的
      `JOIN`（两端类都还在）会永远残留在图上，镜像 ≠ 计划（对账会一直红）。
    """
    if not confirm:
        raise ValueError("pruneReleaseMirror 会删除图数据，必须显式传 confirm=True 确认")
    assertSafeNeo4jTarget(_effectiveTargetUri(), allowProdWrite=allowProdWrite)
    driver = getDriver()
    deleted = 0
    with driver.session() as session:
        for entityType in RELEASE_MIRROR_ENTITY_TYPES:
            label = RELEASE_MIRROR_LABELS[entityType]
            # 先 collect 再删：`DETACH DELETE` 之后节点为 null，`count(n)` 会恒为 0
            # （计数必须在删除前取，否则「删了多少」永远是 0）。
            record = session.run(
                f"""
                MATCH (n:{label} {{release_id: $releaseId}})
                WHERE NOT n.unified_id IN $keep
                WITH collect(n) AS doomed
                FOREACH (x IN doomed | DETACH DELETE x)
                RETURN size(doomed) AS cnt
                """,
                releaseId=releaseId,
                keep=list(keepUnifiedIds),
            ).single()
            deleted += int(record["cnt"]) if record else 0
        _pruneStaleEdges(session, releaseId, keepUnifiedIds)
    logger.info(
        "prune release mirror: releaseId=%s deleted=%s", releaseId, deleted
    )
    return deleted


def _pruneStaleEdges(
    session: Any, releaseId: int, keepUnifiedIds: set[str]
) -> None:
    """删掉「端点还在、但已不在计划里」的镜像边。

    两端都保留的边（例如 manifest 里删掉了一条 JOIN）不会被 DETACH DELETE
    顺带清掉 ⇒ 必须按 relType 单独扫一遍。
    """
    for relType, (sourceType, targetType) in sorted(
        RELEASE_MIRROR_REL_ENDPOINT_TYPES.items()
    ):
        sourceLabel = RELEASE_MIRROR_LABELS[sourceType]
        targetLabel = RELEASE_MIRROR_LABELS[targetType]
        session.run(
            f"""
            MATCH (a:{sourceLabel})-[r:{relType}]->(b:{targetLabel})
            WHERE a.release_id = $releaseId AND b.release_id = $releaseId
              AND NOT (a.unified_id IN $keep AND b.unified_id IN $keep)
            DELETE r
            """,
            releaseId=releaseId,
            keep=list(keepUnifiedIds),
        )

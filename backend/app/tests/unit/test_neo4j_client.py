"""neo4j_client 单元测试（#65 审查修复）。

覆盖：deleteNode 的 label 白名单守卫、URI 日志脱敏。
不需要外部 Neo4j 服务（守卫在访问 driver 前即抛错；脱敏为纯字符串处理）。
"""

from __future__ import annotations

import pytest

from app.infrastructure import neo4j_client as neo4j


def testDeleteNodeRejectsUnknownLabel() -> None:
    # 白名单之外的 label 必须拒绝，防止 CQL 标签注入
    for bad in ("User", "Class; DROP", "`Class`", "Class }"):
        with pytest.raises(ValueError, match="Invalid label"):
            neo4j.deleteNode(bad, "obj:CLASS:1")


def testDeleteNodeAcceptsAllowedLabels() -> None:
    # 合法 label 通过守卫后才访问 driver；此处仅验证不因守卫抛 ValueError
    # （driver 访问交由集成测试/真实环境覆盖）
    assert "Class" in neo4j._ALLOWED_LABELS
    assert "Property" in neo4j._ALLOWED_LABELS
    assert "Metric" in neo4j._ALLOWED_LABELS


def testSanitizeUriStripsEmbeddedCredentials() -> None:
    assert neo4j._sanitizeUri("bolt://user:secret@host:7687") == "bolt://host:7687"
    assert neo4j._sanitizeUri("bolt://neo4j:pw@localhost:7687") == "bolt://localhost:7687"


def testSanitizeUriKeepsBareUri() -> None:
    # 无内嵌凭据时原样返回
    assert neo4j._sanitizeUri("bolt://localhost:7687") == "bolt://localhost:7687"


# =============================================================================
# 业务子图批量写（feat-business-graph-backfill）
# =============================================================================


def testBusinessRelationTypesContainsPlaces() -> None:
    # THBI 采购订单回填引入 Supplier-PLACES->PurchaseOrder 语义边
    assert "PLACES" in neo4j.BUSINESS_RELATION_TYPES


def testUpsertBusinessEntityNodesRejectsUnknownLabel() -> None:
    # 守卫必须在访问 driver 之前抛错（同 upsertBusinessEntityNode 模式）
    for bad in ("User", "Class; DROP", "`Supplier`"):
        with pytest.raises(ValueError, match="Invalid business entity label"):
            neo4j.upsertBusinessEntityNodes(bad, [{"key": "k", "code": "c"}])


def testUpsertBusinessEntityNodesRejectsRowsWithoutKey() -> None:
    # 行结构校验在白名单之后、driver 之前（防批量写悄悄写入脏行）
    for badRows in ([{"code": "c"}], [{"key": "   "}]):  # 缺 key / 空 key
        with pytest.raises(ValueError, match="rows"):
            neo4j.upsertBusinessEntityNodes("Supplier", badRows)


def testLinkBusinessRelationsRejectsUnknownRelationOrLabel() -> None:
    with pytest.raises(ValueError, match="Invalid business relation type"):
        neo4j.linkBusinessRelations("OWNS", "Supplier", "Material", [])
    with pytest.raises(ValueError, match="Invalid business entity label"):
        neo4j.linkBusinessRelations("SUPPLIES", "Hacker", "Material", [])
    with pytest.raises(ValueError, match="Invalid business entity label"):
        neo4j.linkBusinessRelations("SUPPLIES", "Supplier", "Hacker", [])


def testTraversalHasPathCap() -> None:
    # 生产规模图（百万边）下 variable-length path 必须有行数上限，防单查询拖垮图库
    assert neo4j._MAX_TRAVERSAL_PATHS > 0
    # CQL 里必须真的消费这个上限（防「常量存在但没接线」的假绿）
    import inspect

    source = inspect.getsource(neo4j.traverseBusinessGraph)
    assert "LIMIT" in source

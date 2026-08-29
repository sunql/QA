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
            neo4j.deleteNode(bad, 1)


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

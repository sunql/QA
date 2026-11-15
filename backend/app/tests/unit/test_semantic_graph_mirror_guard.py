"""发布镜像的**写目标闸门**单测（P3b D22）。

为什么要单独钉：镜像写路径在 Neo4j 侧**没有**自动备份 ⇒ 误写不可逆 ⇒
写目标闸门是唯一防线。闸门一旦被改松，没人会在 review 里注意到。

闸门本体在 `app/infrastructure/neo4j_write_target_guard.py`（库层，所有调用方
都生效）；脚本只是把它包成脚本退出语义。

双向断言（见记忆 qa-system-guard-false-positive-tests）：
  - 坏目标（生产端口 / 缺省端口 / 任意其它端口）⇒ 必须被拦；
  - 好目标（测试实例 7688）⇒ 必须放行，且不得误伤。
"""

from __future__ import annotations

import pytest

from app.infrastructure.neo4j_write_target_guard import (
    assertSafeNeo4jTarget,
    describeNeo4jTarget,
    effectiveNeo4jPort,
)
from scripts.semantic_graph_mirror_realdata import _assertSafeNeo4jTarget

PROD_URI = "bolt://localhost:7687"
TEST_URI = "bolt://localhost:7688"


def test_prod_port_is_refused() -> None:
    """生产端口必须被拦（且提示要显式 opt-in）。"""
    with pytest.raises(ValueError, match="--allow-prod-write"):
        assertSafeNeo4jTarget(PROD_URI)


def test_unknown_port_is_refused() -> None:
    """生产实例换个端口/主机就绕过去 ⇒ 闸门默认只准测试实例端口。"""
    for uri in ("bolt://qa-neo4j.internal:7687", "bolt://localhost:7699"):
        with pytest.raises(ValueError):
            assertSafeNeo4jTarget(uri)


def test_missing_port_is_refused() -> None:
    """`bolt://neo4j`（没写端口）⇒ 驱动连的就是默认 7687 ⇒ 必须当成生产拦掉。

    按字符串切分端口会把这种 URI 判成「不是 7687」⇒ 闸门失效。
    """
    for uri in ("bolt://neo4j", "bolt://127.0.0.1", "bolt://[::1]", "neo4j://qa-neo4j"):
        with pytest.raises(ValueError):
            assertSafeNeo4jTarget(uri)


def test_test_instance_is_allowed() -> None:
    """正确目标不被误拦（不抛即放行）。"""
    assertSafeNeo4jTarget(TEST_URI)


def test_explicit_opt_in_allows_any_target() -> None:
    """显式 opt-in 才放行其它目标。"""
    assertSafeNeo4jTarget(PROD_URI, allowProdWrite=True)


def test_effective_port_matches_driver_default() -> None:
    """端口口径必须与驱动一致（缺省 bolt 端口 = 7687）。"""
    assert effectiveNeo4jPort("bolt://neo4j") == 7687
    assert effectiveNeo4jPort("bolt://localhost:7688") == 7688
    assert effectiveNeo4jPort("bolt://localhost:7688/") == 7688


def test_target_description_drops_credentials() -> None:
    """报告会被贴进工单/日志 ⇒ URI 里的口令不能出现。"""
    described = describeNeo4jTarget("bolt://neo4j:s3cr3t@qa-neo4j.internal:7688")
    assert described == "neo4j://qa-neo4j.internal:7688"
    assert "s3cr3t" not in described
    assert "@" not in described


def test_script_wrapper_exits_instead_of_raising() -> None:
    """脚本侧把闸门失败转成退出（不抛栈），且消息指向显式 opt-in。"""
    with pytest.raises(SystemExit) as exc:
        _assertSafeNeo4jTarget(PROD_URI, allowProdWrite=False)
    assert "--allow-prod-write" in str(exc.value)


def test_script_wrapper_allows_test_instance() -> None:
    _assertSafeNeo4jTarget(TEST_URI, allowProdWrite=False)

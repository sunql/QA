"""Neo4j 测试实例守卫（fix-neo4j-test-isolation）。

背景（2026-10-03 事故）：宿主机跑集成测试时 `NEO4J_URI` 未设 ⇒ 用 config 默认
`bolt://localhost:7687`，而生产 Neo4j 正发布在这个端口 ⇒ conftest 的
`neo4jCleanDriver` teardown 把生产图谱 `MATCH (n) WHERE n:Class OR n:Property
OR n:Metric DETACH DELETE n` 清空（实测 :Class 64 → 0）。

本守卫的作用：**未显式配置 `TEST_NEO4J_URI` 就 fail-fast**，绝不静默回落到
生产端口。与 PG 侧 `resolveTestDatabaseUrl()` 的既有契约同形。

双向断言（见记忆 qa-system-guard-false-positive-tests）：
  - 坏输入（未配置 / 配置成生产端口）→ 必须被拦
  - 正确输入（配了独立实例）→ 必须放行，且不得误伤
"""

from __future__ import annotations

import os

import pytest

from app.tests._neo4j_support import (
    assertAppNeo4jIsIsolated,
    installTestNeo4jEnv,
    resolveTestNeo4jUri,
)


PROD_URI = "bolt://localhost:7687"
TEST_URI = "bolt://localhost:7688"


@pytest.fixture(autouse=True)
def _isolateEnv(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例从「干净环境」出发，避免宿主机真设了 TEST_NEO4J_URI 导致假绿。"""
    monkeypatch.delenv("TEST_NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_URI", raising=False)


def test_missing_env_fails_fast() -> None:
    """未配置 TEST_NEO4J_URI ⇒ 抛错，且消息点明「禁止连生产」。"""
    with pytest.raises(RuntimeError) as exc:
        resolveTestNeo4jUri()
    msg = str(exc.value)
    assert "TEST_NEO4J_URI" in msg
    assert "生产" in msg


def test_prod_env_is_not_accepted() -> None:
    """把 TEST_NEO4J_URI 配成生产端口也必须拦下 —— 这正是事故的形态。"""
    with pytest.raises(RuntimeError):
        resolveTestNeo4jUri(uri=PROD_URI)


def test_configured_uri_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """配了独立实例 ⇒ 原样返回（正确输入不被误拦）。"""
    monkeypatch.setenv("TEST_NEO4J_URI", TEST_URI)
    assert resolveTestNeo4jUri() == TEST_URI


def test_never_returns_prod_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """无论 env 怎么设，都不得回落到 config 的 7687 默认值。

    这是本守卫唯一要防的事：`NEO4J_URI` 设成生产（宿主机 .env 的常见状态）
    也不能让测试连过去。
    """
    monkeypatch.setenv("NEO4J_URI", PROD_URI)
    with pytest.raises(RuntimeError):
        resolveTestNeo4jUri()


# ---------------------------------------------------------------------------
# installTestNeo4jEnv：把**应用侧**的 config 也钉到测试实例。
#
# 只守 fixture 是不够的：`test_ontology_relation_graph_integration.py` 走的是
# `neo4j.getDriver()`（读 settings.neo4jUri），夹具的 URI 与它各算各的 ⇒
# 「夹具连测试实例、被测代码连生产」这种半隔离比不隔离更危险。
# ---------------------------------------------------------------------------


def test_install_overrides_preexisting_prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主机已把 NEO4J_URI 设成生产（.env 的常见状态）也必须被覆盖。"""
    monkeypatch.setenv("TEST_NEO4J_URI", TEST_URI)
    monkeypatch.setenv("NEO4J_URI", PROD_URI)

    assert installTestNeo4jEnv() == TEST_URI
    assert os.environ["NEO4J_URI"] == TEST_URI
    assert os.environ["NEO4J_URI"] != PROD_URI


def test_install_without_test_uri_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置测试实例 ⇒ 抛错，且**不得**顺手改动 NEO4J_URI（半配置更危险）。"""
    monkeypatch.setenv("NEO4J_URI", PROD_URI)

    with pytest.raises(RuntimeError):
        installTestNeo4jEnv()
    assert os.environ["NEO4J_URI"] == PROD_URI


# ---------------------------------------------------------------------------
# assertAppNeo4jIsIsolated：破坏性清理前的最后一道闸。
#
# 存在的意义是抓「配置漂移」——有人只设了 TEST_NEO4J_URI 却没走 install，
# 或中途把 NEO4J_URI 改回生产。此时应用侧读的是生产，清理会打到线上。
# ---------------------------------------------------------------------------


def test_assert_isolation_rejects_drifted_app_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST_NEO4J_URI=测试实例 但应用侧仍是生产 ⇒ 必须拦（半隔离）。"""
    monkeypatch.setenv("TEST_NEO4J_URI", TEST_URI)

    with pytest.raises(RuntimeError) as exc:
        assertAppNeo4jIsIsolated(appUri=PROD_URI)
    assert "NEO4J_URI" in str(exc.value)


def test_assert_isolation_passes_when_aligned(monkeypatch: pytest.MonkeyPatch) -> None:
    """两侧一致 ⇒ 放行（正确配置不被误拦）。"""
    monkeypatch.setenv("TEST_NEO4J_URI", TEST_URI)

    assertAppNeo4jIsIsolated(appUri=TEST_URI)  # 不抛即通过

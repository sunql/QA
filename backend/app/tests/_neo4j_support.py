"""测试用 Neo4j 实例解析（fix-neo4j-test-isolation）。

与 `_pg_support.resolveTestDatabaseUrl()` 同形的契约：**未显式配置测试实例就
fail-fast**，绝不静默回落到 config 的默认值。

为什么必须有这层守卫（2026-10-03 事故）：`app/config.py` 的 `neo4jUri` 默认是
`bolt://localhost:7687`，而生产容器正把 7687 发布到宿主机。宿主机跑 pytest 时
若没人设 `NEO4J_URI`，就悄悄连上了**生产**；`tests/integration/conftest.py` 的
`neo4jCleanDriver` teardown 会执行
`MATCH (n) WHERE n:Class OR n:Property OR n:Metric DETACH DELETE n`
⇒ 跑一次集成测试清一次生产图谱（实测 :Class 64 → 0）。

「忘了配测试实例」这件事本身无法在代码层杜绝，但可以让它**吵闹地失败**而不是
安静地删库 —— 这就是本模块的全部职责。

用法：
    TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/integration

独立实例的起法见 `Harness/changes/fix-neo4j-test-isolation/summary.md`
（Neo4j Community 不支持多数据库，故按 `qa-pg-a1` 先例单起容器）。
"""

from __future__ import annotations

import os
from urllib.parse import urlparse


# 生产 Neo4j 的默认端口。任何指向它的测试连接都视为事故。
_PROD_DEFAULT_PORT = 7687

_GUARD_HINT = (
    "集成测试会清空 Neo4j 的 Class/Property/Metric 标签，禁止连生产实例。"
    "请指向独立测试实例（起法见 "
    "Harness/changes/fix-neo4j-test-isolation/summary.md），例如：\n"
    "    docker run -d --name qa-neo4j-test -p 7688:7687 -p 7475:7474 \\\n"
    "        -e NEO4J_AUTH=neo4j/<pw> -e NEO4J_PLUGINS='[\"apoc\"]' \\\n"
    "        neo4j:5.23-community\n"
    "    TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/integration"
)


def _rejectProdPort(uri: str) -> None:
    """拒绝指向生产端口的 URI（含用户误配的情形）。"""
    try:
        port = urlparse(uri).port
    except ValueError as exc:  # 端口非法
        raise RuntimeError(f"TEST_NEO4J_URI 无法解析：{uri!r}（{exc}）") from exc
    if port == _PROD_DEFAULT_PORT:
        raise RuntimeError(
            f"TEST_NEO4J_URI 指向生产 Neo4j 端口 {_PROD_DEFAULT_PORT}（{uri!r}）。{_GUARD_HINT}"
        )


def resolveTestNeo4jUri(*, uri: str | None = None) -> str:
    """解析测试用 Neo4j URI；未配置或指向生产端口时 fail-fast。

    Args:
        uri: 显式指定（供测试注入用）。为 None 时读 `TEST_NEO4J_URI`。

    Raises:
        RuntimeError: 未配置 `TEST_NEO4J_URI`，或解析结果落在生产端口上。
    """
    resolved = uri if uri is not None else os.environ.get("TEST_NEO4J_URI")
    if not resolved:
        raise RuntimeError(
            f"未配置 TEST_NEO4J_URI：测试会连上 config 默认的 "
            f"bolt://localhost:{_PROD_DEFAULT_PORT}（即生产实例）。{_GUARD_HINT}"
        )
    _rejectProdPort(resolved)
    return resolved


def installTestNeo4jEnv() -> str:
    """把**应用侧**的 Neo4j 配置也钉到测试实例，返回该 URI。

    光守夹具不够：`test_ontology_relation_graph_integration.py` 等路径走应用的
    `neo4j.getDriver()`（读 `settings.neo4jUri`），与夹具的 URI 各算各的 ——
    「夹具连测试实例、被测代码连生产」这种**半隔离比不隔离更危险**（读写分家，
    破坏仍在生产上发生却看不出）。

    必须在任何 app 代码读 config **之前**调用（integration/conftest.py 导入期），
    否则 `getDriver()` 已把生产 driver 缓存进模块级 `_DRIVER`。故这里一并清
    settings 缓存并关闭已缓存的 driver，下次取用会按新 URI 重建。

    与 Milvus 侧 `MILVUS_DB_NAME`（见 app/tests/conftest.py）同款做法，唯一差别：
    那里用 setdefault 兜底，这里**未配置就 fail-fast** —— 因为默认值恰好是生产。
    """
    uri = resolveTestNeo4jUri()  # 未配置/指向生产端口 → 抛错，不做任何副作用
    os.environ["NEO4J_URI"] = uri

    # 延迟导入：让本模块在纯单元测试里不拖入 app.config / neo4j 依赖。
    from app.config import getSettings
    from app.infrastructure import neo4j_client

    getSettings.cache_clear()
    neo4j_client.closeDriver()
    return uri


def assertAppNeo4jIsIsolated(*, appUri: str | None = None) -> None:
    """破坏性清理前的最后一道闸：应用侧当前配置必须就是那个测试实例。

    抓的是**配置漂移**：只设了 `TEST_NEO4J_URI` 却没走 `installTestNeo4jEnv()`，
    或中途把 `NEO4J_URI` 改回生产。此时夹具连测试实例、被测代码连生产，
    清理落在线上 —— 半隔离比不隔离更难发现，所以要求两边**逐字相等**。

    Args:
        appUri: 应用侧 URI。为 None 时取 `getSettings().neo4jUri`。
    """
    if appUri is None:
        from app.config import getSettings

        appUri = getSettings().neo4jUri
    expected = os.environ.get("TEST_NEO4J_URI")
    if appUri != expected:
        raise RuntimeError(
            f"应用侧 Neo4j 配置未指向测试实例：NEO4J_URI={appUri!r}，"
            f"TEST_NEO4J_URI={expected!r}。拒绝执行破坏性清理。{_GUARD_HINT}"
        )

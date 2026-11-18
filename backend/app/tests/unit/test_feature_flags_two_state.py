"""6 个功能开关的**开/关两态**证据（M0.2 / 判据 3）。

## 为什么需要这张「两态表」

判据 3 的要求是「每个开关 **1 开 1 关**」。此前两态证据散落在多个文件里，
而且**不齐**：

- `ENABLE_WORKFLOW`、`ENABLE_AGENT_ORCHESTRATION`：有路由级两态（off ⇒ 404）；
- `ENABLE_SEMANTIC_RELEASE`、`ENABLE_KNOWLEDGE_FACADE`：只有「Settings 解析」
  两态，**没有**「路由真的不注册 / 真的注册」的证据；
- `ENABLE_SEMANTIC_REGISTRY_NL2SQL`、`ENABLE_SEMANTIC_REGISTRY_RAG`：不挂路由，
  只影响服务层行为（两态证据见 `test_semantic_schema_gate.py` /
  `test_semantic_registry_rag_gate.py`）。

⇒ 本文件把 6 个开关注册到**同一张可执行表**上：4 个路由闸门断言
「整组路由注册 / 不注册」，2 个服务闸门断言「闸门读到的开关值」。任何新开关
都要在这里补一行，否则判据 3 就没有证据。

## 为什么断言「路由集合」而不是发 HTTP

闸门的作用点就是 `create_app` 里的 `include_router` 条件（关 = 整组不注册 ⇒
404）。直接在 `app.routes` 上断言，等价且不依赖数据库 / 鉴权 / 限流；HTTP 级
404 证据另有两个既有用例覆盖（`test_workflow_list_api.py` 的 `offClient`、
`test_agent_orchestration_config_api.py::test_disabled_feature_returns_404`）。

⚠️ `getSettings()` 带 `@lru_cache`：改 env 之后必须 `cache_clear()`，否则
读到的仍是上一个用例的设置（2026-11-18 实测过这个假红）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from app.config import getSettings
from app.services import semantic_registry_rag_gate, semantic_schema_gate
from app.tests._testapp import buildTestApp

try:
    # FastAPI >= 0.141：include_router() 的结果被包成 `_IncludedRouter` 包装层，
    # `app.routes` **不再 flat** ⇒ 直接枚举只能看到直接声明的少数路由
    # ⇒ 「开关打开也没看到路由」的空集假绿（2026-11-18 实测踩中）。
    from fastapi.routing import iter_route_contexts
except ImportError:  # pragma: no cover - FastAPI < 0.141
    iter_route_contexts = None

# 展平失效守卫的下限：展平不到路由时必须红。buildTestApp 不挂 MCP，
# 实测展平后 ~300 条 ⇒ 下限留出余量，只拦「枚举成空集 / 只剩个位数」。
_MIN_ENUMERATED_PATHS = 200

# 路由闸门：开关 ⇒ 必须同时存在（开）或同时不存在（关）的路由前缀。
_ROUTE_GATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ENABLE_SEMANTIC_RELEASE", ("/api/v1/semantic/releases",)),
    (
        "ENABLE_KNOWLEDGE_FACADE",
        ("/api/v1/semantic/knowledge", "/api/v1/procurement/knowledge"),
    ),
    ("ENABLE_WORKFLOW", ("/api/v1/workflow",)),
    (
        "ENABLE_AGENT_ORCHESTRATION",
        ("/api/v1/agent-orchestrations", "/api/v1/agent-runs"),
    ),
)

# 服务闸门：不挂路由，开关只改变服务层行为 ⇒ 断言「闸门读到的值」。
_SERVICE_GATES: tuple[tuple[str, Any], ...] = (
    ("ENABLE_SEMANTIC_REGISTRY_NL2SQL", semantic_schema_gate._flagEnabled),
    ("ENABLE_SEMANTIC_REGISTRY_RAG", semantic_registry_rag_gate.registryRagEnabled),
)

_FLAG_NAMES = tuple(flag for flag, _ in _ROUTE_GATES) + tuple(
    flag for flag, _ in _SERVICE_GATES
)


def _unusedFactory() -> None:  # pragma: no cover —— buildTestApp 只存引用，不调用
    raise AssertionError("buildTestApp 不应在建 app 阶段调用会话工厂")


@contextmanager
def _flagEnv(name: str, value: str | None) -> Iterator[None]:
    """临时设置/删除一个开关 env，并在进出两侧都清 Settings 缓存。"""
    previous = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    getSettings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
        getSettings.cache_clear()


def _routePaths() -> set[str]:
    """展平出测试 app 的**全部**最终路径（含 `_IncludedRouter` 内部路由）。"""
    app = buildTestApp(_unusedFactory)
    if iter_route_contexts is not None:
        paths = {context.path for context in iter_route_contexts(app.routes)}
    else:  # pragma: no cover - FastAPI < 0.141
        paths = {
            route.path for route in app.routes if getattr(route, "path", None)
        }
    assert len(paths) >= _MIN_ENUMERATED_PATHS, (
        f"只枚举到 {len(paths)} 条路由 —— 展平失效，基于路径的断言会空集假绿"
    )
    return paths


def _matchesPrefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


# ---------------------------------------------------------------------------
# 路由闸门（4 个）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag, prefixes", _ROUTE_GATES, ids=[f[0] for f in _ROUTE_GATES])
def test_开关关闭时整组路由不注册(flag: str, prefixes: tuple[str, ...]) -> None:
    """关 ⇒ 前缀下**一条路由都没有**（对外 404，不是 403/401）。"""
    with _flagEnv(flag, None):
        paths = _routePaths()

    for prefix in prefixes:
        matched = sorted(p for p in paths if _matchesPrefix(p, prefix))
        assert matched == [], (flag, prefix, matched)


@pytest.mark.parametrize("flag, prefixes", _ROUTE_GATES, ids=[f[0] for f in _ROUTE_GATES])
def test_开关打开时整组路由注册(flag: str, prefixes: tuple[str, ...]) -> None:
    """开 ⇒ 前缀下**至少一条**路由注册（鉴权/限流由各自依赖负责，不在本断言内）。"""
    with _flagEnv(flag, "true"):
        paths = _routePaths()

    for prefix in prefixes:
        matched = sorted(p for p in paths if _matchesPrefix(p, prefix))
        assert matched, (flag, prefix)


# ---------------------------------------------------------------------------
# 服务闸门（2 个）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag, readFlag", _SERVICE_GATES, ids=[f[0] for f in _SERVICE_GATES])
def test_服务闸门随开关两态切换(flag: str, readFlag: Any) -> None:
    """服务层闸门必须**每次**读当前设置（不能在 import 期固化）。"""
    with _flagEnv(flag, None):
        assert readFlag() is False, flag

    with _flagEnv(flag, "true"):
        assert readFlag() is True, flag


# ---------------------------------------------------------------------------
# 开关清单守卫：漏登记一个 = 判据 3 没证据
# ---------------------------------------------------------------------------


def test_开关清单与config一致() -> None:
    """`config.py` 里每个 `ENABLE_*` 别名都必须登记到本文件的两态表里。

    为什么：新增开关时最容易漏的就是「证据」—— 开关加了、判据表没加，
    判据 3 会静默少一项。
    """
    from app.config import Settings

    declared = {
        field.alias
        for field in Settings.model_fields.values()
        if isinstance(field.alias, str) and field.alias.startswith("ENABLE_")
    }

    assert declared == set(_FLAG_NAMES), (
        f"config.py 的 ENABLE_* 与两态表不一致："
        f"仅 config 有 {sorted(declared - set(_FLAG_NAMES))}；"
        f"仅表里有 {sorted(set(_FLAG_NAMES) - declared)}"
    )

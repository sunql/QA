"""feature-scoped 路由门禁守卫的共享路由树工具。

本仓的特性路由同时由 `main.py` 与 `tests/_testapp.py` 手工装配，API 集成测试只走
后者 ⇒ 生产装配漏门禁时集成套件照样全绿。守卫必须直接构建生产 app
（`app.main.createApp()`）并检查依赖链，因此需要一份能穿透包装层的路由展开器。

⚠️ **版本敏感**：FastAPI ≥0.141 用 `_IncludedRouter` 包装 `include_router()`，
把 include 时声明的 `dependencies=[...]` 放进 `include_context.dependencies`，
**不并入** `route.dependant`（0.136.x 会并入）。只沿 dependant 探测会把声明在
`include_router(dependencies=...)` 上的门禁全部判成「不存在」⇒ 假绿。

目前由以下守卫共用：
- `tests/integration/test_semantic_release_route_auth.py`
- `tests/integration/test_agent_orchestration_route_auth.py`
- `tests/integration/test_governance_write_route_auth.py`
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Any

from fastapi.params import Depends as DependsParam
from fastapi.routing import APIRoute
from starlette.routing import Route as StarletteRoute


def iterRoutesWithDeps(
    routes: list[Any], prefix: str = "", inherited: tuple[Any, ...] = ()
) -> Iterator[tuple[str, APIRoute, tuple[Any, ...]]]:
    """递归展开路由树，产出 (完整路径, APIRoute, include 时声明的依赖)。

    包装层下钻顺序：`original_router` + `include_context` → 普通嵌套 `routes`
    → 普通 Starlette 路由（无依赖树，跳过）。未知包装类型 fail-closed —— 否则
    FastAPI 再换一次包装形态时，枚举器会悄悄少枚举路由（空集假绿）。
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route, inherited
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            includeContext = getattr(route, "include_context", None)
            nestedPrefix = prefix + (getattr(includeContext, "prefix", "") or "")
            nestedDeps = inherited + tuple(
                getattr(dependency, "dependency", None)
                for dependency in (getattr(includeContext, "dependencies", ()) or ())
            )
            yield from iterRoutesWithDeps(
                list(original.routes), nestedPrefix, nestedDeps
            )
            continue
        nested = getattr(route, "routes", None)
        if isinstance(nested, list):
            nestedPrefix = prefix + (
                getattr(route, "prefix", "") or getattr(route, "path", "") or ""
            )
            yield from iterRoutesWithDeps(nested, nestedPrefix, inherited)
            continue
        if not isinstance(route, StarletteRoute):
            raise AssertionError(f"未知的路由包装类型：{type(route)!r}")


def declaredDependencies(call: Any) -> Iterator[Any]:
    """展开一个依赖 callable 自身签名里声明的 `Depends(...)`（递归）。

    `include_context.dependencies` 里只有 `Depends(requireAdmin)` 这一层；
    `getCurrentUser` 是 `requireAdmin` 签名里的参数依赖。静态检查必须沿
    `inspect.signature` 往下走，否则「间接引入」的那一半门禁在 0.141 下不可见。

    ⚠️ 只识别**默认参数**写法（`p: X = Depends(d)`）；`Annotated[X, Depends(d)]`
    写法不在覆盖范围内（当前 `app/` 全仓无此写法）。
    """
    seen: set[int] = set()
    stack = [call]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        try:
            signature = inspect.signature(current)
        except (TypeError, ValueError):
            continue
        for parameter in signature.parameters.values():
            nested = parameter.default
            # `fastapi.Depends` 是工厂函数而不是类（各版本均如此）；类型判断必须用
            # `fastapi.params.Depends`，否则 `isinstance(x, Depends)` 会 TypeError。
            if isinstance(nested, DependsParam):
                stack.append(nested.dependency)


def declaresDependency(
    route: APIRoute, dependency: object, inherited: tuple[Any, ...] = ()
) -> bool:
    """路由依赖链（含 include 时 / router 级 / 间接 dependencies）是否声明了指定依赖。"""
    if any(
        call is dependency for root in inherited for call in declaredDependencies(root)
    ):
        return True
    seen: set[int] = set()
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        if id(dependant) in seen:
            continue
        seen.add(id(dependant))
        if dependant.call is dependency:
            return True
        stack.extend(dependant.dependencies)
    return False

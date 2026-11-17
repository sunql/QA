"""真实 app（`app.main.createApp()`）的装配守卫。

**为什么需要单独一个文件**：集成测试默认走 `app.tests._testapp.buildTestApp`
—— 那是与 `app/main.py` **平行手工维护**的另一套 wiring。它对 `main.py` 的
装配缺陷**结构性不可见**：`_testapp` **根本不挂 MCP**（无任何 `Mount`），
health 没有任何 catch-all 遮蔽，因此在它上面无论把 health 写在哪里都恒为可达；
而真实 app 里 MCP 的 `Mount("")` 就在 health 之前，health 一旦写在 mount 之后
就永久 404。两边跑出来的结论完全相反。本文件的作用就是补上这一处
「跑真实 app」的视角。

（同类教训见 feat-in-app-message：4 根线缺一，测试全绿而端点 404。）
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.routing import Mount

try:
    # FastAPI >= 0.141：include_router() 的结果被包成 `_IncludedRouter` 包装层，
    # app.routes 不再是 flat 的 APIRoute 列表 ⇒ 必须靠公开的 iter_route_contexts 展平。
    # 0.136.x 没有该符号 ⇒ 走下面的 flat 分支。
    from fastapi.routing import iter_route_contexts
except ImportError:  # pragma: no cover - FastAPI < 0.141
    iter_route_contexts = None

# 展开失效守卫的下限：展平不到路由时必须红，而不是「枚举到空集 ⇒ 断言通过」。
_MIN_ENUMERATED_PATHS = 300


def _realApp():
    """构建真实应用实例（`app.main` 的模块级 app 同源）。"""
    from app.main import createApp

    return createApp()


def _routePath(route: object) -> str | None:
    return getattr(route, "path", None)


def _enumeratePaths(app: object) -> list[str]:
    """展平出应用**全部**路由的最终路径。

    ⚠️ 不能直接读 `app.router.routes`：FastAPI 0.141 把 include_router 的结果包成
    `_IncludedRouter`（自身没有 path 属性）⇒ 直接枚举只能看到 app 上直接声明的
    少数路由，任何基于它的「路径存在性」断言都会**空集假绿**（开关打开也报
    「路由不齐备」）。公开 API `iter_route_contexts()` 给出展平后的最终路径。
    """
    if iter_route_contexts is not None:
        return [ctx.path for ctx in iter_route_contexts(app.routes)]
    return [r.path for r in app.routes if getattr(r, "path", None)]


class TestRealAppRouteTable:
    """路由表结构不变量：catch-all Mount 必须是最后一条路由。"""

    def test_no_route_registered_after_catch_all_mount(self) -> None:
        """catch-all Mount（path=""，MCP streamable-http 挂在这里）之后不得再有路由。

        Starlette 按**注册顺序**匹配：`Mount("")` 对任何路径前缀都命中，因此它之后
        注册的路由永远不会被匹配到（被静默吞掉，表现为 404）。`/api/v1/health` 就曾
        因为被写在 MCP mount 之后而整条不可达。

        本断言把「MCP mount 必须是路由表最后一条」钉成不变量：任何新路由若加在
        mount 之后，这里立刻变红，而不是等到线上探测 404 才发现。
        """
        routes = _realApp().router.routes

        mountIdx = next(
            (
                i
                for i, r in enumerate(routes)
                if isinstance(r, Mount) and (r.path or "") == ""
            ),
            None,
        )
        assert mountIdx is not None, (
            "未在真实 app 路由表中找到 catch-all Mount（MCP 挂载点）—— "
            "装配方式已变，请同步更新本守卫"
        )

        shadowed = routes[mountIdx + 1 :]
        assert not shadowed, (
            f"catch-all Mount（第 {mountIdx} 条）之后仍有 {len(shadowed)} 条路由，"
            f"它们会被永久遮蔽（请求一律落到 MCP 子 app）："
            f"{[_routePath(r) for r in shadowed]}"
        )


class TestRealAppWorkflowRouteGating:
    """P5 工作流路由的**开关门控**守卫（真实 app）。

    契约：``ENABLE_WORKFLOW`` 默认 **false** ⇒ 整组路由**不注册**（对外 404）
    ⇒ 「没开 = 没有这个面」。开关打开 ⇒ 5 条路由都在。

    ⚠️ 本文件是**既有** integration 文件（D-12：不得新建 integration 文件，
    否则分片按「排序下标 mod 4」轮转 ⇒ 假 net-new）。
    """

    @staticmethod
    def _workflowPaths(app: object) -> list[str]:
        return sorted(path for path in _enumeratePaths(app) if path.startswith("/api/v1/workflow"))

    def test_开关关闭时路由不注册(self) -> None:
        import os

        from app.config import getSettings

        previous = os.environ.get("ENABLE_WORKFLOW")
        os.environ.pop("ENABLE_WORKFLOW", None)
        getSettings.cache_clear()
        try:
            realApp = _realApp()
            paths = self._workflowPaths(realApp)
            allPaths = _enumeratePaths(realApp)
        finally:
            if previous is None:
                os.environ.pop("ENABLE_WORKFLOW", None)
            else:
                os.environ["ENABLE_WORKFLOW"] = previous
            getSettings.cache_clear()

        # 展开失效守卫：枚举不到路由时本断言会「空集假绿」⇒ 先卡下限。
        assert len(allPaths) >= _MIN_ENUMERATED_PATHS, (
            f"仅枚举到 {len(allPaths)} 条路由（下限 {_MIN_ENUMERATED_PATHS}）—— "
            "app.routes 结构可能已变（FastAPI include_router 行为），本守卫会空集假绿"
        )

        assert paths == [], (
            f"ENABLE_WORKFLOW 关闭时真实 app 仍注册了工作流路由：{paths} —— "
            "默认关闭是 P5 的上线前提（回滚 = 关开关 + 停 Worker）"
        )

    def test_开关打开时路由齐备(self) -> None:
        import os

        from app.config import getSettings

        previous = os.environ.get("ENABLE_WORKFLOW")
        os.environ["ENABLE_WORKFLOW"] = "1"
        getSettings.cache_clear()
        try:
            paths = self._workflowPaths(_realApp())
        finally:
            if previous is None:
                os.environ.pop("ENABLE_WORKFLOW", None)
            else:
                os.environ["ENABLE_WORKFLOW"] = previous
            getSettings.cache_clear()

        assert paths == [
            "/api/v1/workflow/approvals/{taskId}/decision",
            "/api/v1/workflow/instances",
            "/api/v1/workflow/instances/{instanceId}",
            "/api/v1/workflow/instances/{instanceId}/approval-request",
            "/api/v1/workflow/instances/{instanceId}/approvals",
            "/api/v1/workflow/instances/{instanceId}/cancel",
        ], f"工作流路由不齐备：{paths}"


class TestRealAppHealthEndpoint:
    """真实 app 的 HTTP 行为：health 必须可达。"""

    @pytest.mark.asyncio
    async def test_health_endpoint_reachable(self, client: AsyncClient) -> None:
        """GET /api/v1/health → 200 {"status": "ok"}。

        `client` fixture 仅用于让本测试跑在与其余集成测试相同的真实 PG 环境下
        （它同时替换全局会话工厂）；**请求本身发向真实 app**，验证 `main.py` 的
        装配结果而不是 `_testapp` 的。health 当前无 DB 依赖，故这条断言不经过
        `client` 的会话链路 —— 若日后 health 引入 DB 依赖，本测试需改为复用该 client。
        """
        transport = ASGITransport(app=_realApp())
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/health")

        assert resp.status_code == 200, (
            f"/api/v1/health 在真实 app 上不可达（{resp.status_code}）—— "
            f"响应体：{resp.text[:200]}"
        )
        body = resp.json()
        assert body["status"] == "ok"

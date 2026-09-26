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


def _realApp():
    """构建真实应用实例（`app.main` 的模块级 app 同源）。"""
    from app.main import createApp

    return createApp()


def _routePath(route: object) -> str | None:
    return getattr(route, "path", None)


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

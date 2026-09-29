# 路由鉴权收口 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让全应用 46 条匿名可达路由（含 15 条写/删）要求鉴权，并用结构性守卫测试防止复发。

**Architecture:** 在 9 个 `APIRouter` 上加 router 级 `dependencies=[Depends(getCurrentUser)]`（一行/路由，未来新增端点自动继承）。守卫测试展开 `app.routes` 里的 `_IncludedRouter` 包装对象、走依赖链，断言「未鉴权路由集合 ⊆ 白名单」。

**Tech Stack:** FastAPI 0.141 / Starlette / pytest + httpx AsyncClient / 真实 PostgreSQL

## Global Constraints

- 测试环境：`TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test'`（专用容器 `qa-pg-a1`，库名必须是 `qa_metadata_test`）
- **单元测试与集成测试必须分进程跑**（`TRUNCATE` 会抹掉 `ontology_class` 等共享表）
- 不修改 `raw/`；业务查询仅只读 SELECT；不可变数据；显式错误处理
- 函数 < 50 行；文件 < 800 行；嵌套 ≤ 4 层
- 本批次**只做鉴权**：不改 `DataLineage.owner` 取值来源、不引入全局中间件、不补速率限制、不动 `/share/{token}`

---

### Task 1: 路由鉴权收口 + 结构性守卫测试

**Files:**
- Create: `backend/app/tests/integration/test_route_auth_guard.py`
- Modify: `backend/app/api/v1/data_lineage.py`（import L17、router L28）
- Modify: `backend/app/api/v1/data_quality.py`（router L45、`scores_router` L46）
- Modify: `backend/app/api/v1/data_quality_rule_params.py`（import、router L16）
- Modify: `backend/app/api/v1/term_dictionary.py`（router L18）
- Modify: `backend/app/api/v1/ontology.py`（router L65）
- Modify: `backend/app/api/v1/graph.py`（import、router L15）
- Modify: `backend/app/api/v1/vectors.py`（import、router L14）
- Modify: `backend/app/api/v1/wiki_compile.py`（router L23）

**Interfaces:**
- Consumes: `app.dependencies.getCurrentUser`（已有，签名 `(authorization, xUserId, xTenantId, xUserRoles, xUserDepartments, session) -> CurrentUser`）；`app.main.app`
- Produces: 无新公开接口。守卫测试的三个测试函数名即验收锚点

**背景事实（实施前必读，别重新推导）：**
- `app.routes` 含 49 个 `fastapi.routing._IncludedRouter`，其 `path=None` 且 `routes`/`app`/`router` 属性 `hasattr=False`；朴素 `isinstance(r, APIRoute)` **只能枚举到 1 条**。必须经 `__dict__` 的 `original_router` 与 `include_context`（含 `prefix` / `dependencies`）展开
- 展开后应枚举到 **389 条 APIRoute**、**46 条非白名单未鉴权路由**
- 各文件 `getCurrentUser` import 现状（已核对）：`data_quality.py:26` / `term_dictionary.py:14` / `ontology.py:26` / `wiki_compile.py:7` **已有**；`data_lineage.py:17` 只有 `getDb`；`data_quality_rule_params.py` 从 `app.infrastructure.database` 导 `getDb`（**不是** `app.dependencies`）；`graph.py` / `vectors.py` **连 `Depends` 都没导入**

---

- [ ] **Step 1: 写守卫测试（RED）**

创建 `backend/app/tests/integration/test_route_auth_guard.py`：

```python
"""路由鉴权守卫：全应用未鉴权路由集合必须 ⊆ 白名单。

背景：2026-09-30 发现 46 条非公开路由在 AUTH_MODE=real 下匿名可达（含 15 条写/删）。
根因不是「规则缺失」而是「规则无强制」——本测试即强制。
"""
from fastapi.routing import APIRoute, _IncludedRouter

from app.dependencies import getCurrentUser
from app.main import app

# 应公开的路由（键与 (method, APIRoute.path) 同形）
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/v1/health"),
    ("POST", "/api/v1/auth/login"),
    ("GET", "/api/v1/auth/password-policy"),
    ("GET", "/api/v1/data-quality/reports/share/{token}"),
})

# 枚举下限：展开失效时必须红，而不是空集通过（vacuous pass）
MIN_ENUMERATED_ROUTES = 300

_IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})


def _hasCurrentUser(route: APIRoute, extraDependencies: tuple = ()) -> bool:
    """路由依赖链（含 router 级 dependencies）是否含 getCurrentUser。"""
    for dependency in extraDependencies:
        if getattr(dependency, "dependency", None) is getCurrentUser:
            return True
    seen: set[int] = set()
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        if id(dependant) in seen:
            continue
        seen.add(id(dependant))
        if dependant.call is getCurrentUser:
            return True
        stack.extend(dependant.dependencies)
    return False


def _enumerateRoutes() -> list[tuple[str, str, bool]]:
    """枚举 (method, path, hasAuth)。必须展开 _IncludedRouter。

    FastAPI 0.141 的 app.routes 里 _IncludedRouter 的 path 为 None 且
    routes/app 属性不可见，只能经 original_router + include_context 展开。
    """
    found: list[tuple[str, str, bool]] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods - _IGNORED_METHODS:
                found.append((method, route.path, _hasCurrentUser(route)))
        elif isinstance(route, _IncludedRouter):
            context = route.include_context
            prefix = context.prefix or ""
            for sub in route.original_router.routes:
                if isinstance(sub, APIRoute):
                    for method in sub.methods - _IGNORED_METHODS:
                        found.append((
                            method,
                            prefix + sub.path,
                            _hasCurrentUser(sub, context.dependencies),
                        ))
    return found


def test_enumeratedRouteCountMeetsFloor() -> None:
    """展开失效守卫：内部结构变化导致展开不到路由时，本测试必须红。"""
    routes = _enumerateRoutes()
    assert len(routes) >= MIN_ENUMERATED_ROUTES, (
        f"仅枚举到 {len(routes)} 条路由（下限 {MIN_ENUMERATED_ROUTES}）——"
        "_IncludedRouter 展开很可能已失效，鉴权守卫会空集通过"
    )


def test_noUnauthenticatedRouteOutsideWhitelist() -> None:
    """坏输入被拦：非白名单路由必须全部要求鉴权。"""
    offenders = sorted({
        (method, path)
        for method, path, hasAuth in _enumerateRoutes()
        if not hasAuth and (method, path) not in PUBLIC_ROUTES
    })
    assert offenders == [], (
        "以下路由匿名可达（缺 Depends(getCurrentUser)）：\n  "
        + "\n  ".join(f"{m:7} {p}" for m, p in offenders)
    )


def test_whitelistRoutesAreActuallyPublic() -> None:
    """正确输入不被拦：白名单路由不应被误加鉴权（否则登录/对外分享会断）。"""
    byKey = {(m, p): a for m, p, a in _enumerateRoutes()}
    for key in sorted(PUBLIC_ROUTES):
        assert key in byKey, f"白名单路由 {key} 未枚举到——白名单已陈旧"
        assert byKey[key] is False, (
            f"白名单路由 {key} 现在带了鉴权，请确认它是否仍应公开"
        )
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `uv run pytest app/tests/integration/test_route_auth_guard.py -v`
Expected: `test_noUnauthenticatedRouteOutsideWhitelist` **FAIL**，报错文本列出 46 条路由；另两个 PASS。
**把失败输出里的 46 条与 `summary.md` 第三节逐条对账**——数量或条目不符就先查清楚再往下走。

- [ ] **Step 3: 给 9 个 router 补鉴权**

`backend/app/api/v1/data_lineage.py`：
- L17 `from app.dependencies import getDb` → `from app.dependencies import getCurrentUser, getDb`
- L28 `router = APIRouter(dependencies=[])` → `router = APIRouter(dependencies=[Depends(getCurrentUser)])`

`backend/app/api/v1/data_quality.py`：
- L45 `router = APIRouter(dependencies=[])` → `router = APIRouter(dependencies=[Depends(getCurrentUser)])`
- L46 `scores_router = APIRouter(dependencies=[])` → `scores_router = APIRouter(dependencies=[Depends(getCurrentUser)])`
- （import 已在 L26）

`backend/app/api/v1/data_quality_rule_params.py`：
- 新增 `from app.dependencies import getCurrentUser`（放在 `from app.infrastructure.database import getDb` 之后）
- L16 `router = APIRouter(prefix="/dq-rule-params/rules", tags=["dq-rule-params"])` → 追加 `, dependencies=[Depends(getCurrentUser)]`
- （`Depends` 已在 L4 从 fastapi 导入）

`backend/app/api/v1/term_dictionary.py`：
- L18 → 追加 `dependencies=[Depends(getCurrentUser)]`（import 已在 L14）

`backend/app/api/v1/ontology.py`：
- L65 → 追加 `dependencies=[Depends(getCurrentUser)]`（import 已在 L26）

`backend/app/api/v1/graph.py`：
- L11 `from fastapi import APIRouter, HTTPException, Query` → 追加 `, Depends`
- 新增 `from app.dependencies import getCurrentUser`
- L15 `router = APIRouter(tags=["system"])` → `router = APIRouter(tags=["system"], dependencies=[Depends(getCurrentUser)])`

`backend/app/api/v1/vectors.py`：
- L11 `from fastapi import APIRouter, HTTPException, Query` → 追加 `, Depends`
- 新增 `from app.dependencies import getCurrentUser`
- L14 `router = APIRouter(tags=["system"])` → `router = APIRouter(tags=["system"], dependencies=[Depends(getCurrentUser)])`

`backend/app/api/v1/wiki_compile.py`：
- L23 → 追加 `dependencies=[Depends(getCurrentUser)]`（import 已在 L7）

**不要动** `auth.py` 与 `evaluation_report.py`。

- [ ] **Step 4: 跑守卫测试确认 GREEN**

Run: `uv run pytest app/tests/integration/test_route_auth_guard.py -v`
Expected: 3 passed。

- [ ] **Step 5: 证明守卫不是假守卫（下限断言真的会红）**

临时把 `MIN_ENUMERATED_ROUTES` 改成 `10000`，跑 `test_enumeratedRouteCountMeetsFloor`，
确认它 **FAIL**；再改回 `300`，确认 PASS。**这一步必须做**——它证明「展开失效 ⇒ 守卫红」
这条链路真的通，而不是我的一厢情愿。把两次输出记进报告。

- [ ] **Step 6: 跑受影响的既有测试，确认没打红**

Run（分进程，逐个文件跑，避免 TRUNCATE 互踩）:
```
uv run pytest app/tests/integration/test_ontology_join_api.py app/tests/integration/test_data_quality_score_api.py app/tests/integration/test_data_quality_rule_params_api.py -v
uv run pytest app/tests/integration/test_lineage_extract_api.py app/tests/integration/test_term_dictionary_inject.py app/tests/integration/test_graph_traversal_api.py -v
```
Expected: 全绿。若有红，先判断是不是本改动引入的（`git stash` 对照），把结论写进报告。

- [ ] **Step 7: 提交**

```bash
git add backend/app/api/v1/data_lineage.py backend/app/api/v1/data_quality.py \
        backend/app/api/v1/data_quality_rule_params.py backend/app/api/v1/term_dictionary.py \
        backend/app/api/v1/ontology.py backend/app/api/v1/graph.py \
        backend/app/api/v1/vectors.py backend/app/api/v1/wiki_compile.py \
        backend/app/tests/integration/test_route_auth_guard.py
git commit -m "fix(security): 46 条匿名可达路由补 router 级鉴权 + 结构性守卫测试"
```

---

## 批次外步骤（不属 SDD 任务循环）

**部署**：容器与工作树漂移 5 个文件，其中两处是生产安全缺口——
`PATCH /api/v1/wiki/compile/claims/{claimId}` 在生产仍匿名、
`evidences.py` 缺 R2 按会话归属守卫（跨用户 evidence 枚举）。
**改源码是必要条件不是充分条件**，须部署后复测 46 条路由返回 401/403。
部署涉及对外可见动作，**执行前需用户确认**。

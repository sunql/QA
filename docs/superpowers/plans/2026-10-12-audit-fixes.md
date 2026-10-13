# Audit Fixes 2026-10-12 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `docs/QA-1012-01.md` 全部 11 项发现（4 HIGH / 6 MEDIUM / 1 LOW）+ 4 项维护观察；新增/补足单元 + 集成测试覆盖；维护 19 个已有后端集成测试全绿。

**Architecture:**
- 5 phase 串行；每 phase 一个或多个 commit；commit message 含 `[audit-fx-1.1]` 等前缀便于 trace
- 改动范围：`backend/app/api/v1/`、`backend/app/services/`、`backend/app/infrastructure/`、`backend/app/dependencies.py`、`backend/app/main.py`、`backend/app/config.py` + 前端 `frontend/src/api/client.ts` / `LoginPage.tsx` / `RequireAuth.tsx`
- **不**触及 chat 侧、R3 报告侧、ontology、milvus schema（除非 Phase 4 显式需要 l3_acl 表）
- 数据隔离：值字典/角色表等不引入 alembic 迁移的放代码内 dict/literal；acl / 角色字段走 alembic

**Tech Stack:** FastAPI · SQLAlchemy 2.0 · Pydantic v2 · pytest + pytest-asyncio · alembic · React 18 + Vite + antd + axios

---

## Global Constraints（强制适用所有 task）

引自 `wiki/aicode/qa-system/CLAUDE.md` + 审计报告，按字面执行：

- **TDD 强制**：每个 task 必须先写失败测试 → 跑红 → 写最小实现 → 跑绿 → commit
- **真实 DB**：测试用 PostgreSQL 5434 端口 `qa_metadata_test`；Neo4j 测试 7688 ≠ 生产 7687
- **pytest 命令**：`python -m pytest`，**禁** `uv run pytest`
- **环境变量**：每个 pytest 调用前必须 `TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688`
- **不可变性**：不修改 in-place 数据；返回新对象
- **SQL 守卫**：所有外部 SQL 走 SQL Guard；生产代码不用 sqlite
- **文件大小**：200-400 行典型，800 上限；函数 < 50 行；嵌套 ≤ 4
- **命名**：变量/函数 camelCase；类 PascalCase；常量 UPPER_SNAKE_CASE；ORM/Pydantic 字段 snake_case
- **DB 凭据**：`qa_user` / `qa_pg_dev_2026`（**禁**硬编码到代码）
- **认证**：`AUTH_MODE=real`；真实 Bearer JWT；admin 账号 `admin` / `Admin@123`
- **混跑陷阱**：unit + integration + services **不**同进程跑；每个进程前手动 truncate
- **前端 deploy**：`npm run build` 产物才进镜像；改 .tsx/.ts 不会自动反映到运行 bundle
- **chat 侧字节冻结**：本 plan **不**动 chat_service / chat_multistep / TermDictionary
- **R3 报告侧不动**：除非 Finding 9 显式需要；不触 `report_planner.py` / `chart_*` / `display_name_resolver.py`
- **不**修改 `raw/` 目录
- **不**对 prod 库跑 alembic；alembic 只对 `qa_metadata_test` (5434) + dev (5433)
- **commit 不 push** 除非用户显式要求
- **worktree 工作**：所有命令在 worktree 内执行；`git stash` 需带 `-u -m "<tag>"` + 捕获 SHA + `apply <sha>`，禁裸 stash

---

## Branching & Worktree

```bash
# 在共享 checkout 执行一次（用户手跑，因为 session 已被 worktree 锁）
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git fetch origin
git worktree add .worktrees/fix-audit-2026-10-12 fabd487ded3b7721f645a8c48250936dd18ff638
cd .worktrees/fix-audit-2026-10-12
git switch -c fix/audit-2026-10-12
```

- **基线**：`fabd487ded3b7721f645a8c48250936dd18ff638`（审计 commit）
- **新分支**：`fix/audit-2026-10-12`
- **worktree 路径**：`wiki/aicode/qa-system/.worktrees/fix-audit-2026-10-12/`
- **commit 数量**：15-17 个（一 task 一个 commit；Phase 4 多任务）

---

## Test Strategy（继承自审计"测试与维护观察"）

每 task **必须**新增至少 1 个测试（RED→GREEN）。测试分布目标：

| 类别 | 当前 | 计划 | 说明 |
|---|---|---|---|
| 集成测试（auth/model/document） | 19 | +6 | 覆盖 RBAC / mustChangePassword / 401 vs 403 / bcrypt 422 / 限流 / 密级 |
| 单元测试（model_config / cache / rate_limit / upload） | n/a | +8 | 服务级单测 |
| 前端拦截器单测 | 2（临时） | +3 | 401 清 token / 403 保留 / AppError 统一结构 |

**反例必须双向**：每个正向测试配一个反例（未授权→403、过期→401、超长→422、跨用户→拒绝）。

---

## Phase 1：身份与权限门禁（3 HIGH 必做）

### Task 1.1：模型配置 RBAC + endpoint URL 校验（Finding 1）

**Files:**
- Modify: `backend/app/api/v1/model_config.py`（POST/PUT/DELETE 加 `require_admin`）
- Modify: `backend/app/api/v1/dependencies.py`（新增 `require_admin` 工厂）
- Modify: `backend/app/services/model_config_service.py`（endpoint 变更时强制要求 key）
- Create: `backend/app/infrastructure/url_validator.py`（内网/loopback 阻断）
- Modify: `backend/app/infrastructure/llm/factory.py`（缓存键 + version 校验）
- Modify: `backend/app/infrastructure/llm/openai_client.py`（key 重新绑定）
- Test: `backend/app/tests/integration/test_model_config_rbac.py`（新）
- Test: `backend/app/tests/unit/test_url_validator.py`（新）

**RED — 写失败测试** `backend/app/tests/unit/test_url_validator.py`：

```python
import pytest
from app.infrastructure.url_validator import validateEndpointUrl

class TestValidateEndpointUrl:
    def test_https_production_allowed(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        assert validateEndpointUrl("https://api.openai.com/v1") is None

    def test_http_production_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(ValueError, match="必须为 https"):
            validateEndpointUrl("http://api.openai.com/v1")

    def test_private_ip_production_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(ValueError, match="私有地址"):
            validateEndpointUrl("https://10.0.0.1/v1")

    def test_loopback_production_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(ValueError, match="私有地址"):
            validateEndpointUrl("https://127.0.0.1/v1")

    def test_localhost_dev_allowed(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "development")
        assert validateEndpointUrl("http://localhost:1234/v1") is None
```

**Step 2 跑红**：
```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  python -m pytest app/tests/unit/test_url_validator.py -v
```
预期：5 个失败（模块未导入）。

**GREEN — 最小实现** `backend/app/infrastructure/url_validator.py`：

```python
"""端点 URL 校验：生产环境阻断 http + 私有 IP/loopback。"""
from __future__ import annotations
import ipaddress
import os
from urllib.parse import urlparse

def validateEndpointUrl(url: str) -> str | None:
    """返回 None 表示通过；返回 str 表示错误信息。"""
    parsed = urlparse(url)
    env = os.getenv("APP_ENV", "development")
    if env == "production":
        if parsed.scheme != "https":
            return "生产环境 endpoint 必须为 https"
        host = parsed.hostname or ""
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return f"生产环境 endpoint 禁止指向私有地址 {host}"
        except ValueError:
            pass
    return None
```

**Step 4 跑绿**：5 个全过。

**RED — RBAC 集成测试** `backend/app/tests/integration/test_model_config_rbac.py`：

```python
import pytest
from httpx import AsyncClient
from app.main import createApp

@pytest.fixture
async def client(dbSession):
    app = createApp()
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac

@pytest.mark.asyncio
async def test_unprivileged_user_cannot_create_model(client, regularUserToken):
    headers = {"Authorization": f"Bearer {regularUserToken}"}
    resp = await client.post("/api/v1/model-configs", json={
        "provider": "openai", "name": "test", "modelId": "gpt-4o",
        "apiEndpoint": "https://api.openai.com/v1", "apiKey": "sk-test"
    }, headers=headers)
    assert resp.status_code == 403

@pytest.mark.asyncio
async def test_admin_can_create_model(client, adminToken):
    headers = {"Authorization": f"Bearer {adminToken}"}
    resp = await client.post("/api/v1/model-configs", json={
        "provider": "openai", "name": "test2", "modelId": "gpt-4o",
        "apiEndpoint": "https://api.openai.com/v1", "apiKey": "sk-test"
    }, headers=headers)
    assert resp.status_code == 201

@pytest.mark.asyncio
async def test_endpoint_change_requires_new_key(client, adminToken, existingModelConfig):
    headers = {"Authorization": f"Bearer {adminToken}"}
    resp = await client.put(f"/api/v1/model-configs/{existingModelConfig.id}", json={
        "apiEndpoint": "https://api.anthropic.com/v1"
        # 故意不带 apiKey
    }, headers=headers)
    assert resp.status_code == 422
    assert "apiKey" in resp.json()["detail"]
```

**Step 6 跑红**（3 个失败：未启用 RBAC、endpoint 变更未校验 key）。

**GREEN — 改 `model_config.py`**：
- POST/PUT/DELETE 顶部 `dependencies: list = [Depends(require_admin)]`
- service `updateConfig` 接收新 key 才能修改 endpoint
- 启动时 import `validateEndpointUrl`，route 层在写入前调用，失败 422

**Step 8 跑绿**：3 个测试通过。

**Commit**：
```bash
git add backend/app/api/v1/model_config.py \
        backend/app/api/v1/dependencies.py \
        backend/app/services/model_config_service.py \
        backend/app/infrastructure/url_validator.py \
        backend/app/infrastructure/llm/factory.py \
        backend/app/infrastructure/llm/openai_client.py \
        backend/app/tests/unit/test_url_validator.py \
        backend/app/tests/integration/test_model_config_rbac.py
git commit -m "[audit-fx-1.1] model config: require admin role + endpoint URL validation"
```

---

### Task 1.2：生产启动门禁（Finding 2）

**Files:**
- Modify: `backend/app/config.py`（新增 `validate_production()`）
- Modify: `backend/app/main.py:171`（lifespan 启动时调用）
- Test: `backend/app/tests/integration/test_startup_gate.py`（新）

**RED** — `test_startup_gate.py`：

```python
import pytest
from app.config import validateProduction, ProductionConfigError

class TestValidateProduction:
    def test_stub_mode_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AUTH_MODE", "stub")
        with pytest.raises(ProductionConfigError, match="AUTH_MODE"):
            validateProduction()

    def test_stub_enabled_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AUTH_MODE", "real")
        monkeypatch.setenv("AUTH_STUB_ENABLED", "true")
        with pytest.raises(ProductionConfigError, match="STUB"):
            validateProduction()

    def test_default_jwt_secret_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AUTH_MODE", "real")
        monkeypatch.setenv("JWT_SECRET", "dev-secret-change-me")
        with pytest.raises(ProductionConfigError, match="JWT"):
            validateProduction()

    def test_missing_fernet_key_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AUTH_MODE", "real")
        monkeypatch.setenv("JWT_SECRET", "x" * 32)
        monkeypatch.delenv("FERNET_KEY", raising=False)
        with pytest.raises(ProductionConfigError, match="FERNET"):
            validateProduction()

    def test_valid_production_passes(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AUTH_MODE", "real")
        monkeypatch.setenv("AUTH_STUB_ENABLED", "false")
        monkeypatch.setenv("JWT_SECRET", "x" * 32)
        monkeypatch.setenv("FERNET_KEY", "x" * 44)
        assert validateProduction() is None
```

**Step 2 跑红**：`ProductionConfigError` 未定义。

**GREEN** — `backend/app/config.py` 新增：

```python
class ProductionConfigError(RuntimeError):
    """生产环境配置校验失败 — 启动必须终止。"""

def validateProduction() -> None:
    env = os.getenv("APP_ENV", "development")
    if env != "production":
        return
    errors = []
    if os.getenv("AUTH_MODE") != "real":
        errors.append("AUTH_MODE must be 'real' in production")
    if os.getenv("AUTH_STUB_ENABLED", "false").lower() == "true":
        errors.append("AUTH_STUB_ENABLED must be false in production")
    jwt = os.getenv("JWT_SECRET", "")
    if jwt in ("", "dev-secret-change-me", "change-me-in-production"):
        errors.append("JWT_SECRET must be set to a non-default value")
    if not os.getenv("FERNET_KEY"):
        errors.append("FERNET_KEY must be set in production")
    if errors:
        raise ProductionConfigError("; ".join(errors))
```

**`main.py`** lifespan 顶部：
```python
from app.config import validateProduction
@app.on_event("startup")
async def _validateStartup():
    validateProduction()
```

**Step 4 跑绿**：5 个全过。

**Commit**：
```bash
git commit -am "[audit-fx-1.2] enforce production startup config gate"
```

---

### Task 1.3：must_change_password 服务端强制（Finding 3）

**Files:**
- Modify: `backend/app/dependencies.py:202`（新增 `mustChangePasswordGuard`）
- Modify: `backend/app/api/v1/auth.py`（在 `/auth/me` `/auth/change-password` `/auth/logout` 路由放行）
- Modify: `frontend/src/components/common/RequireAuth.tsx`（路由级守卫）
- Test: `backend/app/tests/integration/test_must_change_password.py`（新）
- Test: `frontend/src/components/common/__tests__/RequireAuth.test.tsx`（新）

**RED** — `test_must_change_password.py`：

```python
@pytest.mark.asyncio
async def test_must_change_user_blocked_from_business(client, mustChangeUserToken):
    headers = {"Authorization": f"Bearer {mustChangeUserToken}"}
    resp = await client.post("/api/v1/model-configs", json={...}, headers=headers)
    assert resp.status_code == 403
    assert "must_change_password" in resp.json()["detail"]

@pytest.mark.asyncio
async def test_must_change_user_can_change_password(client, mustChangeUserToken):
    headers = {"Authorization": f"Bearer {mustChangeUserToken}"}
    resp = await client.post("/api/v1/auth/change-password", json={
        "oldPassword": "old", "newPassword": "NewSecure123!"
    }, headers=headers)
    assert resp.status_code == 200

@pytest.mark.asyncio
async def test_must_change_user_can_call_me(client, mustChangeUserToken):
    headers = {"Authorization": f"Bearer {mustChangeUserToken}"}
    resp = await client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200

@pytest.mark.asyncio
async def test_after_change_password_guard_removed(client, userAfterChangeToken):
    headers = {"Authorization": f"Bearer {userAfterChangeToken}"}
    resp = await client.post("/api/v1/model-configs", json={...}, headers=headers)
    assert resp.status_code in (201, 403)  # 业务 RBAC 仍生效，但不再 must_change 阻断
```

**GREEN** — `dependencies.py`：

```python
ALLOWED_PATHS_WHEN_MUST_CHANGE = frozenset({
    "/api/v1/auth/me",
    "/api/v1/auth/change-password",
    "/api/v1/auth/logout",
})

async def mustChangePasswordGuard(request: Request, user: User = Depends(getCurrentUser)) -> None:
    if not user.must_change_password:
        return
    path = request.url.path
    if path not in ALLOWED_PATHS_WHEN_MUST_CHANGE:
        raise HTTPException(
            status_code=403,
            detail={"code": "MUST_CHANGE_PASSWORD", "message": "必须先修改密码"},
        )
```

挂到 router 上：`dependencies=[Depends(mustChangePasswordGuard)]`（除上面三个白名单路由外）。

**前端** `RequireAuth.tsx`：

```tsx
if (me?.mustChangePassword && !isAllowedPath(location.pathname, ["auth/change-password", "auth/login"])) {
  return <Navigate to="/auth/change-password" replace />;
}
```

**Commit**：
```bash
git commit -am "[audit-fx-1.3] enforce must_change_password server-side + frontend redirect"
```

---

## Phase 2：认证生命周期 + 数据完整性（3 MEDIUM）

### Task 2.1：401 vs 403 拆分（Finding 4）

**Files:**
- Modify: `backend/app/dependencies.py:194`
- Modify: `backend/app/domain/exceptions.py:254`
- Modify: `frontend/src/api/client.ts:83`
- Test: `backend/app/tests/integration/test_auth_status_codes.py`（新）
- Test: `frontend/src/api/__tests__/client-interceptor.test.ts`（新）

**RED** — `test_auth_status_codes.py`：

```python
@pytest.mark.asyncio
async def test_expired_token_returns_401(client, expiredToken):
    headers = {"Authorization": f"Bearer {expiredToken}"}
    resp = await client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "AUTH_EXPIRED"

@pytest.mark.asyncio
async def test_invalid_signature_returns_401(client):
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer bad.token.here"})
    assert resp.status_code == 401

@pytest.mark.asyncio
async def test_logged_in_but_no_permission_returns_403(client, userWithoutAdminToken):
    headers = {"Authorization": f"Bearer {userWithoutAdminToken}"}
    resp = await client.get("/api/v1/users", headers=headers)
    assert resp.status_code == 403  # 已认证但无权限
```

**GREEN** — 拆分异常类：

```python
class AuthenticationFailed(HTTPException):
    def __init__(self, code: str, message: str):
        super().__init__(status_code=401, detail={"code": code, "message": message})

class PermissionDenied(HTTPException):
    def __init__(self, code: str, message: str):
        super().__init__(status_code=403, detail={"code": code, "message": message})
```

`getCurrentUser` 用 `AuthenticationFailed`；`require_admin` / `require_role` 用 `PermissionDenied`。

**前端** `client.ts`：

```typescript
axios.interceptors.response.use(
  response => response,
  error => {
    if (error.response?.status === 401) {
      localStorage.removeItem("token");
      window.location.href = "/auth/login";
    }
    return Promise.reject(normalizeError(error));
  }
);
```

**Commit**：
```bash
git commit -am "[audit-fx-2.1] split 401 AuthenticationFailed from 403 PermissionDenied + frontend token cleanup"
```

---

### Task 2.2：模型配置缓存失效（Finding 5）

**Files:**
- Modify: `backend/app/infrastructure/llm/factory.py:48`（缓存键 = `(config_id, updated_at_epoch)`）
- Modify: `backend/app/services/model_config_service.py:80`（updateConfig 显式调 factory.evict）

**RED** — `backend/app/tests/unit/test_llm_factory_cache.py`：

```python
@pytest.mark.asyncio
async def test_cache_invalidated_on_endpoint_update(dbSession, modelConfig):
    client1 = await getOrCreateClient(modelConfig.id)
    modelConfig.api_endpoint = "https://api.anthropic.com/v1"
    await dbSession.commit()
    client2 = await getOrCreateClient(modelConfig.id)
    assert client1 is not client2
    assert client2.endpoint == "https://api.anthropic.com/v1"

@pytest.mark.asyncio
async def test_cache_returns_same_object_if_unchanged(modelConfig):
    c1 = await getOrCreateClient(modelConfig.id)
    c2 = await getOrCreateClient(modelConfig.id)
    assert c1 is c2
```

**GREEN** — `factory.py`：

```python
@lru_cache(maxsize=128)
def _buildClient(config_id: int, updated_at_epoch: int, endpoint: str, key_cipher: str):
    return OpenAIClient(endpoint=endpoint, api_key=decrypt(key_cipher))

async def getOrCreateClient(config_id: int) -> LlmClient:
    cfg = await loadConfig(config_id)
    return _buildClient(cfg.id, int(cfg.updated_at.timestamp()), cfg.api_endpoint, cfg.api_key_cipher)

def evictClient(config_id: int) -> None:
    _buildClient.cache_clear()  # 简单可靠；规模大再换精确 evict
```

`updateConfig` 末尾 `factory.evictClient(config_id)`。

**Commit**：
```bash
git commit -am "[audit-fx-2.2] model config cache keyed by updated_at + explicit eviction on update"
```

---

### Task 2.3：bcrypt 72 字节上限（Finding 6）

**Files:**
- Modify: `backend/app/schemas/auth.py:17`（Pydantic validator）
- Modify: `backend/app/services/password_policy.py:24`
- Test: `backend/app/tests/unit/test_password_validation.py`（新）

**RED**：

```python
def test_72_byte_password_accepted():
    pwd = "a" * 72
    schema = LoginRequest(username="x", password=pwd)
    assert schema.password == pwd

def test_73_byte_password_rejected():
    pwd = "a" * 73
    with pytest.raises(ValidationError) as exc:
        LoginRequest(username="x", password=pwd)
    assert "字节" in str(exc.value)

def test_multibyte_password_under_72_bytes_accepted():
    # 24 个汉字 × 3 字节 = 72 字节
    pwd = "中" * 24
    schema = LoginRequest(username="x", password=pwd)
    assert schema.password == pwd

def test_multibyte_password_over_72_bytes_rejected():
    # 25 个汉字 × 3 字节 = 75 字节
    pwd = "中" * 25
    with pytest.raises(ValidationError):
        LoginRequest(username="x", password=pwd)
```

**GREEN** — `schemas/auth.py`：

```python
@field_validator("password")
@classmethod
def _validatePasswordBytes(cls, v: str) -> str:
    if len(v.encode("utf-8")) > 72:
        raise ValueError("密码 UTF-8 字节数超过 72")
    return v
```

应用到 `LoginRequest` / `ChangePasswordRequest` / `ResetPasswordRequest` / `UserCreate` 全部。

**Commit**：
```bash
git commit -am "[audit-fx-2.3] enforce 72-byte UTF-8 password limit at schema boundary"
```

---

## Phase 3：运维与前端边界（3 MEDIUM）

### Task 3.1：限流身份接入真用户（Finding 7）

**Files:**
- Modify: `backend/app/infrastructure/rate_limit.py:98`（移到 `getCurrentUser` 之后）
- Modify: `backend/app/dependencies.py`（`getCurrentUser` 写 `request.state.user`）
- Test: `backend/app/tests/integration/test_rate_limit_per_user.py`（新）

**RED**：

```python
@pytest.mark.asyncio
async def test_user_id_strategy_isolates_users(client, userAToken, userBToken):
    # 把限流阈值配小一些
    for _ in range(3):
        r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {userAToken}"})
        assert r.status_code == 200
    # 用户 A 已触发限流；用户 B 仍可访问
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {userBToken}"})
    assert r.status_code == 200
```

**GREEN**：

```python
async def getCurrentUser(request: Request, token: str = Depends(oauth2Scheme)) -> User:
    user = await _resolveUser(token)
    request.state.user = user  # 限流可用
    return user
```

`rate_limit.py` 改为路由级 Depends（在 `getCurrentUser` 之后），不再用中间件。

**Commit**：
```bash
git commit -am "[audit-fx-3.1] rate limit reads real user identity from request.state"
```

---

### Task 3.2：上传流式大小限制（Finding 8）

**Files:**
- Modify: `backend/app/api/v1/documents.py:203`
- Modify: `backend/app/api/v1/wiki_import.py:149`
- Modify: `backend/app/main.py`（全局 request body 限制）
- Test: `backend/app/tests/integration/test_upload_size_limit.py`（新）

**RED**：

```python
@pytest.mark.asyncio
async def test_document_upload_above_limit_rejected(client, adminToken):
    headers = {"Authorization": f"Bearer {adminToken}"}
    big = b"x" * (100 * 1024 * 1024)  # 100 MB
    files = {"file": ("big.pdf", big, "application/pdf")}
    resp = await client.post("/api/v1/documents/upload", files=files, headers=headers)
    assert resp.status_code == 413

@pytest.mark.asyncio
async def test_wiki_upload_above_10mb_rejected(client, adminToken):
    big = b"x" * (11 * 1024 * 1024)
    files = {"file": ("big.md", big, "text/markdown")}
    resp = await client.post("/api/v1/wiki/import", files=files, headers=headers)
    assert resp.status_code == 413
```

**GREEN** — `documents.py`：

```python
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
async def uploadDocument(file: UploadFile = File(...)):
    chunks = []
    total = 0
    while chunk := await file.read(64 * 1024):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="文件超过 50 MB")
        chunks.append(chunk)
    content = b"".join(chunks)
```

**Commit**：
```bash
git commit -am "[audit-fx-3.2] streaming upload size limit at 50MB / 10MB for documents / wiki"
```

---

### Task 3.3：前端错误结构统一（Finding 10）

**Files:**
- Modify: `frontend/src/api/client.ts:103`
- Modify: `frontend/src/pages/LoginPage.tsx:33`
- Test: `frontend/src/api/__tests__/client-error.test.ts`（新）
- Test: `frontend/src/pages/__tests__/LoginPage.test.tsx`（新）

**RED**：

```typescript
test("interceptor returns AppError on 401", async () => {
  const err = await runInterceptor({ status: 401, data: { detail: { code: "AUTH_INVALID" }}});
  expect(err.code).toBe("AUTH_INVALID");
  expect(localStorage.getItem("token")).toBeNull();
});

test("LoginPage shows '账号或密码错误' on AUTH_INVALID", async () => {
  render(<LoginPage />);
  await fillAndSubmit("wrong", "wrong");
  expect(await screen.findByText("账号或密码错误")).toBeInTheDocument();
});
```

**GREEN** — `client.ts`：

```typescript
export interface AppError extends Error {
  code: string;
  status: number;
  detail?: unknown;
}
export function normalizeError(err: AxiosError): AppError {
  const status = err.response?.status ?? 0;
  const detail = err.response?.data?.detail ?? null;
  const code = typeof detail === "object" && detail?.code ? detail.code : "UNKNOWN";
  const message = typeof detail === "object" && detail?.message ? detail.message : err.message;
  const appErr: AppError = Object.assign(new Error(message), { code, status, detail });
  if (status === 401) {
    localStorage.removeItem("token");
    window.location.href = "/auth/login";
  }
  return appErr;
}
```

后端错误码补充：`E_AUTH_INVALID_PASSWORD` / `E_AUTH_RATE_LIMITED` / `E_AUTH_EXPIRED`（在 Task 1.2 / 2.1 已部分定义）。

**Commit**：
```bash
git commit -am "[audit-fx-3.3] unify frontend error structure + LoginPage consumes code not text"
```

---

## Phase 4：文档密级访问控制（Finding 9）

> **策略已定**（4 档）：
> - L0 公开 / L1 内部 → 所有登录用户
> - L2 受限 → 用户有 `restricted_access=true` OR 角色含 `restricted_viewer`
> - L3 机密 → 用户在 `l3_acl` 表 OR 角色含 `compliance_officer`

### Task 4.1：l3_acl 表 + alembic 迁移

**Files:**
- Modify: `backend/app/domain/models.py`（新增 `L3Acl` model）
- Create: `backend/alembic/versions/0105_l3_acl.py`（alembic 迁移）
- Test: `backend/app/tests/integration/test_l3_acl_migration.py`（新）

**RED**：

```python
@pytest.mark.asyncio
async def test_l3_acl_table_exists(dbSession):
    result = await dbSession.execute(text(
        "SELECT to_regclass('qa_metadata.l3_acl')"
    ))
    assert result.scalar() == "l3_acl"

@pytest.mark.asyncio
async def test_l3_acl_insert_and_query(dbSession, user, resource):
    await dbSession.execute(L3Acl.__table__.insert().values(
        user_id=user.id, resource_pattern="dataset:*", granted_by=1
    ))
    await dbSession.commit()
    rows = await dbSession.execute(select(L3Acl).where(L3Acl.user_id == user.id))
    assert rows.scalars().one().resource_pattern == "dataset:*"
```

**GREEN**：

```python
class L3Acl(Base):
    __tablename__ = "l3_acl"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    resource_pattern: Mapped[str] = mapped_column(String(255))  # 模糊匹配
    granted_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    granted_at: Mapped[datetime] = mapped_column(server_default=func.now())
```

alembic 迁移 `0105_l3_acl.py`：

```python
def upgrade():
    op.create_table(
        "l3_acl",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("resource_pattern", sa.String(255), nullable=False),
        sa.Column("granted_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("granted_at", sa.DateTime, server_default=sa.func.now()),
    )

def downgrade():
    op.drop_table("l3_acl")
```

**Commit**：
```bash
git commit -am "[audit-fx-4.1] add l3_acl table + alembic migration 0105"
```

---

### Task 4.2：用户 restricted_access 字段 + compliance_officer 角色

**Files:**
- Modify: `backend/app/domain/models.py`（User 加 `restricted_access: bool`）
- Modify: `backend/app/schemas/auth.py`（User schema）
- Modify: `backend/app/services/auth_service.py`（默认值 + admin 设置接口）
- Create: `backend/alembic/versions/0106_user_restricted_access.py`
- Test: `backend/app/tests/integration/test_user_clearance.py`（新）

**RED**：

```python
@pytest.mark.asyncio
async def test_user_has_restricted_access_field(dbSession, user):
    assert hasattr(user, "restricted_access")
    assert user.restricted_access is False  # 默认

@pytest.mark.asyncio
async def test_admin_can_grant_restricted_access(client, adminToken, user):
    resp = await client.patch(f"/api/v1/users/{user.id}", json={"restrictedAccess": True}, headers=...)
    assert resp.status_code == 200
```

**GREEN**：model 字段 + schema 暴露 + PATCH 路由 + alembic 0106。

**Commit**：
```bash
git commit -am "[audit-fx-4.2] add user.restricted_access field + admin patch endpoint"
```

---

### Task 4.3：RAG 检索接入密级过滤

**Files:**
- Modify: `backend/app/services/rag_service.py:237`
- Create: `backend/app/services/document_clearance.py`（filter 构造器）
- Test: `backend/app/tests/integration/test_rag_clearance.py`（新）

**RED**：

```python
@pytest.mark.asyncio
async def test_l3_document_excluded_for_non_authorized_user(ragService, l3Document, regularUser):
    results = await ragService.retrieve(query="anything", user=regularUser)
    assert all(r.id != l3Document.id for r in results)

@pytest.mark.asyncio
async def test_l3_document_returned_for_l3_acl_holder(ragService, l3Document, l3AuthorizedUser):
    results = await ragService.retrieve(query="anything", user=l3AuthorizedUser)
    assert any(r.id == l3Document.id for r in results)

@pytest.mark.asyncio
async def test_l2_document_returned_for_restricted_user(ragService, l2Document, restrictedUser):
    results = await ragService.retrieve(query="anything", user=restrictedUser)
    assert any(r.id == l2Document.id for r in results)
```

**GREEN** — `document_clearance.py`：

```python
def buildClearanceFilter(user: User) -> list[str]:
    """返回用户可访问的密级列表。"""
    if user.has_role("compliance_officer") or _inL3Acl(user):
        return ["L0", "L1", "L2", "L3"]
    if getattr(user, "restricted_access", False) or user.has_role("restricted_viewer"):
        return ["L0", "L1", "L2"]
    return ["L0", "L1"]
```

`rag_service.retrieve` 调 `buildClearanceFilter(user)` 并把密级作为 Milvus `expr` 过滤。

**Commit**：
```bash
git commit -am "[audit-fx-4.3] rag_service enforces document clearance filter per user"
```

---

### Task 4.4：documents 列表/详情接入密级过滤

**Files:**
- Modify: `backend/app/api/v1/documents.py:234`
- Modify: `backend/app/services/document_service.py`
- Test: `backend/app/tests/integration/test_documents_clearance.py`（新）

**RED**：

```python
@pytest.mark.asyncio
async def test_documents_list_excludes_l3_for_unauthorized(client, regularUserToken, l3Doc):
    headers = {"Authorization": f"Bearer {regularUserToken}"}
    resp = await client.get("/api/v1/documents", headers=headers)
    assert resp.status_code == 200
    assert all(d["id"] != l3Doc.id for d in resp.json()["items"])

@pytest.mark.asyncio
async def test_documents_detail_403_for_unauthorized(client, regularUserToken, l3Doc):
    resp = await client.get(f"/api/v1/documents/{l3Doc.id}", headers=...)
    assert resp.status_code == 403
```

**GREEN**：`listDocuments` / `getDocument` 都接收 `user` 参数，构造 `WHERE classification IN (buildClearanceFilter(user))`。

**Commit**：
```bash
git commit -am "[audit-fx-4.4] documents list/detail enforce server-side clearance filter"
```

---

### Task 4.5：密级反例测试套件

**Files:**
- Modify: `backend/app/tests/integration/test_documents_clearance.py`
- Modify: `backend/app/tests/integration/test_rag_clearance.py`

补双向反例：未授权用户尝试 4 个密级全部 403/过滤掉；合规官 + L3 用户能访问所有；restricted 用户能访问 L2；普通用户能访问 L0/L1。

**Commit**：
```bash
git commit -am "[audit-fx-4.5] clearance policy: exhaustive negative tests"
```

---

## Phase 5：质量债（1 LOW + 维护观察）

### Task 5.1：temperature 字段往返（Finding 11）

**Files:**
- Modify: `backend/app/services/model_config_service.py:36`
- Test: `backend/app/tests/integration/test_model_config_temperature.py`（新）

**RED**：

```python
@pytest.mark.asyncio
async def test_temperature_persisted_on_create(client, adminToken):
    resp = await client.post("/api/v1/model-configs", json={
        "provider": "openai", "name": "t", "modelId": "gpt-4o",
        "apiEndpoint": "https://api.openai.com/v1", "apiKey": "sk-x",
        "temperature": 0.7,
    }, headers={"Authorization": f"Bearer {adminToken}"})
    assert resp.status_code == 201
    assert resp.json()["temperature"] == 0.7
    # 再 get 一次
    detail = await client.get(f"/api/v1/model-configs/{resp.json()['id']}", headers=...)
    assert detail.json()["temperature"] == 0.7
```

**GREEN**：`createModelConfig` 内 `temperature=data.temperature`。

**Commit**：
```bash
git commit -am "[audit-fx-5.1] persist model temperature on create (round-trip)"
```

---

### Task 5.2：移除测试 fixtures 的 SQLite fallback

**Files:**
- Modify: `backend/app/tests/conftest.py`
- Modify: `backend/app/tests/integration/conftest.py`
- Test: 所有现有测试必须仍然过；增加 `test_no_sqlite_fallback` 守门

**RED**：

```python
def test_no_sqlite_in_fixtures():
    import subprocess
    out = subprocess.check_output(["grep", "-r", "sqlite", "app/tests/"], text=True)
    # 只允许在注释或测试 sqlite 自身行为的文件里出现
    allowed = ["app/tests/unit/test_sqlite_legacy_migration.py"]
    leaks = [l for l in out.splitlines() if not any(a in l for a in allowed)]
    assert not leaks, f"禁止 SQLite 路径: {leaks}"
```

**GREEN**：删除 fallback；保留的 `app/tests/unit/test_sqlite_legacy_migration.py` 改名为 `.legacy` 跳过。

**Commit**：
```bash
git commit -am "[audit-fx-5.2] remove SQLite fallback from test fixtures; enforce PG-only"
```

---

### Task 5.3：合并 `_testapp` 与 `app.main.createApp`

**Files:**
- Modify: `backend/app/tests/integration/conftest.py`（用 `createApp()` 替换 `_testapp`）
- Modify: `backend/app/tests/integration/test_*.py`（移除 import `_testapp`）
- Test: `backend/app/tests/integration/test_app_lifespan.py`（新）— 验证 lifespan 不抛、路由就绪

**RED**：

```python
@pytest.mark.asyncio
async def test_create_app_lifespan_runs_without_error(dbSession):
    from app.main import createApp
    app = createApp()
    async with app.router.lifespan_context(app):
        # 路由注册成功
        assert any(r.path == "/api/v1/auth/me" for r in app.routes)
```

**GREEN**：替换所有 `from app.tests.integration._testapp import app` 为 `from app.main import createApp` + `createApp()`。`conftest.py` 加 fixture 自动建 app。

**Commit**：
```bash
git commit -am "[audit-fx-5.3] unify testapp with production createApp; ensure lifespan runs"
```

---

## Phase 5.4：文档同步

**Files:**
- Modify: `wiki/aicode/qa-system/docs/security.md`（删除 "Stub MVP" 段；链接实际 SQL Guard 路径）
- Modify: `wiki/aicode/qa-system/docs/auth.md`（说明 401 vs 403、mustChangePassword、生产门禁）
- Commit:
```bash
git commit -am "[audit-fx-5.4] sync security/auth docs to reflect post-audit state"
```

---

## Verification（最终验证）

### 单元测试

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  python -m pytest app/tests/unit -v
```

**预期**：原 12 个 R2 ReportPlanner 测试 + R3 待合并测试（不在本 worktree） + Phase 1-5 新增单元测试全绿。

### 集成测试

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  python -m pytest app/tests/integration -v
```

**预期**：原 19 个全过 + Phase 1-5 新增 ~10 个全过。

### 前端测试

```bash
cd frontend
npm run test
```

**预期**：原 2 个临时拦截器测试 + Phase 3.3 + 1.3 新增 3 个全过。

### 真机 e2e 复测（继承自审计）

```bash
# 重启后端（应用新代码）
cd docker && docker compose build backend
docker compose up -d --force-recreate --no-deps backend

# 复测审计的 4 个 HIGH
# F1: 无 admin 用户 POST /api/v1/model-configs → 403
# F2: APP_ENV=production + AUTH_MODE=stub → 启动失败
# F3: must_change_password=true 用户 POST 模型 → 403
# F9: 普通用户 GET /api/v1/documents?id=<L3 文档> → 列表中不出现 / 详情 403
```

---

## Out of Scope（明确不做）

- **不**改 chat 侧任何文件
- **不**改 R3 报告侧 / display_name_resolver / chart_integration
- **不**改 ontology / milvus schema
- **不**改 A1+A2 方言守卫 / R1 schema reflection
- **不**做跨用户密级继承（如 `org → user → resource` 树）
- **不**做 L3 文档的密级脱敏（仅控制可见性）
- **不**做审计日志（403/401 事件落库）— 留到下一轮
- **不**做 SSO / OAuth / 第三方登录
- **不**做密钥轮换策略（Fernet key 仍单 key）
- **不**做多 worker 缓存同步（Task 2.2 用 updated_at 简单版）
- **不**改后端启动到 `alembic upgrade head` 之外的初始化流程
- **不**改 prod docker-compose.yml 的端口 / 镜像标签

---

## Open Decisions（已默认决策）

| 决策 | 默认 | 备选 |
|---|---|---|
| 文档密级策略 | **L0/L1 公开 + L2 restricted 角色 + L3 l3_acl OR compliance_officer** | 更细粒度（按 org/部门） |
| 分支基线 | **`fabd487d`**（审计 commit） | 当前 `feat/multi-step-persist` HEAD |
| 修复范围 | **11 项 + 维护观察全做** | 仅 HIGH 4 项 |
| 缓存失效 | **updated_at 简单版** | Redis pub/sub 多 worker |
| 限流存储 | **memory://**（单 worker） | Redis 共享 |
| 密级过滤点 | **rag_service + documents list/detail** | 仅一处 |
| L3 acl 匹配 | **精确通配**（`*` 后缀） | 完整 glob |

---

## Self-Review

**1. 审计覆盖**：
- F1 ✓ Task 1.1
- F2 ✓ Task 1.2
- F3 ✓ Task 1.3
- F4 ✓ Task 2.1
- F5 ✓ Task 2.2
- F6 ✓ Task 2.3
- F7 ✓ Task 3.1
- F8 ✓ Task 3.2
- F9 ✓ Task 4.1-4.5
- F10 ✓ Task 3.3
- F11 ✓ Task 5.1
- 维护观察 ✓ Task 5.2, 5.3, 5.4

**2. 反向断言**：每个正向测试都配反例（403/401/422/413 + 未授权 / 跨用户）。

**3. 类型一致**：`AppError` / `ProductionConfigError` / `AuthenticationFailed` / `PermissionDenied` 名字跨 task 一致。

**4. 单一职责**：`url_validator.py` / `document_clearance.py` / `validateProduction()` 都是新独立单元。

**5. 不可变性**：所有新代码不修改 in-place；alembic 迁移用 `op.create_table` 标准模式。

**6. 真实 DB 测试**：所有 `dbSession` fixture 走 5434；`conftest.py` 强 PG 路径。

**7. 前端 ≠ 后端**：错误码在前端 `AppError.code` 消费，不直接拼文案。

**8. 失败面降级**：所有新守卫都 fallback（密级过滤失败 → 当 L0 处理；URL 校验 dev 模式放行）。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-12-audit-fixes.md`.

两个执行选项：

1. **Subagent-Driven (推荐)** — 每个 task 派一个新 subagent；task 完做 spec + quality review；final whole-branch review；context 隔离好，但 token 消耗高
2. **Inline Execution** — 在本会话串行执行所有 task；token 消耗低但 context 越来越长

**选哪个？**

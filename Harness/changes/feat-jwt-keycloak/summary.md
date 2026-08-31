# 变更：feat-jwt-keycloak

- **日期**：2026-08-30
- **作者**：
- **Phase**：Phase 4.5 收尾
- **状态**：draft

## 1. 需求

用 Keycloak 替换 stub auth：客户端发 JWT Bearer token，后端用 Keycloak 的 JWKS 验签 + 解析 claims → 填充 `CurrentUser`。保留 stub 作为 dev/test 分支（`AUTH_STUB_ENABLED=1`）。

**验收标准**：

- docker-compose 加 `keycloak` service（`jboss/keycloak:24.0` + Postgres backend）
- `getCurrentUser` 增加 JWT 解析路径（与 stub 二选一，按 env gate）
- 引入 `JwksCache`（内存 + 1h TTL，自动刷新）
- Claims → CurrentUser 映射：`preferred_username` → `userId`；`realm_access.roles` → `roles`；`departments` 自定义 claim → `departments`
- dev 分支保留 stub（`AUTH_STUB_ENABLED=1`，现有测试一行不动）
- prod 分支（`AUTH_STUB_ENABLED=0`）要求 JWT 必带、签名必过、exp 未过期
- 集成测试：本地签发 RS256 token（用测试私钥）覆盖 happy / 过期 / 篡改 / 缺 roles
- 覆盖率 ≥ 80%
- **所有现有 stub 测试继续工作**（默认 AUTH_STUB_ENABLED=1）

## 2. 设计评审

**认证方案对比**：

| 方案 | 优点 | 缺点 |
|---|---|---|
| **A. Keycloak 自建**（已确认） | 与 docker-compose 同源；JWKS 自动；开源 | 多一个容器要维护 |
| B. Auth0/Okta | 免运维 SLA | 需外部账号 + 出网 |
| C. 自签 JWT | 最简单 | 把 IdP 负担搬到代码 |

**Token 格式**：RS256（Keycloak 默认）。验签用 JWKS endpoint `http://keycloak:8080/realms/qa/protocol/openid-connect/certs`。

**Claims 约定**：

```json
{
  "sub": "u-uuid-1",
  "preferred_username": "alice",
  "email": "alice@corp.com",
  "realm_access": { "roles": ["user"] },
  "departments": ["procurement"],
  "exp": 1735689600,
  "iat": 1735686000
}
```

`CurrentUser` 字段映射：

```python
userId       = claims["preferred_username"]
tenantId     = claims.get("tenant_id", "default")
roles        = tuple(claims["realm_access"]["roles"])
departments  = tuple(claims.get("departments", []))
```

**JWKS 缓存**：

- 内存 LRU + TTL 1h
- 验签前查缓存；过期或未知 kid → 重新拉 JWKS endpoint
- 单实例缓存足够（多实例部署时改 Redis 共享 — Phase 7+）

**两路径并存**（`AUTH_STUB_ENABLED` env gate）：

```python
async def getCurrentUser(authorization: str | None = Header(None), ...) -> CurrentUser:
    if os.environ.get("AUTH_STUB_ENABLED", "1") == "1":
        return _stub_from_headers(...)        # dev/test 默认
    return await _jwt_from_bearer(authorization, jwks_cache=...)  # prod
```

## 3. 数据模型变更

无。

**新增配置**（`backend/app/config.py`）：

```python
keycloak_url: str = "http://keycloak:8080"          # 容器内服务名
keycloak_realm: str = "qa"
keycloak_jwks_url: str | None = None               # 显式覆盖（测试用）
jwt_audience: str = "qa-system"
jwks_ttl_seconds: int = 3600
```

## 4. 接口契约变更

无 API 变更（`CurrentUser` 接口稳定）。

**新增依赖**（`backend/pyproject.toml`）：

```toml
[project.dependencies]
PyJWT = {extras = ["crypto"], version = "^2.8.0"}
httpx = "^0.27.0"   # 已有；拉 JWKS 用
```

## 5. 实现要点

**关键文件**：

| 文件 | 改动 |
|---|---|
| `backend/app/dependencies.py` | `getCurrentUser` 改为路由：stub / JWT |
| `backend/app/auth/jwt_verifier.py` | 新增（验签 + claims 解析） |
| `backend/app/auth/jwks_cache.py` | 新增（带 TTL 的 JWKS 缓存） |
| `backend/app/auth/__init__.py` | 新增 |
| `backend/app/config.py` | 加 Keycloak 配置 |
| `backend/.env.example` | 加 Keycloak 环境变量 |
| `docker/docker-compose.yml` | 加 `keycloak` service + `keycloak-db` |
| `docker/keycloak/realm-qa.json` | 新增（导出的 realm 配置：clients / roles / users / departments claim mapper） |
| `docker/Dockerfile.keycloak` | 可选（直接用 jboss/keycloak:24.0 image） |
| `backend/app/main.py` | 启动时校验 Keycloak 配置（`AUTH_STUB_ENABLED=0` 时必填 `KEYCLOAK_URL`） |
| `backend/app/tests/_testapp.py` | 加 `_issue_dev_jwt` helper（用本地私钥签 RS256 token） |

**JWT verifier 模板**：

```python
# app/auth/jwt_verifier.py
import jwt
from jwt import PyJWKClient
from app.domain.exceptions import PermissionDeniedError

class JwtVerifier:
    def __init__(self, jwks_url: str, audience: str, cache_ttl: int):
        self._client = PyJWKClient(jwks_url, cache_keys=True, lifespan=cache_ttl)
        self._audience = audience
    async def verify(self, token: str) -> dict:
        try:
            signing_key = self._client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, signing_key, algorithms=["RS256"], audience=self._audience)
        except jwt.PyJWTError as e:
            raise PermissionDeniedError(f"JWT 验证失败: {e}") from e
        return claims
```

**Claims → CurrentUser**：

```python
def _claims_to_user(claims: dict) -> CurrentUser:
    roles = tuple(claims.get("realm_access", {}).get("roles", [])) or ("user",)
    depts = tuple(claims.get("departments", []))
    return CurrentUser(
        userId=claims.get("preferred_username") or claims.get("sub", "anonymous"),
        tenantId=claims.get("tenant_id", "default"),
        roles=roles,
        departments=depts,
    )
```

**Keycloak realm JSON 关键段**：

- Client `qa-system-backend`：confidential，service accounts on，valid redirect URIs `http://localhost:5173/*`
- Client `qa-system-frontend`：public，PKCE，direct access grants off
- User `alice`：`realm_access.roles = ["user"]`，attribute `departments = ["procurement"]`
- User `admin1`：`realm_access.roles = ["admin", "user"]`
- Protocol mapper for `departments`：把 user attribute 映射到 JWT claim

## 6. 测试

**单测**：

- `test_jwt_verifier.py`：过期 / 篡改签名 / 错误 audience / 缺 roles
- `test_jwks_cache.py`：TTL 过期重拉 / 网络错误降级
- `test_claims_to_user.py`：完整 / 缺 departments / 多 roles

**集成**：

- `test_jwt_integration.py`（新）：
  - 启动测试 Keycloak 容器（或 mock JWKS endpoint）
  - 签发真实 RS256 token → 调任意 API → 200
  - 篡改 token → 403
  - 过期 token → 403
  - 缺角色访问 admin-only API → 403

**所有现有 stub 测试**：保持 `AUTH_STUB_ENABLED=1` 默认值，零改动。

## 7. 安全审查

- **触发 security-reviewer**：必须（替换 auth 是最大风险面）。
- **关注**：
  - 算法锁定 `["RS256"]`，拒绝 `none` / `HS256`（防止 alg confusion）
  - audience 必须校验（防止跨服务 token 重放）
  - exp / nbf 强制校验
  - JWKS endpoint 用 HTTPS（Keycloak 生产必须 TLS）
  - 缓存的 JWKS 私钥不落盘（只在内存）
  - Keycloak admin 密码不能是默认值（首次启动强制改）
  - 启动时若 `AUTH_STUB_ENABLED=0` 但 `KEYCLOAK_URL` 未配置 → fail-fast（拒绝启动）
  - Token 解析失败日志：记录 `kid` / 错误码，**不**记录 token 内容

## 8. 部署验证

```bash
cd backend
TEST_DATABASE_URL=... .venv/bin/pytest \
  app/tests/unit/test_jwt_verifier.py \
  app/tests/unit/test_jwks_cache.py \
  app/tests/integration/test_jwt_integration.py -v
# 全部现有 stub 测试也跑一遍（验证零回归）

# Docker 冒烟
cd docker && docker compose up -d keycloak keycloak-db
sleep 30  # 等 Keycloak ready
docker compose up -d backend
# 浏览器打开 http://localhost:8080 用 admin/admin 登录 Keycloak
# 创建用户 + roles + departments
# 用前端登录 → 拿 token → 调任意 API → 应能成功
```

## 9. 真实数据验证

- 启动 Keycloak + 创建 alice / admin1 / bob 三个用户，分别配 roles + departments
- 用 alice 的 token 调 `/api/v1/kpi-catalog`（GET）→ 200
- 用 bob 的 token 调 `/api/v1/kpi-catalog` POST 创建 KPI → 200（owner=procurement，bob.departments=["procurement"]）
- 用 bob 的 token 改其他部门的 KPI → 403（ACL 拒绝）
- 用 admin1 的 token 改任意 KPI → 200（admin 短路）
- 用 alice 的 token 调 `/api/v1/audit/logs` → 403（非 admin）
- 用 admin1 的 token 调 `/api/v1/audit/logs` → 200
- 全部 curl 输出 + Keycloak 管理界面截图贴进本节

## 10. 关联

- 前置：`feat-acl-extension-3-entities`（JWT 解析出的 departments 必须能驱动 ACL）
- 前置：`feat-audit-history-api`（admin 角色权限验证）
- 前置：`feat-audit-outbox`（worker 进程也要传 CurrentUser，但 worker 是系统级，直接读环境变量或专用 service token）
- 后置：未来 Phase 5+ 的多租户 / 联邦身份 / SSO 集成
- 规则：`Harness/rules/权限与安全规范.md` 全文
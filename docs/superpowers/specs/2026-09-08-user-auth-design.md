# 用户登录与个人中心 — 设计文档

- 日期：2026-09-08
- 分支：feat/user-auth
- 状态：已获用户批准的设计（2026-09-08 "采用方案A的基础上，保留后期增加后端会话管理的能力" + "直接在生产库进行开发，不要使用测试库"）

## 1. 目标

为 qa-system 增加自建账号密码登录、个人信息查看与修改密码能力，并在右上角语言切换右侧增加个人信息入口（`UserMenu`）。同步为未来接 Keycloak / 服务端会话管理预留扩展点（JWT 携带 `jti`、写 `user_sessions` 表、`POST /auth/logout` 端点、改密吊销所有 session）。

**不在本期范围**：
- 接入企业 SSO（OIDC / SAML / Keycloak）—— 预留 JWT `iss/aud` 字段与 RS256 算法类型，但不实现
- 服务端 token 黑名单强制校验 —— `user_sessions` 表写入但不读；开启校验只需在 `getCurrentUser` 加一次 SELECT
- Refresh token / 长会话 —— 默认 1h TTL，记住我仅扩到 sessionStorage
- MFA / TOTP
- 密码历史 / 强制定期改密
- 设备/IP 绑定
- httpOnly Cookie 改造（仍 localStorage + Authorization 头）

## 2. 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 认证形态 | 自建账号密码登录 + JWT (HS256) |
| 改密权限 | 用户改自己（`PUT /auth/me/password`，需旧密码）+ 管理员重置任意用户（`PUT /users/{id}/password`，无需旧密码） |
| 未登录处理 | 前端 `<RequireAuth>` 路由守卫重定向到 `/login?from=<原路径>` |
| i18n 命名空间 | 新增 `userMenu.*` 与 `auth.*` |
| 密码策略 | 中等：≥8 位、含字母 + 数字 |
| admin 初始密码 | seed 默认 `Admin@123` + `must_change_password=true`；prod 走 `SEED_ADMIN_PASSWORD` env |
| 会话管理预留 | JWT 携带 `jti`；`user_sessions` 表写入（不验证）；`POST /auth/logout` 端点 + 改密吊销所有 session |
| 个人中心 UI | 独立页 `/profile`（只读）+ `/change-password`（表单） |
| 右上角入口 | antd `Dropdown`：个人信息 / 修改密码 / 登出 |
| Token 存储 | `localStorage` + Authorization 头 |
| 路由守卫 | `App.tsx` 顶层 `<RequireAuth>` |
| **数据库** | **直接在生产库 `qa_metadata` 上开发与测试**（按 `Harness/rules/数据库环境使用规范.md`：表结构/迁移直接 prod，先 `pg_dump` 备份；`qa_metadata_test` 不参与本期开发） |

## 3. 架构与数据流

```
┌─────────── 未登录 ───────────┐         ┌─────────── 已登录 ───────────┐
│                              │         │                              │
│ 访问任意非 /login 路由        │  302    │ AppLayout Header 右侧       │
│   ↓                          │ ──────→ │   UserMenu Dropdown          │
│ <RequireAuth> 检测            │         │   ├─ 个人信息 → /profile    │
│   localStorage.auth.token?   │         │   ├─ 修改密码 → /change-pwd │
│   - 无 → 跳 /login?from=...  │         │   └─ 登出 → POST /auth/     │
│                              │         │       logout + 清 token     │
└──────────────────────────────┘         └──────────────────────────────┘
                  ↓                                      ↑
            /login 提交                                  │
                  ↓                                      │
        POST /api/v1/auth/login                         │
        {username, password}                            │
                  ↓                                      │
        bcrypt 校验 users.password_hash                 │
                  ↓                                      │
        生成 JWT {sub, jti, iat, exp, iss, aud}         │
        写入 user_sessions (本期不强制验证)              │
                  ↓                                      │
        200 {accessToken, mustChangePassword, user}     │
                  ↓                                      │
        写 localStorage + 跳 from 或 /                  │
        mustChangePassword=true → 跳 /change-pwd ──────┘
```

## 4. 改动分层

| 层 | 改动 | 文件数估算 |
|---|---|---|
| 后端 Domain | `User` 模型加 4 字段；新增 `UserSession` 模型；`Auth*` schemas | 3 |
| 后端 Infrastructure | bcrypt 工具；JWT 签发/校验工具；Alembic 0048 | 3 |
| 后端 Service | `AuthService` (login/changeOwnPassword/adminResetPassword/logout/getMe) + `password_policy` | 3 |
| 后端 API | `auth.py`；`users.py` 加 admin 重置密码端点 | 2 |
| 后端 Middleware | `getCurrentUser` 优先解析 Bearer，dev 回退 stub | 1 |
| 后端 Scripts | `seed_rbac.py` 改；`seed_user_passwords.py` 新（一次性为 NULL 密码用户初始化） | 2 |
| 后端 Messages | `messages_zh.py` 加 8 个 MSG_* 常量 | 1 |
| 前端 Page | `LoginPage`、`ProfilePage`、`ChangePasswordPage` | 3 |
| 前端 Component | `RequireAuth`、`UserMenu` | 2 |
| 前端 Store | `authStore` (Zustand + persist) | 1 |
| 前端 API | `api/auth.ts`、`api/profile.ts` | 2 |
| 前端 i18n | `userMenu.*` + `auth.*` (zh-CN + en-US) | 2 |
| 前端 Config | 移除 `DEFAULT_USER_ID` 硬编码；token 注入 axios | 1 |
| 前端 E2E | `auth-flow.spec.ts` (Playwright) | 1 |
| 菜单 seed | `seed_menu_config.py` 加 2 个 item + 4 处断言改 34→36/28→30 | 2 |
| 后端测试 | 单元 + 集成 ~10 个文件 | 10 |
| 前端测试 | Vitest + RTL ~8 个文件 | 8 |
| **合计** | | **~45 文件** |

## 5. 关键不变量

1. `password_hash` 永不进 `UserRead`（list/detail/me 都不暴露）
2. `must_change_password` 仅出现在 `AuthMeRead` 和 login 响应，不进 `UserRead`
3. `AUTH_STUB_ENABLED=0` 时 `X-User-Id` 头被忽略；`AUTH_STUB_ENABLED=1`（dev）仍可走桩
4. bcrypt cost factor = 12（OWASP 2024 推荐下限）
5. 生产环境剥离客户端 `Authorization` / `X-User-*` 头（Nginx / ingress 配置）

## 6. 数据模型（Alembic 0048）

### 6.1 `users` 表扩展

```python
# alembic/versions/0048_user_password.py
def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("last_login_ip", sa.String(45), nullable=True))
    op.create_index(
        "ix_users_must_change_password",
        "users",
        ["must_change_password"],
        postgresql_where=sa.text("must_change_password = true"),
    )

def downgrade() -> None:
    op.drop_index("ix_users_must_change_password")
    op.drop_column("users", "last_login_ip")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "password_hash")
```

**关键决策**：
- `password_hash nullable=True` — 留出"先创建账号后设密"或"SSO-only 账号"两种未来形态
- 不存明文密码、不存原始 salt（bcrypt 自带 salt 编码在 hash 里）
- `last_login_at` / `last_login_ip` 是审计需要，不进 DTO

### 6.2 新表 `user_sessions`（会话预留，本期写入不强制验证）

```python
def upgrade() -> None:
    # ... users 字段增量（同 6.1）...
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("jti", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(64), nullable=True),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_user_sessions_jti", "user_sessions", ["jti"], unique=True)
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_active", "user_sessions", ["user_id", "revoked_at"])
```

**本期行为**：
- 登录时：写入一条新行（jti 唯一）
- 登出时：把当前 jti 行的 `revoked_at` 设为 now()，**但 `getCurrentUser` 不查询该表**（保留扩展点）
- 改密时（用户自己改或 admin 重置）：把所有该用户的 active session 标记 `revoked_at=now(), revoked_reason='password_changed'/'admin_reset'`

**未来扩展点**（代码注释标 TODO）：
- `getCurrentUser` 加 `_validate_session(jti)` 钩子：开启后做单点失效
- `GET /api/v1/auth/sessions` 列出当前用户活跃 session
- `DELETE /api/v1/auth/sessions/{id}` 远程吊销
- 过期 session 后台清理 job

### 6.3 ORM 模型

```python
# backend/app/models/rbac.py
class User(Base, TimestampMixin):
    # ... 既有字段 ...
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)


class UserSession(Base):
    """登录会话记录（本期写入不验证；为将来服务端会话失效预留）。"""
    __tablename__ = "user_sessions"
    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
```

### 6.4 Seed 脚本（`backend/scripts/seed_rbac.py` 增量）

```python
DEFAULT_ADMIN_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD", "Admin@123")

def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")

# admin upsert：
admin_user = User(
    username="admin",
    display_name="System Admin",
    email=None,
    enabled=True,
    password_hash=_hash(DEFAULT_ADMIN_PASSWORD),
    must_change_password=True,  # 首次登录强制改密
)
```

### 6.5 一次性迁移脚本（`backend/scripts/seed_user_passwords.py` 新）

> **重要**：0048 迁移后，老 `users` 行的 `password_hash` 全部为 NULL，必须跑此脚本为 enabled=true 的现有用户初始化密码。

```python
# 1. 查 password_hash IS NULL AND enabled=true 的所有用户
# 2. 为每个用户生成长度 12 的临时密码（大小写+数字+特殊字符）
# 3. bcrypt 哈希写入 password_hash
# 4. must_change_password = true
# 5. 写 CSV：username, display_name, temp_password 到 --output 指定文件
# 6. 完成后建议删除该 CSV
```

```bash
# 仅本地/dev（dev 默认密码：admin/Admin@123）
docker exec qa-backend python -m backend.scripts.seed_user_passwords \
  --output /tmp/initial_passwords.csv
```

**生产禁用**：脚本启动检查 `AUTH_STUB_ENABLED=0` 时直接拒绝运行（拒绝条件是 `os.environ.get("AUTH_STUB_ENABLED") == "0"`，即产线环境）。

## 7. 后端 API 设计

### 7.1 新文件 `backend/app/api/v1/auth.py`

```python
router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login", response_model=AuthLoginResponse) ...
@router.post("/logout", status_code=204) ...
@router.get("/me", response_model=AuthMeRead) ...
@router.put("/me/password", status_code=204) ...
@router.get("/password-policy", response_model=PasswordPolicyRead) ...
```

**挂载**（`backend/app/main.py:226-258`）：
```python
app.include_router(auth.router, tags=["auth"])
# 路径自动 /api/v1/auth/*
```

### 7.2 端点契约

#### `POST /api/v1/auth/login`

| 字段 | 说明 |
|---|---|
| Request | `AuthLoginRequest { username: str, password: str }` |
| 200 | `AuthLoginResponse { accessToken, tokenType:"Bearer", expiresIn:3600, mustChangePassword, user: UserSummaryRead }` |
| 401 | 用户名不存在 / 密码错 / 账号 enabled=false（统一 `MSG_INVALID_CREDENTIALS`，防枚举） |
| 422 | 字段校验（空 username / 空 password） |
| 429 | 限流：5 次/分钟/IP+username |

**关键实现**：
```python
async def login(payload, request, session):
    user = await _find_by_username(session, payload.username)
    if not user or not user.password_hash or not user.enabled:
        # 假用户也跑一遍 bcrypt + sleep 防时间侧信道
        bcrypt.checkpw(b"dummy", _DUMMY_HASH.encode())
        await asyncio.sleep(0.2)
        raise AuthFailedError()
    if not bcrypt.checkpw(payload.password.encode(), user.password_hash.encode()):
        raise AuthFailedError()
    
    jti = str(uuid.uuid4())
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=JWT_TTL_SECONDS)
    access_token = _sign_jwt(user_id=user.id, jti=jti, ...)
    
    # 写 user_sessions（不强制验证）
    session.add(UserSession(
        jti=jti, user_id=user.id, issued_at=issued_at, expires_at=expires_at,
        ip=request.client.host, user_agent=request.headers.get("user-agent"),
    ))
    user.last_login_at = issued_at
    user.last_login_ip = request.client.host
    await session.commit()
    
    return AuthLoginResponse(
        accessToken=access_token, tokenType="Bearer", expiresIn=JWT_TTL_SECONDS,
        mustChangePassword=user.must_change_password,
        user=UserSummaryRead.from_orm(user),
    )
```

#### `POST /api/v1/auth/logout`

| Auth | Bearer token（解析 jti） |
|---|---|
| 204 | 无 body |
| 401 | 无 token / token 无效 |

```python
async def logout(actor, session) -> Response:
    # actor.jti 由 getCurrentUser 注入
    await session.execute(
        update(UserSession)
        .where(UserSession.jti == actor.jti, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc), revoked_reason="logout")
    )
    await session.commit()
    return Response(status_code=204)
```

#### `GET /api/v1/auth/me`

```python
class AuthMeRead(CamelModel):
    id: int
    username: str
    displayName: str
    email: str | None
    enabled: bool
    mustChangePassword: bool          # 仅此处返回
    roles: list[str]                  # 解析自 user_roles + 兜底
    organizations: list[str]          # 解析自 user_organizations
    tenantId: str
    lastLoginAt: datetime | None
```

**关键**：`password_hash` / `last_login_ip` **不返回**。

#### `PUT /api/v1/auth/me/password`

| 字段 | 说明 |
|---|---|
| Request | `AuthChangePasswordRequest { oldPassword, newPassword }` |
| 204 | 无 body |
| 401 | 旧密码错（`MSG_OLD_PASSWORD_INCORRECT`，与 login 区分） |
| 422 | 新密码不满足复杂度 |
| 429 | 限流：3 次/小时/user |

**关键行为**：
- 改密成功后，**强制吊销该用户所有 active session**（含当前 session）
- 前端收到 204 后：清 localStorage + 跳 /login 提示"密码已修改，请重新登录"

#### `GET /api/v1/auth/password-policy`（无需认证）

```python
class PasswordPolicyRead(CamelModel):
    minLength: int = 8
    requireLetter: bool = True
    requireDigit: bool = True
    requireSpecial: bool = False       # 中策略
    minSpecialCount: int | None = None
```

#### admin 重置密码（`backend/app/api/v1/users.py` 增量）

```python
@router.put("/{user_id}/password", status_code=204,
            dependencies=[Depends(getAdminOnlyActor)])
async def admin_reset_password(
    user_id: int,
    payload: AdminResetPasswordRequest,  # { newPassword, forceChangeOnNextLogin=True }
    actor: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> Response:
```

- 不需要旧密码（admin 权限）
- 默认 `forceChangeOnNextLogin=True`
- 改密成功同样吊销该用户所有 active session（reason="admin_reset"）
- 审计：复用 `Harness/changes/feat-audit-history-api` 的 `AuditLogPage` + `listAll` 机制

### 7.3 `getCurrentUser` 改造（`backend/app/dependencies.py:53-110`）

```python
async def getCurrentUser(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    xUserId: str | None = Header(default=None, alias="X-User-Id"),
    xTenantId: str | None = Header(default=None, alias="X-Tenant-Id"),
    xUserRoles: str | None = Header(default=None, alias="X-User-Roles"),
    xUserDepartments: str | None = Header(default=None, alias="X-User-Departments"),
    session: AsyncSession = Depends(getDb),
) -> CurrentUser:
    # 优先级 1: Authorization: Bearer <jwt>
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        try:
            payload = _decode_jwt(token)  # 验签 + exp + iss + aud
        except JWTError:
            raise PermissionDeniedError("MSG_TOKEN_INVALID")
        return await _user_from_jwt(session, payload)  # 带回 jti/iat/exp

    # 优先级 2: dev stub 头（仅 AUTH_STUB_ENABLED=1）
    if settings.AUTH_STUB_ENABLED:
        return await _user_from_stub_headers(session, xUserId, xTenantId, xUserRoles, xUserDepartments)
    
    raise PermissionDeniedError("MSG_AUTH_REQUIRED")
```

**`CurrentUser` dataclass 扩展**：
```python
@dataclass(frozen=True)
class CurrentUser:
    userId: int
    username: str
    dbUserId: int
    tenantId: str
    roles: tuple[str, ...]
    departments: tuple[str, ...]
    jti: str | None = None            # 仅 Bearer 路径有值
    tokenExp: int | None = None
```

**JWT payload 结构**：
```python
{
    "iss": "qa-system",
    "aud": "qa-system-web",
    "sub": "<user_id 字符串>",
    "username": "admin",
    "jti": "uuid4",
    "iat": <unix>,
    "exp": <unix>,
}
```

**HS256 配置**（`backend/app/config.py` 增量）：
```python
JWT_SECRET: str = Field(..., min_length=32)  # 必须 ≥32 字节，启动校验
JWT_ALGORITHM: Literal["HS256"] = "HS256"    # 后续可扩 RS256
JWT_TTL_SECONDS: int = 3600                   # 1 小时
JWT_ISSUER: str = "qa-system"
JWT_AUDIENCE: str = "qa-system-web"
```

### 7.4 错误码（`backend/app/services/messages_zh.py` 增量）

| 常量 | 中文文案 |
|---|---|
| `MSG_INVALID_CREDENTIALS` | 用户名或密码错误 |
| `MSG_OLD_PASSWORD_INCORRECT` | 当前密码不正确 |
| `MSG_PASSWORD_TOO_WEAK` | 密码至少 8 位且必须包含字母和数字 |
| `MSG_TOKEN_INVALID` | 登录已失效，请重新登录 |
| `MSG_TOKEN_EXPIRED` | 登录已过期，请重新登录 |
| `MSG_AUTH_REQUIRED` | 请先登录 |
| `MSG_PASSWORD_CHANGED_LOGOUT` | 密码已修改，请重新登录 |
| `MSG_ACCOUNT_DISABLED` | 账号已停用，请联系管理员 |

> **用户枚举防护**：`MSG_INVALID_CREDENTIALS` 统一用于「用户名不存在」「密码错」「账号禁用」三种场景。

## 8. 前端结构

### 8.1 路由改造（`frontend/src/App.tsx`）

```tsx
<Routes>
  <Route path="/login" element={<LoginPage />} />
  <Route element={<RequireAuth />}>
    <Route element={<AppLayout />}>
      <Route path="/" element={<Navigate to="/chat" replace />} />
      {/* 27 个原有页面 ... */}
      <Route path="/profile" element={<ProfilePage />} />
      <Route path="/change-password" element={<ChangePasswordPage />} />
    </Route>
  </Route>
</Routes>
```

**`RequireAuth` 组件**（新文件 `frontend/src/components/common/RequireAuth.tsx`）：
```tsx
export default function RequireAuth() {
  const token = useAuthStore((s) => s.token);
  const location = useLocation();
  if (!token) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }
  return <Outlet />;
}
```

### 8.2 状态管理（`frontend/src/stores/authStore.ts`）

```ts
interface AuthState {
  token: string | null;
  user: AuthMeRead | null;
  mustChangePassword: boolean;
  
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  fetchMe: () => Promise<void>;
  changeOwnPassword: (oldPwd: string, newPwd: string) => Promise<void>;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null, user: null, mustChangePassword: false,
      
      login: async (username, password) => {
        const res = await authApi.login({ username, password });
        set({ token: res.accessToken, mustChangePassword: res.mustChangePassword });
        await get().fetchMe();
      },
      
      logout: async () => {
        try { await authApi.logout(); } catch {}
        set({ token: null, user: null, mustChangePassword: false });
        delete apiClient.defaults.headers.common.Authorization;
      },
      
      fetchMe: async () => {
        const me = await authApi.getMe();
        set({ user: me });
      },
      
      changeOwnPassword: async (oldPwd, newPwd) => {
        await authApi.changeOwnPassword({ oldPassword: oldPwd, newPassword: newPwd });
        await get().logout();
      },
    }),
    {
      name: "qa-system-auth",
      partialize: (s) => ({ token: s.token, mustChangePassword: s.mustChangePassword }),
    }
  )
);
```

### 8.3 axios 拦截器（`frontend/src/api/client.ts`）

```ts
apiClient.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token;
  if (token) config.headers.Authorization = `Bearer ${token}`;
  config.headers["X-Tenant-Id"] ??= "default";
  return config;
});

apiClient.interceptors.response.use(
  (resp) => resp,
  async (err) => {
    if (err.response?.status === 401 && !err.config?.url?.includes("/auth/login")) {
      const state = useAuthStore.getState();
      if (state.token) await state.logout();
    }
    return Promise.reject(err);
  }
);
```

### 8.4 文件清单

| 文件 | 用途 |
|---|---|
| `frontend/src/pages/LoginPage.tsx` | 登录页（独立 layout，**不**走 AppLayout） |
| `frontend/src/pages/ProfilePage.tsx` | 只读展示当前用户信息 |
| `frontend/src/pages/ChangePasswordPage.tsx` | 修改自己密码表单 |
| `frontend/src/components/common/RequireAuth.tsx` | 路由守卫 |
| `frontend/src/components/common/UserMenu.tsx` | Header 右侧头像 Dropdown |
| `frontend/src/stores/authStore.ts` | Zustand 认证状态 |
| `frontend/src/api/auth.ts` | login / logout / getMe / changeOwnPassword / getPasswordPolicy |
| `frontend/src/api/client.ts` | 改：移除 X-User-Id 默认；加 Authorization 注入 + 401 拦截 |
| `frontend/src/config.ts` | 改：移除 `DEFAULT_USER_ID` 导出；保留 `DEFAULT_TENANT_ID` |
| `frontend/src/components/common/AppLayout.tsx` | 改：Header 右侧在 `<LanguageSwitch />` 后加 `<UserMenu />` |
| `frontend/src/i18n/zh-CN.ts` | 增 `userMenu.*` + `auth.*` |
| `frontend/src/i18n/en-US.ts` | 增 `userMenu.*` + `auth.*` |
| `frontend/src/types/auth.ts` | `AuthLoginRequest` / `AuthLoginResponse` / `AuthMeRead` / `AuthChangePasswordRequest` / `PasswordPolicyRead` |
| `frontend/src/tests/authStore.test.ts` | store 状态机 |
| `frontend/src/tests/RequireAuth.test.tsx` | 路由守卫 |
| `frontend/src/tests/UserMenu.test.tsx` | Dropdown 三项 + mustChangePassword 红点 |
| `frontend/src/tests/LoginPage.test.tsx` | 字段校验 / 错误展示 / 限流提示 / from 回跳 |
| `frontend/src/tests/ProfilePage.test.tsx` | 字段渲染 + 跳 /change-password |
| `frontend/src/tests/ChangePasswordPage.test.tsx` | 旧密码错 / 弱密码 / 不匹配 / 成功提示 |
| `frontend/src/tests/apiAuth.test.ts` | 5 个端点 + 401 拦截器 |
| `frontend/src/tests/api.test.ts` | 加 `/profile` / `/change-password` 到 frontend_routes 集合（**更新红线**） |
| `frontend/e2e/auth-flow.spec.ts` | 7 步端到端（Playwright） |

### 8.5 UI 设计：Header 右侧 UserMenu

```
┌─────────────────────────────────────────────────────────────┐
│ 当前菜单标题    [☾ 暗色]    [中文 ▾]    [👤 admin ▾]        │
└─────────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼
                            ┌────────────────────────────────┐
                            │  admin (System Admin)          │
                            │  admin@local                   │
                            │  ─────────────────────────     │
                            │  👤 个人信息                    │
                            │  🔑 修改密码       ⚠ 需改密     │  (mustChangePassword 时红点)
                            │  ─────────────────────────     │
                            │  ⏏  登出                       │
                            └────────────────────────────────┘
```

- `antd` `<Avatar>` + `<Dropdown>` 实现
- `mustChangePassword=true` 时："修改密码"右侧加红点 + tooltip「首次登录请修改密码」
- 头像默认显示 username 首字母（无头像 URL 字段）

### 8.6 UI 设计：LoginPage

```
┌──────────────────────────────────────┐
│          [Brand Logo]                │
│        qa-system                     │
│                                      │
│    ┌────────────────────────────┐    │
│    │ 用户名                      │    │
│    │ [________________]          │    │
│    │                            │    │
│    │ 密码                        │    │
│    │ [________________] [👁]     │    │
│    │                            │    │
│    │ [ ] 记住我  (本地 8h)        │    │
│    │                            │    │
│    │ [    登 录    ]             │    │
│    │                            │    │
│    │ 忘记密码？请联系管理员        │    │
│    └────────────────────────────┘    │
│                                      │
│   中文 ▾   (跟随系统即可)             │
└──────────────────────────────────────┘
```

- 居中卡片，宽 400px；使用现有 teal `#00D9C0` 主色
- 错误用 `message.error`；429 限流提示"尝试次数过多，请稍后再试"
- 密码字段右侧眼睛图标切换可见性
- "记住我"勾选：localStorage persist（默认 8h）；不勾：sessionStorage
- 路由参数 `?from=<path>` 登录成功后回跳

### 8.7 UI 设计：ProfilePage

- 卡片列出：`用户名 / 显示名 / 邮箱 / 角色（chip 列表） / 所属组织（chip 列表） / 上次登录时间 / 租户 ID`
- 右上角"修改密码"按钮 → 跳 `/change-password`
- 字段用 antd `<Descriptions>` 布局

### 8.8 UI 设计：ChangePasswordPage

```
┌────────────────────────────────────────┐
│  修改密码                                │
│                                        │
│  当前密码                                │
│  [_____________________]                │
│                                        │
│  新密码 (≥8 位，含字母+数字)              │
│  [_____________________]                │
│  ● 强度：中                              │
│                                        │
│  确认新密码                              │
│  [_____________________]                │
│                                        │
│  [取消]                  [确认修改]      │
│                                        │
│  ⚠ 修改后需重新登录                       │
└────────────────────────────────────────┘
```

- 三字段验证通过才启用提交按钮
- 密码强度条（满足策略=中，加特殊字符且≥12=强）
- 成功后弹 info「密码已修改，请重新登录」+ 跳 `/login`

### 8.9 i18n 键规划

```ts
// zh-CN.ts 增量
userMenu: {
  profile: "个人信息",
  changePassword: "修改密码",
  logout: "登出",
  changePasswordHint: "首次登录请修改密码",
  displayName: "{displayName} ({username})",
}
auth: {
  login: {
    title: "登录 qa-system",
    username: "用户名",
    password: "密码",
    rememberMe: "记住我",
    submit: "登录",
    forgot: "忘记密码？请联系管理员",
    invalidCredentials: "用户名或密码错误",
    accountDisabled: "账号已停用，请联系管理员",
    rateLimited: "尝试次数过多，请稍后再试",
    networkError: "网络异常，请稍后重试",
  },
  changePassword: {
    title: "修改密码",
    oldPassword: "当前密码",
    newPassword: "新密码",
    confirmPassword: "确认新密码",
    policyHint: "≥8 位，含字母和数字",
    weak: "密码至少 8 位且必须包含字母和数字",
    mismatch: "两次输入不一致",
    oldWrong: "当前密码不正确",
    success: "密码已修改，请重新登录",
  },
  profile: {
    title: "个人信息",
    username: "用户名",
    displayName: "显示名",
    email: "邮箱",
    roles: "角色",
    organizations: "所属组织",
    lastLogin: "上次登录",
    tenant: "租户",
    editPassword: "修改密码",
  },
}
menu: {
  item: {
    // ... 既有 28 项 ...
    profile: "个人信息",
    changePassword: "修改密码",
  }
}
```

`en-US.ts` 同步翻译。

### 8.10 菜单 seed 改动

`backend/scripts/seed_menu_config.py` 增量：
```python
MenuItem(code="item.profile", parent="section.systemConfig",
         label_key="menu.item.profile", icon_code="user", sort_order=580,
         path="/profile"),
MenuItem(code="item.changePassword", parent="section.systemConfig",
         label_key="menu.item.changePassword", icon_code="lock", sort_order=585,
         path="/change-password"),
```

**`backend/app/tests/integration/test_seed_menu_config.py` 红线更新**（4 处断言）：
```diff
- count == 34
+ count == 36
- len(result.sections) == 6
+ len(result.sections) == 6
- total_items == 28
+ total_items == 30
- len(rows) == 34
+ len(rows) == 36
```

`frontend/src/tests/api.test.ts` 的 `frontend_routes` 集合断言加 `/profile` / `/change-password`。

## 9. 安全细节

### 9.1 密码策略实现（前后端共用）

**单一事实源**：`backend/app/services/password_policy.py`（新文件）

```python
import re
MIN_LENGTH = 8
_PASSWORD_POLICY = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{8,}$")

def validate_password(password: str) -> tuple[bool, str | None]:
    if len(password) < MIN_LENGTH:
        return False, "MSG_PASSWORD_TOO_WEAK"
    if not _PASSWORD_POLICY.match(password):
        return False, "MSG_PASSWORD_TOO_WEAK"
    return True, None

def strength_label(password: str) -> Literal["weak", "medium", "strong"]:
    if len(password) < MIN_LENGTH or not _PASSWORD_POLICY.match(password):
        return "weak"
    if re.search(r"[^A-Za-z0-9]", password) and len(password) >= 12:
        return "strong"
    return "medium"
```

**暴露规则给前端**：`GET /api/v1/auth/password-policy`（无需认证）。

**前端**：`api/auth.ts` 增加 `getPasswordPolicy()`，LoginPage / ChangePasswordPage 启动时拉一次缓存到 sessionStorage。

### 9.2 限流策略

复用现有 `SlowAPIMiddleware`（`backend/app/main.py:193-228`）：

| 端点 | 限流 key | 规则 |
|---|---|---|
| `POST /auth/login` | `ip + username` 复合 | 5 次/分钟 → 429 |
| `PUT /auth/me/password` | `userId` | 3 次/小时 → 429 |
| `PUT /users/{id}/password` (admin) | `actor.userId` | 10 次/小时 → 429 |

### 9.3 防用户名枚举（登录响应统一化）

- 用户名不存在 / 密码错 / 账号禁用 → 同一文案 `MSG_INVALID_CREDENTIALS`
- 假用户路径也跑一遍 bcrypt + sleep 200ms 防时间侧信道

### 9.4 JWT 安全

| 风险 | 对策 |
|---|---|
| 算法混淆（`alg=none` 或 RS→HS） | `_decode_jwt` 显式 `algorithms=["HS256"]` |
| 密钥强度 | `JWT_SECRET` 启动校验 `min_length=32`；K8s Secret 注入 |
| token 重放 | 本期接受；`UserSession.jti` 唯一索引 + `getCurrentUser` 钩子预留 |
| 跨服务 token 串用 | `iss=qa-system` + `aud=qa-system-web` 强校验 |
| 长有效期风险 | 默认 1h；不在"记住我"上扩展长期 token |
| 登出后 token 仍可用 | 本期接受；`revoked_at` 字段 + 钩子预留 |

### 9.5 改密吊销所有 session 的语义

```python
async def _revoke_all_user_sessions(session, user_id: int, reason: str) -> int:
    result = await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc), revoked_reason=reason)
    )
    return result.rowcount
```

- 自己改密：`reason="password_changed"`
- admin 重置：`reason="admin_reset"`
- 自己登出：`reason="logout"`（仅当前 session）
- admin 改自己密码：吊销自己的 session（语义一致）

### 9.6 审计写入

复用 `Harness/changes/feat-audit-history-api`（`commit 8c015f0..f164776`）的 `AuditLogPage` + `listAll` 机制：

| 事件 | action | target |
|---|---|---|
| 登录成功 | `auth.login` | `user:{id}` |
| 登录失败 | `auth.login_failed` | `user:{username}` |
| 登出 | `auth.logout` | `user:{id}` |
| 自己改密 | `auth.password_changed` | `user:{id}` |
| admin 重置密码 | `user.password_reset` | `user:{id}` + actor |

### 9.7 CSRF / XSS

- 本期 token 存 localStorage + Authorization header（非自动 cookie），不引入 CSRF
- 所有用户输入用 antd 受控组件，不 `dangerouslySetInnerHTML`

### 9.8 头剥离（生产部署）

`Harness/wiki/operations-runbook.md` 增补：
> 生产环境 Nginx / K8s ingress 必须剥离客户端传来的 `Authorization` / `X-User-Id` / `X-Tenant-Id` 头，仅允许后端签发的值透传。

```nginx
proxy_set_header Authorization "";
proxy_set_header X-User-Id "";
proxy_set_header X-Tenant-Id "";
proxy_set_header X-Real-IP $remote_addr;
```

### 9.9 已知妥协

| 妥协 | 影响 | 缓解 |
|---|---|---|
| HS256 共享密钥 | 任何拿到密钥的服务可伪造 token | 单后端服务；密钥走 K8s Secret |
| token 无 server 端黑名单 | 登出后 token 在 1h 内仍可用 | `UserSession.revoked_at` 预留；开启校验只需在 `getCurrentUser` 加一次 SELECT |
| localStorage 存 token | XSS 攻击可窃 token | 严格 CSP（已有）+ 最小权限原则 + 1h 短 TTL |
| 无 refresh token | 1h 后必须重新登录 | 企业内网场景可接受 |
| 无设备/IP 绑定 | token 可被复制到其他设备使用 | `last_login_ip` 留字段；后续可加异常登录检测 |
| 无密码历史 | 用户可循环使用旧密码 | 后续可加 `password_history` 表 |
| 无 MFA | 仅密码 | 后续可加 TOTP/SMS |

## 10. 测试策略

### 10.1 后端测试（强制真实 PG + 完整 API 链路，按 `Harness/rules/测试规范.md`）

| 文件 | 类型 | 覆盖 |
|---|---|---|
| `backend/app/tests/unit/test_password_policy.py` | 单元 | `validate_password` / `strength_label` 全部边界 |
| `backend/app/tests/unit/test_jwt_codec.py` | 单元 | 签发 / 验签 / 过期 / 算法混淆 / iss+aud 错 |
| `backend/app/tests/integration/test_auth_login.py` | 集成 | 200/401/422/429/账号禁用/响应时间均匀 |
| `backend/app/tests/integration/test_auth_me.py` | 集成 | GET /me 返回字段；不含 password_hash |
| `backend/app/tests/integration/test_auth_logout.py` | 集成 | 204；DB 中 revoked_at 写入 |
| `backend/app/tests/integration/test_auth_change_password.py` | 集成 | 自己改密：旧密码错/弱密码/不匹配/成功+吊销 |
| `backend/app/tests/integration/test_users_admin_reset_password.py` | 集成 | admin 重置：非 admin 403/admin 自己重置/吊销他人 session |
| `backend/app/tests/integration/test_get_current_user_bearer.py` | 集成 | Bearer 解析优先级 / stub 头剥离 / token 过期 |
| `backend/app/tests/integration/test_seed_menu_config.py` | 集成 | 改 34→36 / 28→30 红线 |
| `backend/app/tests/integration/test_audit_auth_events.py` | 集成 | 5 个审计事件写入 |

**测试数据库**：`TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/qa_metadata_test`（`Harness/rules/测试规范.md` 要求）。注：本期**开发与手工验证在生产库 qa_metadata**（5433/qa_metadata），自动化测试**仍然**用 qa_metadata_test（5433/qa_metadata_test），两者通过 alembic 迁移脚本同步；详见 11.3。

### 10.2 前端测试（Vitest + RTL + MSW）

见 8.4 文件清单。

### 10.3 E2E（Playwright）

`frontend/e2e/auth-flow.spec.ts` 7 步：
1. 访问 /chat → 重定向到 /login?from=/chat
2. 输入 admin/Admin@123 → 登录 → 跳 /change-password（mustChangePassword=true）
3. 修改密码成功 → 跳 /login → 重新登录 → 跳 /chat
4. 右上角点击 UserMenu → 看到"个人信息"/"修改密码"/"登出" 三项
5. 点击"个人信息" → /profile 渲染字段
6. 退出登录 → 跳 /login
7. 改密后旧 token 调任意 API → 401 + 自动跳 /login

## 11. 部署 / 迁移影响

### 11.1 数据库（生产库 `qa_metadata` 直接开发）

按 `Harness/rules/数据库环境使用规范.md`：表结构/迁移直接 prod，先 `pg_dump` 备份。

```bash
# 1. 备份
docker exec qa-postgres pg_dump -U postgres -t users -t user_sessions \
  qa_metadata > /backup/qa_metadata_pre_auth_<YYYYMMDD>.sql

# 2. 应用迁移（在生产库上直接 alembic upgrade head）
docker exec qa-backend alembic upgrade head   # 应用 0048

# 3. dev 环境跑一次性脚本初始化现有用户密码
docker exec qa-backend python -m backend.scripts.seed_user_passwords \
  --output /tmp/initial_passwords.csv
# → admin CSV 文件通过加密渠道分发，阅后即焚
```

**生产部署清单**：
- [ ] 备份 users 表
- [ ] K8s Secret 注入 `JWT_SECRET`（≥32 字节随机）
- [ ] K8s Secret 注入 `SEED_ADMIN_PASSWORD`（一次性，seed 后轮换）
- [ ] **生产库 0048 迁移执行**（在低峰期，pg_dump 备份在同事务外）
- [ ] 一次性 `seed_user_passwords.py` 跑（启用 `must_change_password=true`）
- [ ] 通知所有用户走"忘记密码"流（或分发初始密码）
- [ ] 首次启动后立即修改 admin 密码
- [ ] Nginx / ingress 剥离 `Authorization` / `X-User-*` 头
- [ ] 验证 `AUTH_STUB_ENABLED=0`（生产）

### 11.2 前端

```bash
npm run test:coverage  # 必须 ≥ 80%
npm run build          # 必须无 TS 错误
```

### 11.3 测试库 vs 生产库（按用户约束）

- **生产库 `qa_metadata`（5433）**：本期开发、本地手工验证、0048 迁移目标
- **测试库 `qa_metadata_test`（5433）**：自动化测试（`TEST_DATABASE_URL`），与生产库共用 Alembic 链
- **本地手工验证**：用生产库 `qa_metadata`，跑 `seed_user_passwords.py` 后用 admin/Admin@123 登录；验证完恢复（必要时 truncate `user_sessions` 表，保留 `users` 表 0048 后的字段）

### 11.4 菜单 seed 重跑

```bash
docker exec qa-backend python -m backend.scripts.seed_menu_config
```

**断言红线必须更新**（详见 8.10）。

## 12. 风险与回滚

| 风险 | 概率 | 影响 | 缓解 / 回滚 |
|---|---|---|---|
| Alembic 0048 迁移失败 | 低 | 中 | 已在 backup；`alembic downgrade -1` 即可回退 |
| 现有用户无 password_hash 全部无法登录 | 高 | 中 | `seed_user_passwords.py` 一次性初始化；admin 用 admin 重置端点逐个设密 |
| JWT_SECRET 泄漏 | 低 | 高 | K8s Secret + 立即吊销所有 session（清表或加 `revoked_at`） |
| seed 红线断言漏改 | 中 | 低 | 测试必跑；PR 卡门禁 |
| 旧前端缓存 | 中 | 低 | 部署时强制刷新 / `Cache-Control: no-cache` |
| 现有 admin 页面依赖 stub 头被外部直接调用 | 中 | 中 | 文档化：生产 stub 必须关；Nginx 剥离头 |
| localStorage XSS 窃 token | 低 | 中 | 已有 CSP；后续可加 httpOnly cookie 改造 |

**回滚步骤**：
1. `alembic downgrade -1`（drop user_sessions + 4 个 users 字段）
2. 还原 `seed_menu_config.py` 与 `test_seed_menu_config.py` 4 处断言
3. 还原 `frontend/src/config.ts` 的 `DEFAULT_USER_ID`
4. 前端 `npm run build` 部署上一个版本
5. 不影响 `users` 表老数据（NULL password_hash 不破坏）

## 13. SSOT 文档（落地后）

新增 `Harness/changes/feat-user-auth/` 目录：
- `summary.md`：背景、目标、范围
- `data-model.md`：0048 增量 + user_sessions 表
- `api.md`：6 个新端点
- `frontend.md`：LoginPage / UserMenu / RequireAuth / 路由
- `security.md`：9.1-9.9
- `testing.md`：10.1-10.3
- `runbook.md`：部署 + 回滚

## 14. 验收清单

**功能**：
- [ ] 现有用户全部能用 admin 重置端点设密后登录
- [ ] admin 首次登录 `must_change_password=true` 跳 `/change-password`
- [ ] 自己改密成功后旧 token 失效（401 + 自动跳 /login）
- [ ] admin 重置用户密码后该用户所有 session 失效
- [ ] 右上角 UserMenu 三项可见，mustChangePassword 时红点提示
- [ ] /profile 展示字段完整且不含 password_hash
- [ ] /change-password 字段校验与后端规则一致
- [ ] 登出后调用任意 API 触发 401 自动跳 /login

**质量门禁**：
- [ ] 后端测试 `pytest --cov=app --cov-fail-under=80` 通过
- [ ] 前端测试 `npm run test:coverage` ≥ 80%
- [ ] 菜单 seed 4 处断言全过
- [ ] `frontend_routes` 集合断言包含 /profile / /change-password
- [ ] Playwright e2e auth-flow 7 步全过
- [ ] i18n 完成检查清单：zh-CN 与 en-US 同步；`i18n:check` 通过
- [ ] code-reviewer / security-reviewer 双审无 CRITICAL/HIGH

**部署**：
- [ ] JWT_SECRET K8s Secret 就绪
- [ ] AUTH_STUB_ENABLED=0
- [ ] Nginx 头剥离配置就位
- [ ] 初始密码 CSV 走加密渠道分发并删除
- [ ] 监控告警：登录失败率突增、改密失败率、429 触发率

**生产库合规**：
- [ ] 备份文件就位且大小与原表一致
- [ ] 0048 迁移在生产库成功（`alembic current` 输出 `0048`）
- [ ] 一次性 `seed_user_passwords.py` 跑通；CSV 已阅后即焚
- [ ] 手工验证 admin/Admin@123 登录 + must_change_password 跳 /change-password

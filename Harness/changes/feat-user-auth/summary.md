# 变更：feat-user-auth

- **日期**：2026-09-20
- **作者**：
- **Phase**：feat-user-auth（独立特性）
- **状态**：shipped

## 1. 需求

现状：后端 stub auth —— 前端发固定 `X-User-Id: system`，任何人都可伪造 admin
角色。已有 `users` 表（`password_hash / last_login_at / last_login_ip /
must_change_password` 4 列已声明在 ORM，DB 有迁移历史），无任何登录端点 /
JWT / session 记录。

需求：增加真实登录（用户名 + 密码 → JWT），按角色/组织授予菜单权限，
`AUTH_MODE=stub` 保留开发/测试免登，配置开关切换。

## 2. 设计决策（用户确认）

1. **stub 模式（`AUTH_MODE=stub`）**：X-User-Id 头仍能解析，后端 JWT 校验完全
   绕过，方便本地测试/自动化/集成测试直接指定用户。生产由反向代理（nginx）
   统一剥离 `Authorization` / `X-User-*` 头保证安全。
2. **Session 表**：写且验证。`getCurrentUser` 每次同步查 `sessions WHERE
   jti=xxx AND revoked_at IS NULL`。改密/admin 重置时 UPDATE `revoked_at`，
   旧 token 即时失效，无需等待 JWT TTL 自然过期。
3. **前端 Zustand persist**：token 写 localStorage（rememberMe=true）。
4. **bcrypt cost = 12**；JWT TTL = 3600s；JWT_SECRET 启动校验 ≥32 字节
   （`AUTH_MODE=real` 时）。
5. **防枚举**：登录失败统一 `MSG_INVALID_CREDENTIALS`，响应时间均匀（查无用户
   也跑 bcrypt + 200ms 延迟）。

## 3. 文件结构

### 后端（新增 / 修改）

| 文件 | 职责 |
|---|---|
| `backend/app/services/password_policy.py` | 密码策略（中策略≥8 位、字母+数字） |
| `backend/app/services/jwt_codec.py` | HS256 签发/校验（PyJWT） |
| `backend/app/services/auth_service.py` | login / me / logout / change_password / admin_reset_password |
| `backend/app/api/v1/auth.py` | 5 个端点 |
| `backend/app/api/v1/users.py` | 加 admin 重置密码端点 |
| `backend/app/dependencies.py` | `getCurrentUser` 加 Bearer 优先 + stub 回退；`AUTH_MODE` 开关 |
| `backend/app/config.py` | 加 `AUTH_MODE`/`JWT_SECRET`/... 11 个字段 |
| `backend/app/domain/error_messages.py` | 加 10 个 MSG_* |
| `backend/app/domain/exceptions.py` | 加 `AuthFailedError` + status 映射 |
| `backend/app/domain/models.py` | `AuditLog.action` 扩 VARCHAR(20→32) |
| `backend/app/models/rbac.py` | 加 `UserSession` ORM |
| `backend/app/services/audit_service.py` | 加 5 个 audit 事件 |
| `backend/app/main.py` + `backend/app/tests/_testapp.py` | 注册 auth router |
| `backend/alembic/versions/0082_user_auth_tables.py` | `user_sessions` 表 + `users.must_change_password` 部分索引 |
| `backend/alembic/versions/0083_audit_log_auth_actions.py` | 扩 action 列 + 加 CK |
| `backend/scripts/seed_rbac.py` | admin 设 bcrypt 密码 + `must_change_password=true` |
| `backend/scripts/seed_user_passwords.py` | 一次性给无密码用户生成临时密码 → CSV |

### 前端（新增 / 修改）

| 文件 | 职责 |
|---|---|
| `frontend/src/types/auth.ts` | DTO 类型 |
| `frontend/src/stores/authStore.ts` | Zustand+persist |
| `frontend/src/api/auth.ts` | 5 个端点调用 + admin reset |
| `frontend/src/api/authHeaders.ts` | 裸 axios 调用的头注入 helper |
| `frontend/src/api/client.ts` | 移除 `X-User-Id`，Authorization 注入，401 拦截清 store |
| `frontend/src/config.ts` | 移除 `DEFAULT_USER_ID` 导出 |
| `frontend/src/components/common/RequireAuth.tsx` | 路由守卫 |
| `frontend/src/components/common/UserMenu.tsx` | Header 右侧 Dropdown |
| `frontend/src/components/common/AppLayout.tsx` | Header 加 UserMenu |
| `frontend/src/pages/LoginPage.tsx` | 独立登录页 |
| `frontend/src/pages/ProfilePage.tsx` | 改用 `/auth/me` |
| `frontend/src/pages/ChangePasswordPage.tsx` | 改密页 |
| `frontend/src/App.tsx` | /login 独立 + RequireAuth 包裹 + /change-password |
| `frontend/src/i18n/zh-CN.ts` / `en-US.ts` | 加 auth.* / userMenu.* |

### 测试

| 文件 | 覆盖 |
|---|---|
| `backend/app/tests/unit/test_password_policy.py` | 13 单元 |
| `backend/app/tests/unit/test_jwt_codec.py` | 8 单元 |
| `backend/app/tests/integration/test_get_current_user_bearer.py` | 6 集成 |
| `backend/app/tests/integration/test_auth_endpoints.py` | 14 集成 |
| `backend/app/tests/integration/test_admin_reset_password.py` | 5 集成 |
| `backend/app/tests/integration/test_audit_auth_events.py` | 5 集成 |
| `frontend/src/tests/client.test.ts` | 6 单元 |
| `frontend/src/tests/App.test.tsx` | 1 路由 |
| `frontend/src/tests/ProfilePage.test.tsx` | 2 单元 |

**合计**：51 后端 + 9 前端 = 60 测试全绿（不含前端回归：EntityMappingPage
预存失败 1 个，与本特性无关）。

## 4. 数据迁移

```bash
cd backend && alembic upgrade head
```

两库同步执行：prod（`qa_metadata`）+ test（`qa_metadata_test`）。

| 迁移 | 内容 |
|---|---|
| `0082_user_auth_tables.py` | `user_sessions` 表（id/jti UNIQUE/user_id FK CASCADE/issued_at/expires_at/revoked_at/revoked_reason/ip/user_agent/created_time）+ `users.must_change_password` 部分索引 |
| `0083_audit_log_auth_actions.py` | `audit_log.action` VARCHAR(20→32) + 5 个新 auth action 的 CHECK 约束 |

## 5. 部署清单

### 后端环境变量

| 变量 | 默认 | 必需 | 说明 |
|---|---|---|---|
| `AUTH_MODE` | `stub` | 否 | `stub`（开发/测试免登）/`real`（强制 JWT） |
| `AUTH_STUB_ENABLED` | `true` | 否 | stub 模式是否启用（细粒度开关） |
| `ALLOW_STUB_WHEN_REAL` | `false` | 否 | 真模式下放行 stub 头（仅 local/staging） |
| `JWT_SECRET` | `""` | **real 模式必填** | ≥32 字节；启动校验 |
| `JWT_ALGORITHM` | `HS256` | 否 | 签法白名单 |
| `JWT_TTL_SECONDS` | `3600` | 否 | JWT 过期秒数 |
| `JWT_ISSUER` | `qa-system` | 否 | iss claim |
| `JWT_AUDIENCE` | `qa-system-web` | 否 | aud claim |
| `BCRYPT_ROUNDS` | `12` | 否 | bcrypt cost |
| `AUTH_MIN_DELAY_MS` | `200` | 否 | 登录失败等长延迟（防时间侧信道） |
| `SEED_ADMIN_PASSWORD` | `Admin@123` | 否 | admin seed 用的初始密码 |

### Nginx 反向代理（生产）

```nginx
# 剥离客户端伪造的身份头 —— 防止绕过登录直接冒名 admin
# **Authorization 保留**：那是登录后的合法 Bearer JWT，不能误杀。
# 仅剥离 X-User-* 冒充身份。
proxy_set_header X-User-Id "";
proxy_set_header X-User-Roles "";
proxy_set_header X-User-Departments "";
proxy_set_header X-Tenant-Id $http_x_tenant_id;  # 保留
```

dev/test：注释掉以上几行（保留客户端发 stub 头的本地调试能力）。

### Seed 一次性脚本

```bash
cd backend
AUTH_STUB_ENABLED=1 python -m scripts.seed_rbac
AUTH_STUB_ENABLED=1 python -m scripts.seed_user_passwords
# → user_temp_passwords.csv（阅后即焚）
```

生产环境（`AUTH_STUB_ENABLED=0`）必须显式 override：

```bash
AUTH_STUB_ENABLED=0 ALLOW_PROD_PASSWORD_SEED=1 python -m scripts.seed_user_passwords
```

## 6. 验收标准

1. `alembic upgrade head` 后 `user_sessions` 表存在（8 列 + 3 索引）
2. `audit_log.action` 列宽度 = VARCHAR(32)
3. admin 用户有 bcrypt 密码 + `must_change_password=true`
4. `POST /api/v1/auth/login` 200 → 返回 `accessToken` + `mustChangePassword=true`
5. `GET /api/v1/auth/me` + Bearer → 200
6. 401 → 拦截器清 store + 跳 /login
7. 改密成功 → 自动吊销所有 session → 跳 /login
8. admin 重置密码 → 目标用户所有 session 吊销
9. `auth.login / auth.login_failed / auth.logout / auth.password_changed /
   user.password_reset` 5 个 audit 事件正确写入
10. 登录失败响应时间均匀（200ms 防枚举）

## 7. 已知边界 / 不做

- **前端 sessionStorage 路径**：当前实现只用 localStorage；后续若要加
  `rememberMe=false` 走 sessionStorage，需要迁移避免多 tab 串号
- **OIDC / OAuth**：未集成；仅自建 username+password
- **多端点单点登录**：未实现；改密只吊销当前 DB session
- **密码重试次数 / 账号锁定**：未实现（依赖 bcrypt cost + JWT TTL 控制爆破成本）

## 8. 提交方式

按用户约束「我自己提交」，本特性所有 commit 由用户本人执行，不通过 AI 工具触发。

## 9. 部署复盘（2026-09-20，含 4 次前端 bundle 不生效事故）

**症状**：每次「前端代码改完让用户 hard refresh」后浏览器 console 仍报**几天前**的
bundle hash（如 `index-BMm9puG4.js`），伴随 `/api/v1/api/v1/menu-config 404`
（双层路径）、`[menu-config] fallback to static nav`（菜单扁平）等。

**误诊史（4 次失败循环）**：
1. 怀疑 nginx 配置 → 加 `proxy_set_header X-User-Id ""` 剥离客户端伪造头，**同时**误
   写了 `proxy_set_header Authorization ""` 把合法 Bearer JWT 也剥了 → 登录直接坏
2. 怀疑 backend bcrypt 缺失 → 重建镜像，OK
3. 怀疑 backend `JWT_SECRET` 未注入 → 补 .env，OK
4. 怀疑 alembic 迁移未进容器 → cp 0082/0083，OK
5. 怀疑 `fetchMenuConfig` 用了裸 fetch 绕过 axios 拦截器 → 改 `httpClient.get`，路径仍
   写 `/api/v1/menu-config` → 双层 `/api/v1/api/v1/menu-config`
6. 修路径为 `/menu-config`，再让用户刷新 —— **还是报错**

**真正根因（第 6 次才看清）**：
- `cd frontend && npm run build` 只把 `frontend/dist/` 写到**本地沙箱**
- 容器内 nginx serve 的 `/usr/share/nginx/html` 来自**镜像 bake 时** `COPY --from=build`
- `docker cp frontend/dist qa-frontend:/usr/share/nginx/html` 是临时覆盖，**重建容器即
  回退**（同类教训见 `fix-nginx-upstream-stale-ip`）
- 4 次「前端已重建」实际都是宿主机本地 build，**从未进入镜像**
- 用户浏览器看到的 `index-BMm9puG4.js` 是几天前 `docker compose build` bake 进去的，
  hard refresh 多少次都拿不到新 hash
- `VITE_API_BASE_URL=http://localhost:8000/api/v1` 也被 bake 进旧 bundle，**生产
  应该走 nginx 5173**（绕过 nginx 直连 8000 → nginx 剥离伪造头策略完全失效）

**正确部署命令**（一行）：
```bash
docker compose -f docker/docker-compose.yml build --no-cache frontend \
  && docker compose -f docker/docker-compose.yml up -d frontend
```

**诊断三件套**（任何「前端改完用户看不到」先跑）：
```bash
docker exec qa-frontend ls /usr/share/nginx/html/assets/ | grep index-.*\.js
ls frontend/dist/assets/ | grep index-.*\.js
curl -s http://localhost:5173/ | grep -oE 'index-[A-Za-z0-9_-]+\.js'
```
三个 hash 必须**完全一致**。不一致 = 镜像没重建或 build 没进容器。

**已沉淀的策略**：[开发流程规范 §前端变更部署](../rules/开发流程规范.md) 新增 5 条规则，
强制 `docker compose build --no-cache frontend` 而非本地 build + 临时 cp。

**留作 memory**：`qa-system-frontend-deploy-build-required`，每次涉及前端的特性
dispatch 时 fast-recall，避免再次陷入「本地 build 通过 = 用户能看到」的心智陷阱。

# feat-admin-user-password 总结

## 1. 背景与目标

**用户反馈**：「新建用户没有输入密码的地方；管理用户没有修改密码的功能」。

**目标**：admin 创建用户时能设初始密码；admin 能重置任意用户的密码。复用既有 `PUT /users/{id}/password` 端点。

## 2. 设计决策

### 决策 1：新建用户时的密码来源 = A. 必填明文密码

admin 在创建弹窗里输入明文密码，后端 bcrypt hash 入库 + `must_change_password=true`（下次登录强制改）。**理由**：admin 主动给初始密码通常因为知道是谁接账号，比随机临时密码更可控；随机临时密码在企业场景里 admin 还要用 IM/邮件转交，多一道泄露面。

### 决策 2：管理改密码的 UI 入口 = A. 复用创建/编辑弹窗

admin 点行内「编辑」时，编辑弹窗多一个密码输入框（留空 = 不修改密码）。**理由**：admin 改密码通常和改 display_name / enabled 是一组动作（用户离职、改密同步），独立按钮反而打断流程。

## 3. 文件改动清单

### 后端
- `backend/app/schemas/rbac.py` — `UserCreate.password: str` 必填字段（min_length=8, max_length=128）
- `backend/app/services/identity_service.py` — `create_user` 加服务端 `validate_password` 校验 + 写 bcrypt hash + `must_change_password=True`
- `backend/app/tests/integration/test_user_create_password.py`（新建，3 测试）
- `backend/app/tests/integration/test_identity_create_user_password.py`（新建，2 测试）
- `backend/app/tests/integration/test_admin_user_password.py`（新建，4 测试 + `_rbacSeeds` autouse fixture）

### 前端
- `frontend/src/types/rbac.ts` — `UserCreatePayload.password` 必填 + 新 `AdminResetPasswordPayload`
- `frontend/src/api/users.ts` — 新 `adminResetPassword(userId, payload)` 函数
- `frontend/src/pages/AdminUsersPage.tsx` — 创建/编辑表单加 `<Input.Password>` 字段（创建必填、编辑可选）；`onSubmit` 编辑分支填密码额外调 `adminResetPassword`
- `frontend/src/i18n/zh-CN.ts` — `rbac.user.password/passwordPlaceholder/passwordStrengthHint` 3 keys
- `frontend/src/i18n/en-US.ts` — 同上 3 keys
- `frontend/src/tests/rbac.password.test.ts`（新建，2 测试）
- `frontend/src/tests/users.api.password.test.ts`（新建，1 mock 测试）
- `frontend/src/tests/AdminUsersPage.password.test.tsx`（新建，4 测试）

## 4. 接口契约

### POST /api/v1/users
请求体新增字段：`password: str`（min_length=8）。
- 缺 password → 422（schema ValidationError）
- 弱 password（无字母或无数字）→ 422（服务端 `validate_password` 抛 ValidationError）
- 创建成功后 `users.password_hash` = bcrypt(value, rounds=12)；`must_change_password` = true

### PUT /api/v1/users/{user_id}/password
既有端点，本次未改动 schema（AdminResetPasswordRequest 保持）。admin 重置后目标用户所有 session 即时吊销。

## 5. 数据迁移

无 alembic 迁移：`users.password_hash` 与 `users.must_change_password` 列在更早的 feat-user-auth 已存在（migrations 0082 + 0083）。

## 6. 测试报告

| 测试套 | 数量 | 结果 |
|---|---|---|
| `test_user_create_password.py`（Task 1） | 3 | passed 2.60s |
| `test_identity_create_user_password.py`（Task 2） | 2 | passed 2.92s |
| `test_admin_user_password.py`（Task 3） | 4 | passed 5.52s |
| `rbac.password.test.ts`（Task 4） | 2 | passed + tsc clean |
| `users.api.password.test.ts`（Task 5） | 1 | passed |
| `AdminUsersPage.password.test.tsx`（Task 6） | 4 | passed |
| **合计直接相关** | **16** | **全绿** |

全量回归（Task 7）：
- 前端 vitest 1159/1165（与 Task 6 baseline 完全一致，6 预存失败与本任务无关）
- 后端直接相关 9 测试全绿
- 后端 `test_auth_endpoints.py` 7 失败为**预存** JWT_SECRET 环境问题（conftest autouse fixture + `getSettings` `@lru_cache` 缓存未刷新），与本任务无关

## 7. 已知边界 / 不做

- **edit 时密码字段不预先填值**：用户编辑时输入 password 才会触发 `adminResetPassword`；留空保持原密码不动（admin 看不到当前 hash 明文，UI 也不暴露 forceChangeOnNextLogin 开关）
- **密码强度客户端软提示**：schema min_length=8 是硬校验；强度提示「至少 8 位，须含字母与数字」是 extra 文本，不阻塞流程；服务端 `validate_password` 是真正的强度闸
- **`MSG_PASSWORD_TOO_WEAK` 常量未导入**：因为 `validate_password` 返回的是 i18n key 而非 user-facing 字符串，fallback 写死中文「密码至少 8 位且必须包含字母和数字」——follow-up 应改 `password_policy.validate_password` 契约返回 `""` 让 callers 用常量
- **stub 模式下也可走真密码**：admin endpoint 不受 AUTH_MODE 开关影响（`getAdminOnlyActor` 仍要求 X-User-Id 命中 DB admin 用户），参考 `test_rbac_api.py` 模式

## 8. 提交方式

按用户约束「我自己提交」，本特性所有 commit 由用户本人执行，不通过 AI 工具触发。

## 9. 部署复盘

**前端部署**（无后端 schema 变化，监听 8000 端口）：
- 本特性只动前端代码 + 后端 schema 字段（已部署到 qa-backend 镜像的运行容器）
- **前端镜像**必须 rebuild：`docker compose -f docker/docker-compose.yml build --no-cache frontend && docker compose -f docker/docker-compose.yml up -d frontend`
- 详细诊断三件套与为何本地 build ≠ 容器镜像见 `Harness/rules/开发流程规范.md §前端变更部署` + memory `qa-system-frontend-deploy-build-required`

**后端变更**：UserCreate.password 字段 + create_user 写 hash 都是 Python 代码变更，**必须**重建后端镜像：
- `docker compose -f docker/docker-compose.yml build --no-cache backend && docker compose -f docker/docker-compose.yml up -d backend`
- alembic 迁移无新增（0082/0083 已应用）

**真机验收**：
1. admin 登录后访问「用户管理」→ 点「新建用户」→ 应见「密码」必填字段
2. 填新用户名 + 初始密码 + 提交 → 新用户能凭此密码登录（首次登录会强制改密）
3. 编辑某用户 → 输入新密码 → 提交 → 该用户当前 session 即时吊销，新密码生效
4. 编辑某用户 → 不填密码 → 提交 → 用户密码不变
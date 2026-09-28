# feat-admin-user-password Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** admin 创建用户时必填明文密码（自动 hash 入库 + must_change_password=true），编辑用户时可填密码触发服务端重置（走已有 `PUT /users/{id}/password`），不填则不动密码。

**Architecture:** 后端扩 `UserCreate.password` 必填字段 + `IdentityService.create_user` 写 bcrypt hash + `must_change_password=True`；前端 `AdminUsersPage` 创建/编辑表单加密码 `Input.Password` 字段（创建必填、编辑可选），提交时分流——编辑路径若填了密码额外走 `PUT /users/{id}/password`。所有密码策略统一复用 `validate_password()`（minLength=8 + letter + digit）。

**Tech Stack:** FastAPI + Pydantic v2 + SQLAlchemy async + bcrypt；React + antd Form + zustand authStore + react-i18next；pytest + vitest。

## Global Constraints

- 后端测试用真实 PostgreSQL `qa_metadata_test`（via `TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test`）；禁用 sqlite mock-only。详见 `Harness/rules/测试规范.md`。
- TDD：RED → GREEN → IMPROVE；覆盖率 ≥80%。
- 用户硬约束「我自己提交」——AI 不执行 `git commit`，仅写代码 + 跑测试 + 报告。
- bcrypt rounds = 12（`settings.bcryptRounds`，已存在）。
- 密码策略：`validate_password()` 在 `app/services/auth_service.py:324`：minLength=8 + 必须含字母 + 必须含数字（服务端强校验，前端软提示）。
- 网络契约：snake_case 后端 → camelCase（`UserCreate.password` → JSON key `password`）。
- `must_change_password` 默认 True（admin 创建 = 给初始密码，下次登录强制改）。
- 前端 i18n 必须双写 zh-CN 和 en-US（不允许硬编码中文/英文字面量）。
- 前端部署：代码写完后**不**执行 `docker compose build`（遵循 `Harness/rules/开发流程规范.md §前端变更部署`——用户自己 rebuild）。

---

## File Structure

| 文件 | 改动 |
|---|---|
| `backend/app/schemas/rbac.py` | `UserCreate.password` 必填字段（min_length=8） |
| `backend/app/services/identity_service.py` | `create_user` 写 `password_hash` + `must_change_password=True` |
| `backend/app/api/v1/users.py` | 不改（`PUT /users/{id}/password` 已存在，复用 `AuthService.admin_reset_password`） |
| `frontend/src/types/rbac.ts` | `UserCreatePayload.password` 必填 + 新 `AdminResetPasswordPayload` |
| `frontend/src/api/users.ts` | 加 `adminResetPassword(userId, payload)` |
| `frontend/src/pages/AdminUsersPage.tsx` | 创建/编辑表单加密码字段（创建必填、编辑可选）+ 提交流程分支 |
| `frontend/src/i18n/zh-CN.ts` | 加 `rbac.user.password*` 文案 |
| `frontend/src/i18n/en-US.ts` | 同步加 |
| `backend/app/tests/integration/test_admin_user_password.py` | 新建：覆盖 4 路径 |
| `frontend/src/tests/AdminUsersPage.password.test.tsx` | 新建：覆盖 3 路径 |
| `Harness/changes/feat-admin-user-password/summary.md` | SSOT 9 段（按 `变更记录强制规范 §5`） |

---

## Task 1: 后端 UserCreate schema 加 password 字段

**Files:**
- Modify: `backend/app/schemas/rbac.py:23-29`

**Interfaces:**
- Consumes: 无
- Produces: `UserCreate.password: str`（min_length=8，max_length=128）

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/integration/test_user_create_password.py`：

```python
import pytest
from app.schemas.rbac import UserCreate
from pydantic import ValidationError


def test_user_create_requires_password():
    """新建用户必须带 password 字段（min_length=8）。"""
    with pytest.raises(ValidationError) as exc:
        UserCreate(username="alice", displayName="Alice")
    # 至少有一个 error 指向 password 字段
    errors = exc.value.errors()
    assert any(e["loc"] == ("password",) for e in errors)


def test_user_create_password_too_short():
    with pytest.raises(ValidationError) as exc:
        UserCreate(username="alice", displayName="Alice", password="short")
    errors = exc.value.errors()
    assert any(e["loc"] == ("password",) for e in errors)


def test_user_create_password_ok():
    u = UserCreate(username="alice", displayName="Alice", password="ValidPass1")
    assert u.password == "ValidPass1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pass@localhost:5433/qa_metadata_test pytest app/tests/integration/test_user_create_password.py -v`
Expected: 3 个 FAIL（`UserCreate.password` 字段不存在）

- [ ] **Step 3: 实现 UserCreate.password**

改 `backend/app/schemas/rbac.py:23-29`：

```python
class UserCreate(CamelModel):
    """新增用户。username 不可变（作为 X-User-Id 映射键）。
    password 必填——admin 创建时必须给初始密码（feat-admin-user-password）。
    """

    username: str = Field(min_length=1, max_length=64, pattern=_USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    email: str | None = Field(default=None, max_length=255)
    enabled: bool = True
    password: str = Field(min_length=8, max_length=128)
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2 命令
Expected: 3 passed

- [ ] **Step 5: 不 commit（用户自己提交）**

---

## Task 2: IdentityService.create_user 写 password_hash

**Files:**
- Modify: `backend/app/services/identity_service.py:85-110`

**Interfaces:**
- Consumes: `UserCreate.password`（来自 Task 1）
- Produces: `User` 行带 `password_hash` (bcrypt) + `must_change_password=True`

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/integration/test_identity_create_user_password.py`：

```python
import pytest
import bcrypt
from app.services.identity_service import IdentityService
from app.schemas.rbac import UserCreate
from app.dependencies import CurrentUser
from app.domain.exceptions import ValidationError


@pytest.mark.asyncio
async def test_create_user_hashes_password(dbSession):
    """create_user 必须写 password_hash（bcrypt 不可逆）+ must_change_password=True。"""
    actor = CurrentUser(userId="admin", tenantId="default", roles=("admin",), departments=())
    svc = IdentityService()
    plain = "ValidPass1"
    row = await svc.create_user(
        dbSession,
        UserCreate(username="alice", displayName="Alice", password=plain),
        actor,
    )
    await dbSession.commit()

    assert row.password_hash is not None
    assert row.password_hash != plain  # 不存明文
    # bcrypt 验证可解
    assert bcrypt.checkpw(plain.encode(), row.password_hash.encode())
    assert row.must_change_password is True  # 强制下次登录改密


@pytest.mark.asyncio
async def test_create_user_password_too_weak_raises(dbSession):
    actor = CurrentUser(userId="admin", tenantId="default", roles=("admin",), departments=())
    svc = IdentityService()
    # min_length=8 通过 schema 校验，但 validate_password 要求字母+数字
    # 8 字符无字母 "12345678" → 服务端 validate_password 应拒
    with pytest.raises(ValidationError):
        await svc.create_user(
            dbSession,
            UserCreate(username="bob", displayName="Bob", password="12345678"),
            actor,
        )
```

> 注：conftest 提供的 fixture 名是 **`dbSession`**（camelCase）而非 `db_session`。`CurrentUser` 在 `app/dependencies.py` 直接构造，不需要 `buildActor` 辅助函数。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pass@localhost:5433/qa_metadata_test pytest app/tests/integration/test_identity_create_user_password.py -v`
Expected: 2 FAIL（`create_user` 不处理 password）

- [ ] **Step 3: 实现 create_user 写 hash**

改 `backend/app/services/identity_service.py:85-110`：

```python
async def create_user(
    self, session: AsyncSession, dto: UserCreate, actor: CurrentUser
) -> User:
    from app.services.auth_service import _hash_password, validate_password
    from app.config import getSettings
    from app.domain.exceptions import ValidationError
    from app.domain.error_messages import MSG_PASSWORD_TOO_WEAK

    # 服务端强校验（前端软提示已通过 schema min_length=8，复杂策略服务端再校验一遍）
    ok, err = validate_password(dto.password)
    if not ok:
        raise ValidationError(err or MSG_PASSWORD_TOO_WEAK)

    settings = getSettings()
    row = User(
        username=dto.username,
        display_name=dto.display_name,
        email=dto.email,
        enabled=dto.enabled,
        password_hash=_hash_password(dto.password, settings.bcryptRounds),
        must_change_password=True,  # 初始密码强制下次登录改
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as e:
        await session.rollback()
        if "uq_users_username" in str(e.orig):
            raise ConflictError(f"用户名已存在: {dto.username}")
        raise
    await self._outbox.enqueue(
        session,
        event_type="user_created",
        entity_type="user",
        entity_id=row.id,
        actor=actor.userId,
        payload={"before": None, "after": _user_payload(row)},
    )
    return row
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2 命令
Expected: 2 passed

- [ ] **Step 5: 不 commit（用户自己提交）**

---

## Task 3: 合并后端集成测试到 test_admin_user_password.py

**Files:**
- Create: `backend/app/tests/integration/test_admin_user_password.py`

**Interfaces:**
- Consumes: Task 1（schema）+ Task 2（service）+ 既有 `AuthService.admin_reset_password`
- Produces: 4 个集成测试覆盖完整流程

- [ ] **Step 1: 写 4 个测试覆盖端到端流程**

```python
"""feat-admin-user-password：admin 创建用户密码 + 重置密码端到端集成测试。

全链路真实 PG (TEST_DATABASE_URL=qa_metadata_test)；通过 X-User-* stub 头走 admin。
复用 test_rbac_api.py 已定义的 AUTH_ADMIN 常量（X-User-Id=admin + X-User-Roles=admin）。
"""
import pytest
import bcrypt
from httpx import AsyncClient
from sqlalchemy import select

from app.models.rbac import User

AUTH_ADMIN = {"X-User-Id": "admin", "X-User-Roles": "admin"}


@pytest.mark.asyncio
async def test_admin_create_user_with_password_writes_hash(client: AsyncClient, dbSession):
    """POST /users 带 password → 201 → password_hash 写入 + must_change_password=True。"""
    resp = await client.post(
        "/api/v1/users",
        json={"username": "alice", "displayName": "Alice", "password": "ValidPass1"},
        headers=AUTH_ADMIN,
    )
    assert resp.status_code == 201

    row = (await dbSession.execute(select(User).where(User.username == "alice"))).scalar_one()
    assert row.password_hash is not None
    assert bcrypt.checkpw(b"ValidPass1", row.password_hash.encode())
    assert row.must_change_password is True


@pytest.mark.asyncio
async def test_admin_create_user_missing_password_returns_422(client: AsyncClient):
    resp = await client.post(
        "/api/v1/users",
        json={"username": "bob", "displayName": "Bob"},  # 无 password
        headers=AUTH_ADMIN,
    )
    assert resp.status_code == 422
    assert any("password" in str(e.get("loc", [])) for e in resp.json().get("detail", []))


@pytest.mark.asyncio
async def test_admin_reset_password_works(client: AsyncClient, dbSession):
    """PUT /users/{id}/password → 204 → hash 更新 + must_change=True。"""
    create = await client.post(
        "/api/v1/users",
        json={"username": "carol", "displayName": "Carol", "password": "OldPass123"},
        headers=AUTH_ADMIN,
    )
    assert create.status_code == 201
    user_id = create.json()["id"]

    reset = await client.put(
        f"/api/v1/users/{user_id}/password",
        json={"newPassword": "NewPass456", "forceChangeOnNextLogin": True},
        headers=AUTH_ADMIN,
    )
    assert reset.status_code == 204

    row = (await dbSession.execute(select(User).where(User.id == user_id))).scalar_one()
    assert bcrypt.checkpw(b"NewPass456", row.password_hash.encode())
    assert not bcrypt.checkpw(b"OldPass123", row.password_hash.encode())
    assert row.must_change_password is True


@pytest.mark.asyncio
async def test_admin_reset_password_weak_password_rejected(client: AsyncClient, dbSession):
    create = await client.post(
        "/api/v1/users",
        json={"username": "dave", "displayName": "Dave", "password": "ValidPass1"},
        headers=AUTH_ADMIN,
    )
    user_id = create.json()["id"]

    reset = await client.put(
        f"/api/v1/users/{user_id}/password",
        json={"newPassword": "short", "forceChangeOnNextLogin": False},
        headers=AUTH_ADMIN,
    )
    assert reset.status_code in (400, 422)  # 取决于 ValidationError → 400 还是 schema → 422
```

> 注：fixture 名是 `dbSession`/`client`（camelCase，conftest.py 已定义）。admin 端点必须带 AUTH_ADMIN header 才能过 `getAdminOnlyActor` 校验。

- [ ] **Step 2: 跑测试**

Run: `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pass@localhost:5433/qa_metadata_test pytest app/tests/integration/test_admin_user_password.py -v`
Expected: 4 passed

- [ ] **Step 3: 不 commit（用户自己提交）**

---

## Task 4: 前端 rbac 类型加 password

**Files:**
- Modify: `frontend/src/types/rbac.ts:21-32`

- [ ] **Step 1: 写失败测试**

新建 `frontend/src/tests/rbac.password.test.ts`：

```typescript
import type { UserCreatePayload, AdminResetPasswordPayload } from "../types/rbac";

// 仅类型层断言：编译期校验
describe("rbac types include password fields", () => {
  it("UserCreatePayload.password is required", () => {
    const payload: UserCreatePayload = {
      username: "alice",
      displayName: "Alice",
      password: "ValidPass1",
    };
    expect(payload.password).toBe("ValidPass1");
  });

  it("AdminResetPasswordPayload has newPassword + forceChangeOnNextLogin", () => {
    const payload: AdminResetPasswordPayload = {
      newPassword: "NewPass456",
      forceChangeOnNextLogin: true,
    };
    expect(payload.newPassword).toBe("NewPass456");
    expect(payload.forceChangeOnNextLogin).toBe(true);
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/tests/rbac.password.test.ts`
Expected: FAIL（types 不导出 password）

- [ ] **Step 3: 扩类型**

改 `frontend/src/types/rbac.ts:21-32`：

```typescript
export interface UserCreatePayload {
    username: string;
    displayName: string;
    email?: string | null;
    enabled?: boolean;
    /** admin 创建必填（feat-admin-user-password，2026-09-20）。 */
    password: string;
}

export interface UserUpdatePayload {
    displayName?: string;
    email?: string | null;
    enabled?: boolean;
}

export interface AdminResetPasswordPayload {
    newPassword: string;
    forceChangeOnNextLogin: boolean;
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2
Expected: 2 passed

- [ ] **Step 5: 不 commit**

---

## Task 5: 前端 users.ts 加 adminResetPassword API

**Files:**
- Modify: `frontend/src/api/users.ts`

- [ ] **Step 1: 写失败测试**

新建 `frontend/src/tests/users.api.password.test.ts`：

```typescript
import { adminResetPassword } from "../api/users";
import { httpClient } from "../api/client";

vi.mock("../api/client", () => ({
  httpClient: { put: vi.fn() },
}));

describe("adminResetPassword", () => {
  it("PUT /users/{id}/password with newPassword + forceChangeOnNextLogin", async () => {
    (httpClient.put as ReturnType<typeof vi.fn>).mockResolvedValue({ status: 204 });
    await adminResetPassword(42, { newPassword: "NewPass456", forceChangeOnNextLogin: true });
    expect(httpClient.put).toHaveBeenCalledWith(
      "/users/42/password",
      { newPassword: "NewPass456", forceChangeOnNextLogin: true },
    );
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/tests/users.api.password.test.ts`
Expected: FAIL（`adminResetPassword` 未导出）

- [ ] **Step 3: 加 API**

改 `frontend/src/api/users.ts`（在 `getUserEffectivePermissions` 前插入）：

```typescript
import type {
  UserEffectivePermissions,
  UserRow,
  UserCreatePayload,
  UserUpdatePayload,
  RoleIdsUpdatePayload,
  OrganizationIdsUpdatePayload,
  MenuCodesUpdatePayload,
  AdminResetPasswordPayload,
} from "../types/rbac";

// ... 既有代码 ...

/** admin 重置用户密码（feat-admin-user-password）。
 * 走已有 PUT /users/{id}/password；后端自动吊销目标用户所有 session。 */
export async function adminResetPassword(
  userId: number,
  payload: AdminResetPasswordPayload,
): Promise<void> {
  await httpClient.put(`${PREFIX}/${userId}/password`, payload);
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2
Expected: 1 passed

- [ ] **Step 5: 不 commit**

---

## Task 6: 前端 AdminUsersPage 加密码字段

**Files:**
- Modify: `frontend/src/pages/AdminUsersPage.tsx`
- Modify: `frontend/src/i18n/zh-CN.ts`
- Modify: `frontend/src/i18n/en-US.ts`

**Interfaces:**
- Consumes: `UserCreatePayload.password`（Task 4）+ `adminResetPassword`（Task 5）
- Produces: 创建弹窗新增密码必填字段；编辑弹窗新增密码可选字段；保存时若编辑 + 填了密码 → 额外调 `adminResetPassword`

- [ ] **Step 1: 写失败测试**

新建 `frontend/src/tests/AdminUsersPage.password.test.tsx`（覆盖 3 路径）：

```typescript
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "../i18n/i18n";
import AdminUsersPage from "../pages/AdminUsersPage";

vi.mock("../api/users", () => ({
  listUsers: vi.fn().mockResolvedValue([]),
  createUser: vi.fn(),
  updateUser: vi.fn(),
  deleteUser: vi.fn(),
  setUserRoles: vi.fn(),
  setUserOrganizations: vi.fn(),
  setUserPermissions: vi.fn(),
  getUserEffectivePermissions: vi.fn(),
  adminResetPassword: vi.fn(),
}));
vi.mock("../api/roles", () => ({ listRoles: vi.fn().mockResolvedValue([]) }));
vi.mock("../api/organizations", () => ({ listOrganizations: vi.fn().mockResolvedValue([]) }));
vi.mock("../api/menuConfig", () => ({ listMenuRows: vi.fn().mockResolvedValue([]) }));

const wrap = (children: React.ReactNode) =>
  <I18nextProvider i18n={i18n}>{children}</I18nextProvider>;

describe("AdminUsersPage password fields", () => {
  beforeEach(() => vi.clearAllMocks());

  it("create modal shows required password field", async () => {
    render(wrap(<AdminUsersPage />));
    fireEvent.click(screen.getByRole("button", { name: /新建用户/ }));
    // 密码 label 应存在（用 getAllByText 兼容多次出现的字符串）
    expect(screen.getAllByText(/^密码$|Password/).length).toBeGreaterThan(0);
    // 找到 password input（antd Input.Password 渲染为 type="password"）
    const passwordInputs = document.querySelectorAll('input[type="password"]');
    expect(passwordInputs.length).toBeGreaterThan(0);
  });

  it("submit without password shows validation error", async () => {
    const { createUser } = await import("../api/users");
    render(wrap(<AdminUsersPage />));
    fireEvent.click(screen.getByRole("button", { name: /新建用户/ }));
    // 填 username + displayName，不填 password
    fireEvent.change(screen.getByLabelText(/用户名|Username/, { selector: "input" }), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText(/显示名|Display Name/, { selector: "input" }), {
      target: { value: "Alice" },
    });
    fireEvent.click(screen.getByRole("button", { name: /确\s*定|OK/ }));
    await waitFor(() => {
      expect(createUser).not.toHaveBeenCalled();  // 应被前端校验拒
    });
  });

  it("edit with filled password triggers adminResetPassword", async () => {
    const { adminResetPassword, updateUser, listUsers } = await import("../api/users");
    (listUsers as ReturnType<typeof vi.fn>).mockResolvedValue([
      { id: 1, username: "alice", displayName: "Alice", email: null, enabled: true,
        roleIds: [], roleCodes: [], organizationIds: [], organizationCodes: [],
        createdTime: "2026-01-01", updatedTime: null },
    ]);
    render(wrap(<AdminUsersPage />));
    await waitFor(() => expect(screen.getByText("alice")).toBeTruthy());
    // 点编辑
    fireEvent.click(screen.getByRole("button", { name: /^编辑|Edit$/ }));
    // 填密码
    fireEvent.change(screen.getByLabelText(/^密码$|Password/, { selector: "input" }), {
      target: { value: "NewPass456" },
    });
    // 提交
    fireEvent.click(screen.getByRole("button", { name: /确\s*定|OK/ }));
    await waitFor(() => {
      expect(updateUser).toHaveBeenCalled();
      expect(adminResetPassword).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ newPassword: "NewPass456", forceChangeOnNextLogin: true }),
      );
    });
  });

  it("edit without password does NOT call adminResetPassword", async () => {
    const { adminResetPassword, updateUser, listUsers } = await import("../api/users");
    (listUsers as ReturnType<typeof vi.fn>).mockResolvedValue([
      { id: 1, username: "alice", displayName: "Alice", email: null, enabled: true,
        roleIds: [], roleCodes: [], organizationIds: [], organizationCodes: [],
        createdTime: "2026-01-01", updatedTime: null },
    ]);
    render(wrap(<AdminUsersPage />));
    await waitFor(() => expect(screen.getByText("alice")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /^编辑|Edit$/ }));
    // 不填密码
    fireEvent.click(screen.getByRole("button", { name: /确\s*定|OK/ }));
    await waitFor(() => expect(updateUser).toHaveBeenCalled());
    expect(adminResetPassword).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/tests/AdminUsersPage.password.test.ts`
Expected: 4 FAIL

- [ ] **Step 3: 加 i18n key**

`frontend/src/i18n/zh-CN.ts` 在 `rbac.user.*` 段（约 line 171）加：

```typescript
user: {
  // ... 既有 key ...
  password: "密码",
  passwordPlaceholder: "编辑时留空表示不修改密码",
  passwordStrengthHint: "至少 8 位，须含字母与数字",
},
```

`frontend/src/i18n/en-US.ts` 同步加：

```typescript
user: {
  // ... 既有 key ...
  password: "Password",
  passwordPlaceholder: "Leave blank to keep current password",
  passwordStrengthHint: "Min 8 chars, must contain letter and digit",
},
```

- [ ] **Step 4: 改 AdminUsersPage**

`frontend/src/pages/AdminUsersPage.tsx`：

**(a)** import：`adminResetPassword` 加入 import 行 31-40 区域：

```typescript
import {
    listUsers,
    createUser,
    updateUser,
    deleteUser,
    setUserRoles,
    setUserOrganizations,
    setUserPermissions,
    getUserEffectivePermissions,
    adminResetPassword,  // 新增（feat-admin-user-password）
} from "../api/users";
```

**(b)** `onSubmit`（line 117-166）改成：

```typescript
const onSubmit = async (values: Record<string, unknown>) => {
    setSaving(true);
    try {
        const roleIds = (values.roleIds as number[] | undefined) ?? [];
        const organizationIds =
            (values.organizationIds as number[] | undefined) ?? [];
        const newPassword = values.password as string | undefined;

        if (editing) {
            const payload: UserUpdatePayload = {
                displayName: values.displayName as string,
                email: values.email as string | undefined,
                enabled: values.enabled as boolean,
            };
            await updateUser(editing.id, payload);
            // 密码可选：填了才走 adminResetPassword（feat-admin-user-password）
            if (newPassword && newPassword.trim()) {
                await adminResetPassword(editing.id, {
                    newPassword: newPassword.trim(),
                    forceChangeOnNextLogin: true,
                });
            }
            await setUserRoles(editing.id, { roleIds });
            await setUserOrganizations(editing.id, { organizationIds });
            message.success(t("rbac.messages.updated"));
            setModalOpen(false);
        } else {
            // 创建：password 必填（前端校验已通过 schema 必填规则）
            const payload: UserCreatePayload = {
                username: values.username as string,
                displayName: values.displayName as string,
                email: values.email as string | undefined,
                enabled: (values.enabled as boolean | undefined) ?? true,
                password: newPassword ?? "",  // schema 必填，逻辑层确保非空
            };
            const created = await createUser(payload);
            try {
                await setUserRoles(created.id, { roleIds });
                await setUserOrganizations(created.id, { organizationIds });
                message.success(t("rbac.messages.created"));
            } catch (e) {
                const err = e as Error & { message?: string };
                message.error(
                    `${t("rbac.errors.partialCreated")}: ${err.message ?? String(e)}`,
                );
                setModalOpen(false);
                void fetchUsers();
                return;
            }
            setModalOpen(false);
        }
        void fetchUsers();
    } catch (e) {
        const err = e as Error & { message?: string };
        message.error(`${t("rbac.errors.failed")}: ${err.message ?? String(e)}`);
    } finally {
        setSaving(false);
    }
};
```

注意最后一行 `message.error(...)` 原代码是 `t("rbac.errors.failed")`——上面伪代码里写错，复制时保持原样 `t("rbac.errors.failed")`。

**(c)** 表单（line 313-362），在 `<Form.Item name="email">` **之后**插入密码字段（在 `<Form.Item name="enabled">` 之前）：

```tsx
<Form.Item
    name="password"
    label={t("rbac.user.password")}
    // 创建必填、编辑可选
    rules={
        editing
            ? []
            : [{ required: true, message: t("rbac.user.password") }]
    }
    extra={editing ? t("rbac.user.passwordPlaceholder") : t("rbac.user.passwordStrengthHint")}
>
    <Input.Password autoComplete="new-password" />
</Form.Item>
```

**(d)** `onEdit`（line 104-115）确保打开编辑时不预先填 password——`onCreate` 已经调过 `form.resetFields()`，表单状态干净；`onEdit` 不再 reset，但 password 字段无 initialValue 也不会有脏值。无需额外 `password: ""`。**注意**：用户在编辑时输入 password 才会触发 `adminResetPassword`；如果上次输入未提交，需保留输入框内容——antd 默认保留。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/tests/AdminUsersPage.password.test.ts`
Expected: 4 passed

- [ ] **Step 6: 跑全量前端测试确认无回归**

Run: `cd frontend && npx vitest run`
Expected: 全绿（含新增 4）

- [ ] **Step 7: 不 commit**

---

## Task 7: 跑全量后端测试确认无回归

**Files:** 无（仅验证）

- [ ] **Step 1: 跑后端集成 + 单元测试**

Run: `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pass@localhost:5433/qa_metadata_test pytest app/tests/ -v --tb=short`
Expected: 全绿，新加 9（3 schema + 2 service + 4 endpoint）通过

- [ ] **Step 2: 跑后端 lint + 类型**

Run: `cd backend && mypy app/ && ruff check app/`
Expected: 0 errors

- [ ] **Step 3: 不 commit**

---

## Task 8: SSOT summary + memory

**Files:**
- Create: `Harness/changes/feat-admin-user-password/summary.md`
- Create: `~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-admin-user-password.md`
- Modify: `~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/MEMORY.md`（索引行）

- [ ] **Step 1: 写 SSOT summary.md**

按 `Harness/rules/变更记录强制规范.md` 9 段：
1. 背景与目标
2. 设计决策（A+A）
3. 文件改动清单
4. 接口契约（JSON 字段）
5. 数据迁移（无）
6. 测试报告（9 + 4 = 13 通过）
7. 已知边界/不做
8. 提交方式（用户自己）
9. 部署复盘（无 docker 改动，API 已 deploy，提示用户 rebuild frontend）

- [ ] **Step 2: 写 memory**

memory 文件 body 写明：决策 A+A + 3 个关键实现约束（schema min_length=8 + 服务端 validate_password + must_change_password=True），加 cross-link `[[qa-system-frontend-deploy-build-required]]`（前端部署必 rebuild）。

- [ ] **Step 3: MEMORY.md 加索引行**

- [ ] **Step 4: 不 commit**

---

## Self-Review（计划自查）

1. **Spec 覆盖**：
   - 决策 1「新建必填明文 + bcrypt + must_change=True」→ Task 1（schema）+ Task 2（service）+ Task 3（集成测试） ✅
   - 决策 2「编辑弹窗复用 + 密码可选 + 走 PUT /users/{id}/password」→ Task 6（前端） + Task 5（API 客户端） ✅
   - 复用 `validate_password()` + `AuthService.admin_reset_password` 既有代码 ✅
   - 双语 i18n → Task 6 Step 3 ✅
   - TDD 全程（每 task 第一步写测试） ✅

2. **占位符扫描**：无 TBD/TODO/「类似 N 任务」/「添加错误处理」占位。所有 code block 完整。

3. **类型一致性**：
   - `UserCreate.password` (Task 1) ↔ `UserCreatePayload.password: string` (Task 4) ✓
   - `AdminResetPasswordPayload.newPassword` (Task 4) ↔ `adminResetPassword(userId, payload)` (Task 5) ↔ `PUT /users/{id}/password` 请求体（Task 6）✓
   - `forceChangeOnNextLogin: true` 固定值（Task 6 Step 4(b)）— 决策上不接受 UI 暴露该开关，简化交互

---

## 验收标准

1. `pytest app/tests/integration/test_admin_user_password.py -v` 4 passed
2. `pytest app/tests/integration/test_user_create_password.py test_identity_create_user_password.py -v` 5 passed（3 + 2）
3. `npx vitest run src/tests/AdminUsersPage.password.test.ts src/tests/rbac.password.test.ts src/tests/users.api.password.test.ts` 7 passed（4 + 2 + 1）
4. `npx vitest run` 全量无回归
5. `mypy app/ && ruff check app/` 0 errors
6. `Harness/changes/feat-admin-user-password/summary.md` 9 段齐
7. MEMORY.md 索引行已加
8. 用户实际执行 `docker compose build --no-cache frontend` 后能 hard refresh 看到密码字段
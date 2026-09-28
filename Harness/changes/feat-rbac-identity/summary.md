# 变更：feat-rbac-identity

- **日期**：2026-09-07
- **作者**：启琳（Claude Code）
- **Phase**：RBAC 权限体系（需求 1-5 全量落地）
- **状态**：implementation done + code-reviewer/security-reviewer pass（HIGH/MEDIUM 已修复并补回归） + prod 部署验证通过

## 1. 需求

QA System 原本无持久化身份/权限体系（stub header 认证 + owner∈departments ACL + `getAdminOnlyActor`），菜单不按人过滤。本次落地 5 项：

1. **用户管理**：增删改查用户
2. **角色 + 组织管理**：给用户授予角色、分配到组织
3. **菜单管理 + 三维度授权**：动态增删菜单；用户/角色/组织任一维度授菜单权限，**最终权限 = 三来源合集**
4. **权限查询**：任意用户 / 任意角色 / 任意组织的当前权限（含来源拆分）
5. **数据权限预留**：本次仅菜单权限；接口签名按多资源类型预留，文档写明后续 `data_permission_grant` 方案

## 2. 已确认设计决策（4 项）

| 决策 | 结论 |
|---|---|
| 用户↔组织 | **多对多**（`user_organizations`），与 `CurrentUser.departments` 列表语义一致 |
| 执行深度 | 本次仅**菜单可见性**（过滤 `GET /menu-config`），后端接口不做逐 API 403（admin-only 管理路由除外） |
| 身份解析 | `getCurrentUser` 用 `X-User-Id` 查真实 `users.username`；**查无此人回退桩默认**（dev/test 保持绿） |
| 组织 | **扁平**，`parent_id` 仅预留，无树 UI / 权限继承 |

## 3. 数据模型（Alembic `0045_rbac_identity`）

6 张表（`backend/app/models/rbac.py`，re-export 至 `app/domain/models.py`）：

- `users`（username unique、无密码无登录）
- `roles`（`code='admin'` 内置超管角色）
- `organizations`（parent_id 可空自引用，仅预留）
- `user_roles` / `user_organizations`（多对多复合 PK）
- `permission_grant`：`(menu_code → menu_config.code ON DELETE CASCADE, subject_type ∈ USER/ROLE/ORGANIZATION, subject_id)` 多态授权，unique `(menu_code, subject_type, subject_id)`

约定：`Base` / `TimestampMixin` / `BigIntPk` / `BigIntFk`。DTO 在 `schemas/rbac.py`（CamelModel）。

## 4. 权限语义

- 有效权限 = `direct(USER) ∪ role(ROLE) ∪ org(ORGANIZATION)`，仅统计**已持有主体**的授权
- admin 角色旁路 = 全部可见菜单（超管）
- 授权采用 **set-replace**（`PUT .../permissions` 传 `list[menu_code]` 全量），幂等防重复
- 共享助手 `app/api/v1/rbac_grant.py:replaceSubjectMenuGrants`：先校验 code ∈ 叶子集（未知 code → 422），再委托 `PermissionService.replaceSubjectGrants`
- **#5 预留**：`PermissionService.computeEffective(session, subject_key, all_menu_codes, ...)` 以菜单为粒度；数据权限后续新增 `data_permission_grant(resource_type, resource_code, subject_type, subject_id)`，`resource_type ∈ ontology_class/ontology_property/metric`，复用同一 subject + 合集解析

## 5. 接口面

- `app/api/v1/users.py` / `roles.py` / `organizations.py`：CRUD + `PUT|GET /{id}/permissions`；users 另有 `PUT /{id}/roles`、`PUT /{id}/organizations`、`GET /{id}/permissions`（有效权限含 direct/role/org 来源拆分，`UserEffectivePermissionsRead`）
- `app/api/v1/menu_config.py`：新增 `GET /admin`（全量扁平行）、`POST/PUT/DELETE ""`（admin-only，动态菜单）
- 全部写操作 `Depends(getAdminOnlyActor)`；`GET /menu-config` 现按调用方有效菜单过滤（Phase D）
- `scripts/seed_rbac.py`：幂等 upsert admin 角色 + admin 用户绑定（接入 lifespan）；`seed_menu_config.py` 追加 4 个 RBAC 管理叶子（item.adminUsers/Roles/Organizations/Menus，`section.auditSecurity`，零 section 变更）

## 6. 身份接线（`app/dependencies.py`）

- `CurrentUser` 增 `dbUserId: int | None`
- `getCurrentUser`：命中 DB（enabled）→ roles/departments 以 DB 为准（忽略 header 角色，防伪造 X-User-Roles=admin）；未命中 → 桩默认 `(user, admin)`
- `getAdminOnlyActor` 语义不变

## 7. 前端（React 18 + antd 5）

- `pages/Admin{Users,Roles,Organizations,Menus}Page.tsx` 4 页 + `components/rbac/MenuGrantModal.tsx`（勾选树授权 / readOnly 查看）
- `api/{users,roles,organizations}.ts`、扩展 `api/menuConfig.ts`（admin CRUD）
- `types/rbac.ts` + 扩展 `types/menuConfig.ts`；`menuIcons` 增 user/team/org/menu；App.tsx 4 条路由 `/admin/{users,roles,organizations,menus}`
- i18n `rbac.*` + `menu.item.admin*`（zh-CN/en-US）

## 8. 验证（review 修复后复跑）

- 后端：`test_rbac_api.py` **34 passed**（31 + 3 回归：disable-last-admin ×2、org self-parent）；`test_permission_service.py` + `test_dependencies.py` = **21 passed**（真实 PostgreSQL + 完整 API 链路）
- 前端：`vitest run` **718 passed / 89 files**（含 menuGrantModal 纯函数 4 测试）；`npx tsc -b` + `npm run build` 通过（jsdom `getComputedStyle(elt, pseudoElt)` stderr 为 antd 渲染既有噪音，非失败）
- menu seed：`seed_menu_config` 幂等（run1/run2 = 6 sections + 28 items，零新增）

## 8b. Prod 部署（2026-09-08）

- **背景**：用户报告"前端看不到 4 个 RBAC 管理菜单"。根因：(a) `seed_menu_config` 从未接入 lifespan；(b) prod `qa_metadata` 缺 `0045_rbac_identity` 6 张表，alembic 落后；(c) 前端/后端容器镜像未重建，工作树改动未进容器（runbook §11 / §13）
- **修复**：(1) `app/main.py` lifespan 在 `seedRbacBaseline` 前加 `await seedMenuConfig(session_factory)`；(2) 停后端 → `alembic upgrade head`（0044→0045，建 6 表 + `permission_grant`）；(3) `docker compose build backend frontend` → `up -d`
- **lifespan 启动日志**：`menu_config seed: 34 rows upserted` + `seedRbacBaseline: admin role/user ensured`
- **端到端 smoke**：`GET /menu-config` 6 sections，auditSecurity 含 5 项 admin item；`GET /users` 返回 admin baseline 用户；前端 bundle hash `index-BCwMhoKR.js` 含 4 条 admin 路由 + 4 个 i18n 文案
- **prod 数据状态**：6 张 RBAC 表已建；菜单 34 行（28 item 含 4 个 RBAC 管理页）；admin 用户/角色就位

## 9. 遗留 / 待办

- 真实浏览器 E2E（playwright 脚本复用，见菜单 E2E 记忆）——Phase F review 后
- 前端 funcs 覆盖率门槛 74.94% 预失败（其它 feature 0% funcs 文件，见 [[qa-system-frontend-coverage-gate]]），本次纯逻辑已补单测
- 数据权限（#5）仅预留签名 + 文档，无表无实现

## 10. 审查结论（code-reviewer / security-reviewer）

两路 reviewer 均已跑完，无 CRITICAL；发现并**已修复** 3 项，余下按既有桩认证设计 **documented-by-design**：

**已修复（含回归测试）**
| 级别 | 问题 | 修复 |
|---|---|---|
| HIGH | `seedRbacBaseline` 无内部 commit，lifespan 末尾调用后不持久化（测试中被 `_seedMenus` 的 commit 掩盖；prod 致命） | `seed_rbac.py` 函数末尾加 `await session.commit()`，对齐其它 seed 函数；主流程冗余 commit 保留无害 |
| MEDIUM | `identity_service.update_user` 缺 last-admin 防护：唯一 admin 可被 `enabled:false` 停用，绕过 delete/set_role 红线 | update_user 停用前加 `_user_has_admin_role && _count_admin_users <= 1` → `ConflictError`；补 `test_cannot_disable_last_admin` + `test_cannot_disable_last_admin_when_two_admins` |
| MEDIUM | 前端新建用户 createUser 成功后关联角色/组织失败 → 弹窗停留、重试 409 重复用户名 | `AdminUsersPage.onSubmit`：create 关联步骤独立 try/catch，失败即关弹窗 + 刷新 + `rbac.errors.partialCreated` 明确提示可编辑补设；补 i18n 双语文案 |
| LOW | `update_organization` 允许自引用（`parent_id==row.id` 跳过校验并赋值）→ 组织成自身子级、delete 永久 409 | 显式 `ValidationError`；补 `test_update_self_parent_422` |
| LOW | `set_user_roles` 双查询；`_count_admin_users` 物化列表 | 合并为一次 `select(Role.id, Role.code)`；`func.count(distinct user_id)` |

**documented-by-design（系统级桩认证预存设计，非本次回归；生产由 `AUTH_STUB_ENABLED=0` + 反向代理剥头闸门）**
- 桩身份查无此人（含默认 `X-User-Id: system`）回退 `(user, admin)` → 匿名即超管：dev/test 便利，无真实登录前按设计
- `fetchMenuConfig`（raw fetch）不带 `X-User-Id` → 无登录态下 UI 菜单过滤不生效（全量可见）；真实登录接上后生效
- 审计 actor 取 header `X-User-Id`（当前桩语义）
- 已停用用户命中 DB → 仍按 DB roles/departments 解析（无 enabled 检查），不更危险：停用仅影响管理页登录意图，权限仍按角色旁路

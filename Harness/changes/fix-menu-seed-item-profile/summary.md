# 变更：fix-menu-seed-item-profile

- **日期**：2026-10-23
- **作者**：启琳
- **Phase**：done
- **状态**：done（代码已验证；生产端手工 INSERT 待确认后由下版部署收编）

## 1. 需求

**现象**：CentOS 7 首装部署后，admin 新建用户报 500（前端 console：`POST /api/v1/users 500`）。

**根因**：`seed_menu_config.py` 的 `ITEMS` 清单**从未包含** `item.profile` /
`item.changePassword`。新建用户时后端要给新用户授「个人中心」菜单
（`identity_service.py:123`，`rbac.py:39 DEFAULT_NEW_USER_MENU_CODE = "item.profile"`），
`permission_grant.menu_code` 有 FK 指向 `menu_config` ⇒ 表里没有这行 ⇒
`ForeignKeyViolationError`（约束 `fk_permission_grant_menu`）→ 500。

**为什么开发环境一直没暴露**：dev 库的这两行是历史上手工 SQL / 菜单 UI 补进去的，
seed 缺口被现网数据掩盖。任何**新装库**（首装、重建、迁移环境）必踩。

**同源缺口**（同一 feature 漏同步的三处）：
- `seed_menu_config.py` ITEMS 缺 2 条（本变更主修）
- `test_seed_paths_aligned_with_frontend_routes` 手工维护的 `frontend_routes`
  集合缺 `/profile`（App.tsx:132 真实路由存在）
- backend 启动自动 seed 只覆盖 agent_tool_bindings / agent_tool_configs /
  feature_rules，`menu_config` 从来是手动脚本——而部署文档/计划的初始化清单没列它

**实测证据**：
- 生产 traceback：`asyncpg.exceptions.ForeignKeyViolationError: ... Key
  (menu_code)=(item.profile) is not present in table "menu_config"`
- 排除 schema 漂移：`alembic current` = `alembic heads` = `0132`
- 生产跑 seed 后仍 `SELECT ... WHERE code='item.profile'` → 0 rows，
  全表恰 51 行 = seed 清单总数 ⇒ 缺的是脚本条目不是执行
- dev 库（qa-postgres:5433）权威行：parent=section.systemConfig，
  profile visible=t / changePassword visible=f，icon user/lock，sort 580/585

## 2. 设计评审

| 方案 | 结论 |
|---|---|
| A. 只在生产库手工 INSERT 两行 | 治标。下次重装/新环境必再踩 |
| B. 把 `item.profile` 等 2 条补进 seed ITEMS | **采纳**（治本 + 手工 INSERT 作生产临时解） |
| C. 改 `identity_service` 授菜单前判空 | 否。掩盖数据缺口，默认菜单静默丢失比 500 更危险 |
| D. 把 seed 挂进 backend 启动 | 留作后续（牵涉「启动 seed 在半空库产脏数据」铁律，见部署计划 Task 门禁；单列决策） |

**采纳 B 的连带修正**：seed 的 insert 原来把 `visible` **写死 `True`**，
ITEMS 里写 `visible` 键会被静默忽略——改为 `it.get("visible", True)`。
不修正的话 `changePassword`（应隐藏）会被播成可见。

**验收标准**：
1. 真实 PG 集成测试 `test_seed_menu_config.py` 全绿（幂等、数量、路由对齐、main 端到端）
2. 新装库跑一遍 seed 后 `SELECT code FROM menu_config WHERE code='item.profile'` 有行
3. 新装库上「新建用户」→ 201

## 3. 实施清单

- `backend/scripts/seed_menu_config.py`
  - ITEMS +2：`item.profile`（visible，sort 580，path /profile，icon user）、
    `item.changePassword`（visible=false，sort 585，path /change-password，icon lock），
    取值与 dev 库权威行一致
  - insert 改 `visible=it.get("visible", True)`（原写死 True）
- `backend/app/tests/integration/test_seed_menu_config.py`
  - 总数断言 51→53；可见项 43→44（changePassword 隐藏，`list_sections` 不列）
  - `frontend_routes` 对齐集合补 `/profile`（注明 /change-password 在 App.tsx
    存在但 seed 隐藏，list_sections 不列隐藏项故不入该集合）
- 生产临时解（不进代码）：手工 INSERT 两行，parent 用
  `(SELECT id FROM menu_config WHERE code='section.systemConfig')` 子查询
  （两环境 section id 不同，禁止写死）+ `ON CONFLICT (code) DO NOTHING`

## 4. 验收结果

- `uv run pytest app/tests/integration/test_seed_menu_config.py` → **6 passed**
  （qa-pg-a1:5434 真实 PG + qa-neo4j-test 实例）
- 生产端 SQL 已交付，新建用户复测待操作者回执

## 5. 教训（已沉淀进规则）

1. **「seed 输出 N rows」只是清单总数，不是实写数**——不得当成功证据；
   数据是否落库以 SELECT 为准。
2. **新功能引用的静态数据必须在 seed 脚本可复现**——凡代码里引用常量
   （如 `DEFAULT_NEW_USER_MENU_CODE`）指向 DB 行的，该行必须进 seed + 集成测试
   数量断言 + 路由/引用对齐测试。代码常量 ↔ seed 条目 ↔ 测试断言三处同改。
3. dev 库的「手工补数据」会掩盖 seed 缺口，新装部署是唯一能暴露它的场景
   ⇒ 首装冒烟必须包含「新建用户」。
4. 策略落点：`Harness/rules/部署与访问规范.md` §五（新装库初始化 seed 清单）。

## 6. 遗留

- `POST /api/v1/models` 422（同一次首装暴露，独立问题，尚未定位——422 是
  请求校验拒绝，需 422 响应体的 detail 定位字段）
- 部署 runbook（docs/deployment/04-initialization.md 与 CentOS7 计划）补
  seed 清单步骤——随本次规则更新后续跟进

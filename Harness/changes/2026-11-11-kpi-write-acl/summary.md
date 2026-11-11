# KPI 写入口收口到 admin（评审 HIGH-1 的产品决策落地）

> 分支：`feat/kpi-write-acl`（base = `origin/main` `08788a67`，含 PR #119 的匹配器 v2）
> 来源：`feat/kpi-match-v2`（PR #119）评审 HIGH-1
> 状态：**已完成，未部署**。零 DDL、零生产写、无 Alembic 迁移。

## 1. 背景

KPI 匹配器 v2 评审（PR #119）报出一条 HIGH：

- `matchRoute` **不读** `kpi_catalog.match_threshold`；v2 下「短语命中即命中」，只靠名称/关键词决胜负；
- KPI 写入口（create/update/delete）在收口前**只要求登录**（`getCurrentUser`），`owner` 还是客户端自填；
- 组合后果：任意登录用户可新建一条同名 KPI，把自己的问句劫持到该 KPI 的契约块（B3 注入 / chat L1 路由方向）。

产品决策（2026-11-11）：**写入口一律 admin**，与词条字典 / 本体指标（PR #116 治理写入口收口）同口径。
前端按角色隐藏写按钮，后端同时强制。

## 2. 变更

| 面 | 文件 | 变化 |
|---|---|---|
| 后端路由 | `backend/app/api/v1/kpi_catalog.py` | `createKpi` / `updateKpi` / `deleteKpi` 的依赖 `getCurrentUser` → `getAdminOnlyActor`；`bulk-bind` 本来就已是 admin |
| 后端 service 注释 | `backend/app/services/kpi_catalog_service.py` | `createKpi` docstring 不再声称「CREATE 不做 ACL」—— 权限在路由层 |
| 后端测试 | `backend/app/tests/integration/test_kpi_catalog_governance.py` | ACL 段改为新策略：非 admin（含 owner 部门、多部门）一律 403，且断言 403 的 detail 含 admin 语义；新增「非 admin 不能建 PUBLISHED / admin 可以」「非 admin 仍可读列表与详情」反向守卫；`_create` 助手与审计/历史用例改用显式 admin 头（此前依赖 stub 默认角色，属隐式通过）；`TestKpiAclRollbackOnFailure` 语义更正为「路由层拒绝无写副作用」 |
| 前端 | `frontend/src/pages/KpiCatalogPage.tsx` | `useAuthStore` 取 `roles`；非 admin 不渲染「新建 KPI」按钮与操作列（编辑/删除） |
| 前端测试 | `frontend/src/tests/KpiCatalogPage.test.tsx` | 默认以 admin 渲染既有用例；新增「写入口限 admin」组：非 admin 无三入口 + admin 有三入口（反向守卫） |

**刻意不新增 integration 测试文件**：CI 分片按 `find | sort` 的「下标 mod 4」切分，新增任何文件都会让其后的
文件整体轮转一位（PR #116 / #119 均踩过）。写入口用例因此并入既有的 `test_kpi_catalog_governance.py`
（629 行，仍 < 800 行上限）。

**保留不动**：`KpiCatalogService` 的 owner ACL（`AclService`）仍在（admin 恒通过），仅用于审计语义，
不再作为权限闸门 —— 与本次路由层收口是纵深关系，不重复也不冲突。

## 3. 行为变化（需告知用户）

- 以前：`owner` 部门成员可编辑/删除本部门 KPI；
- 现在：**只有 admin 能写**。部门成员在目录页看不到写按钮，直接调 API 得 403。
- 403 文案沿用 `getAdminOnlyActor` 的既有 detail（`仅 admin 可访问审计 API`），对 KPI 场景不够贴切 ——
  记 LOW：该文案被 78 处复用，全局改属独立清理，不在本批。

## 4. 验收证据

真实 PostgreSQL（5434）+ 完整 HTTP 链路：

| 集合 | 结果 |
|---|---|
| `test_kpi_catalog_governance.py` | **18 passed**（含新策略 ACL、发布态对照、读路径守卫） |
| 全部 `test_kpi_*.py` 集成文件 | **228 passed, 1 failed** —— 唯一失败为**既有** `test_kpi_catalog_search_api.py::test_search_requires_auth`（在 `feat/kpi-match-v2` 上同样失败） |
| `test_audit_api.py` / `test_audit_export_api.py` / `test_history_replay_api.py` / `test_reports_api.py` / `test_seed_menu_config.py` | **41 passed, 4 failed** —— 4 条均为 `test_seed_menu_config.py` 既有失败（在 `feat/kpi-match-v2` 上同样 4 条失败） |
| 前端 `KpiCatalogPage.test.tsx` | **27 passed**（含新增 2 条） |
| 前端 `tsc --noEmit` | **exit 0** |

**变异取证**：把 `KpiCatalogPage.tsx` 的 `isAdmin` 断言改成常量 `true`，新增的
「非 admin 不渲染新建/编辑/删除入口」用例**立刻失败**（1 failed / 26 skipped）；改动已还原。

## 5. 明确未做

- **未部署**（生产仍是旧代码）。
- 未开 `system_config` 灰度行 `KPI_MATCH_CONTAINMENT_ENABLED`：本批只解除 ACL 阻塞，
  灰度开启仍是独立的一次数据操作，且需在 #119 与本 PR 都合入并部署之后再定。
- 未改 `getAdminOnlyActor` 的 403 文案（LOW，见 §3）。
- 未动 service 层 owner ACL（保留审计语义）。
- 未修**生产鉴权硬闸门的既有漏检形态**（安全评审 M1，pre-existing，单独立项）：
  `backend/app/config.py` 的 `validateProduction` 用 `os.getenv("APP_ENV")` 读取（只写在
  `backend/.env` 时不生效），且 `ALLOW_STUB_WHEN_REAL` 不在硬闸门内；`main.py` 只打 ERROR 日志。
  本次 admin 闸门的强度**依赖部署侧鉴权正确**，建议尽快核对生产 `APP_ENV` 注入方式与
  `AUTH_MODE=real` / `AUTH_STUB_ENABLED=0` / `ALLOW_STUB_WHEN_REAL=false`。

## 6. 回滚

纯代码改动，`git revert` 即可；回滚后写入口恢复为「登录即可写」，matching v2 的劫持面重新暴露 ——
因此回滚本批时应同时确保灰度行仍不存在。

# P5 Workflow 最小前端批次 —— 变更记录（**已实现**）

> 上位：[`../2026-11-16-p5-workflow-command/summary.md`](../2026-11-16-p5-workflow-command/summary.md)（P5 后端）
> 场景：`docs/update/1117/P5功能验收手册.md` —— 「先 API 验收，再定 UI」中的 **UI 步**

| 项 | 值 |
|---|---|
| **状态** | **implemented**（待 PR） |
| **规模** | M（3 个页面级单元 + 1 个 api 模块 + 3 个后端只读接口 + 菜单 seed） |
| **分支** | `docs/p5-acceptance-1117`（worktree `/private/tmp/qa-p5`） |
| **合并** | PR #<<PR_NO>> squash ⇒ `main @ <<MERGE_SHA>>`（合并后回填） |
| **迁移** | **无**（只追加 GET 接口） |
| **开关** | 同 P5：`ENABLE_WORKFLOW` 关闭 ⇒ 三个新接口一并 404 |

## 1. 范围：用户要的三件事 × 实际需要的四件事

用户诉求（原文归一）：

1. workflow 页：实例列表 + 详情（步骤时间线 + 审批记录）；
2. 待办审批页：我的待办 + 通过/驳回；
3. 提交入口：从能力/业务对象页跳转 **或** 一个简单表单；
4. 菜单 seed（属 CI 面 ⇒ 闸门 exit 6 走 owner 仲裁）。

**侦察发现**：P5 后端只有 6 个接口，且**没有列表面**（只有 `GET /instances/{id}`）。没有列表
就没有页面 —— 因此本批次必然包含后端只读接口。最终交付：

| 层 | 内容 |
|---|---|
| 后端读面（3 个新接口） | `GET /workflow/instances`（分页，可见性见 §3）· `GET /workflow/approvals`（我的待办，要求已校验身份）· `GET /workflow/definitions`（仅 PUBLISHED，**不下发定义体**） |
| 前端 | `pages/WorkflowInstancesPage.tsx`（列表 + 详情抽屉 + 提交 Modal + 取消）· `pages/WorkflowApprovalsPage.tsx`（待办 + 通过/驳回）· `api/workflow.ts` · `types/workflow.ts` |
| 接线 | `App.tsx` 两条路由 `/workflow`、`/workflow/approvals`；i18n zh-CN/en-US 各一套 `workflow.*`；`ICON_REGISTRY` 新增 `workflow`/`approval` |
| 菜单 | `seed_menu_config.py` 新增 `item.workflowInstances`（bizConfig，377）、`item.workflowApprovals`（378） |

提交入口选**页内表单**而非「从业务对象页跳转」：业务对象页跳转需要先把「业务对象 → 能力」
的映射做成可见的 UI 概念，那是 P6/A4 的题材；简单表单（选已发布定义 + 幂等键 + 请求 JSON）
已经能完成提交与验收，且不欠下跨页契约。

## 2. 三个新接口的语义（各自的一个坑）

### 2.1 实例列表的可见性 = 自作用域 ∪ 审批持有人 ∪ 真实 admin

与详情面 `_assertInstanceVisible` **逐条一致**（列表看得到 ⇔ 详情打得开）：

- 自己提交的：不需要真实身份（自作用域）；
- 别人的：必须 `isRealIdentity` 且（admin 角色 或 审批持有人）——stub 头可冒充任意账号；
- admin 判定用 `isRealIdentity and ADMIN_ROLE_CODE in roles`：**桩身份的 admin 不算 admin**。

### 2.2 「我的待办」的持有人匹配有三条来源（这就是坑）

任务行 `approver_user` **只在决策那一刻**写签署人；创建时是 NULL。所以待办不能只看行：

1. `task.approver_user == actor`（已决策任务的历史匹配）；
2. 定义体审批矩阵 `approverUser`（指定的具体人）；
3. 矩阵 `approverRole` / 任务行 `approver_role`（角色匹配）。

矩阵条目设置了 `approverUser` 时 **不再**回落到角色分支（与原 `elif` 语义逐字一致）。

### 2.3 待办列表要求**已校验身份**（403 `APPROVAL_REAL_AUTH_REQUIRED`）

`GET /approvals` 本质是跨用户读 ⇒ 与决策接口同口径。前端把 403+该 detail 翻译成
「请登录后重试」的可执行提示，而不是通用错误条。

## 3. 实施中新增的关键裁决

### D-U1 抽出 `workflow_approval_query.py`（行数上限的硬约束）

实现后 `workflow_approval_service.py` 到 **929 行**，直接撞 `test_workflow_write_gate.py`
的 800 行守卫（真实 RED）。处置：把读路径（`matchesHolder` / `listMyApprovals` /
`listVisibleInstanceIds`）抽成独立只读查询层，`WorkflowApprovalService` 与
`WorkflowService` 共用同一个 `matchesHolder` —— 这同时**消除了列表/详情两套匹配逻辑
漂移的可能**（正是最容易埋授权漏洞的地方）。最终行数全部 < 800。

### D-U2 持有人扫描有上限且**不静默截断**

跨实例匹配需要拉矩阵再比对，代价是 O(实例数)。当前 `HOLDER_SCAN_LIMIT = 2000`
（按 `due_at` 升序），达上限**打 warning**而不是悄悄少返回。正解 = A4-0：把指定审批人
落到任务行（审批面扩展），届时扫描退化为一次索引查询。

### D-U3 定义列表不下发 `definitionJson`

测试 `test_定义列表_不含定义体` 断言响应既无 `definitionJson` 键、也无 `approvalMatrix`
字样：矩阵里有「谁批这件事」，对所有登录用户下发等于泄露审批面。

### D-U4 菜单 seed 属 CI 面 ⇒ 闸门 exit 6（owner 仲裁）

`seed_menu_config.py` 是被闸门规则扫描的 CI 面文件，本次改动会让 merge-gate 判 exit 6。
按既有流程走 owner 仲裁（与 PR #144/#145 同法）。两个 item 都只挂在 `section.bizConfig`
下（377/378），不动 section。

## 4. 证据

| 面 | 证据 |
|---|---|
| 后端新增读面 | `app/tests/unit/test_workflow_list_api.py` **11 用例**（开关关闭 404 / 自作用域 / 持有人 / admin / 分页 / 待办匹配与上下文 / 未验签 403 / 决策后流转 / 定义只取 PUBLISHED / 不带定义体） |
| 后端回归 | `test_workflow_*.py` 全部 **23 个文件 = 335 passed**（全新隔离库 `qa_metadata_test_*`、单进程 ⇒ 0 failed） |
| 菜单 seed | 菜单相关 5 个文件 = **27 passed**；seed 实跑 `58 rows upserted`（8 类 + 50 项，docstring 旧计数已一并修正） |
| 前端新增 | `tests/WorkflowInstancesPage.test.tsx` 4 例 + `tests/WorkflowApprovalsPage.test.tsx` 4 例（含「驳回必填意见不发请求」「403 显示登录提示」「决策后重拉」） |
| 前端全量 | `npx vitest run`（bare，不用目录过滤）⇒ **237 文件 / 2414 用例全绿** |
| 类型 | `npm run build`（`tsc -b` + vite build）通过，tsc 基线仍 0 错误 |
| 图标契约 | `menuIcons.test.ts` 白名单加入 `workflow`/`approval`，注册表与 seed 对齐 |
| 本地栈部署 | 2026-11-17 已用本分支代码重建 `qa-system-backend` / `qa-system-frontend`（同一镜像 `14da8d76`），worker 同镜像重启；DB 在 `0152`；`GET /workflow/{definitions,instances,approvals}` 均 200；`/api/v1/menu-config` 含两条新菜单码；前端 bundle 含「工作流实例 / 待办审批」 |

> 新增测试文件全在 `unit/` 与 `src/tests/`：integration 分片按「排序下标 mod 4」轮转，
> 新建 integration 文件会把既有文件挤到别的分片 ⇒ 假 net-new（D-12）。
>
> **取证纪律（本次踩过）**：曾出现 `uq_ontology_class_name_version (Supplier,1)` 唯一冲突 +
> 「实例 1 不存在」404，形状与代码回归完全一致；实因是一条后台命令里 `$(ls 通配)` 被 zsh
> 空匹配中止 ⇒ 裸 `pytest` 收全量套件、在后台长跑并共用同一 test 库，与排查进程互相
> TRUNCATE。**先 `ps` 抓僵尸 pytest、再在全新库单进程复跑**，之后才可判回归；本批次最终
> 零代码改动即全绿。

## 5. 已知遗留（明确不做 / 后续）

| 项 | 处置 |
|---|---|
| 定义管理 UI（发布/版本/矩阵编辑） | 本批次不做，已由**定义管理批次**补齐：[`../2026-11-17-p5-workflow-definition-admin/summary.md`](../2026-11-17-p5-workflow-definition-admin/summary.md)（`/workflow/definitions`，admin 写面 + 审批矩阵行编辑） |
| 待办的扫描上限（D-U2） | A4-0：审批人落任务行 |
| 「从业务对象页跳转提交」 | 不做，见 §1；P6 再谈 capability→UI 的映射 |
| 页面在 stub 身份下的待办 | 按设计 403 并提示登录；本地验收需真实 JWT（见验收手册） |

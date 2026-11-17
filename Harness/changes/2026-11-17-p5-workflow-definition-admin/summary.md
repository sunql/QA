# P5 工作流定义管理（新建/编辑/发布/停用）—— 变更记录（**已实现**）

> 上位：[`../2026-11-16-p5-workflow-command/summary.md`](../2026-11-16-p5-workflow-command/summary.md)（P5 后端）·
> [`../2026-11-17-p5-workflow-ui/summary.md`](../2026-11-17-p5-workflow-ui/summary.md)（最小前端批次）
> 场景：用户诉求「把 新建定义（模板）增加UI/API，需要能够从前台增加」

| 项 | 值 |
|---|---|
| **状态** | **implemented**（已合入 main） |
| **规模** | M（5 个 admin 接口 + 1 个定义页 + 1 个存储层 + 菜单 seed + 1 处前端框架修复） |
| **分支** | `docs/p5-acceptance-1117`（worktree `/private/tmp/qa-p5`） |
| **合并** | PR **#150** squash ⇒ `main @ 1e04b46e`（2026-11-17） |
| **迁移** | **无**（复用 P5 已有的 `workflow_definition` 表与既有约束） |
| **开关** | 同 P5：`ENABLE_WORKFLOW` 关闭 ⇒ 整组 `/workflow/*` 路由不注册（404） |

## 1. 为什么这是一件「安全边界」上的事

改动前，定义（模板）只能靠**直接写库**产生（迁移播种 / 脚本 INSERT）。这有两个问题：

1. **绕过了全部校验**：定义体 `steps` / `approvalMatrix` 的合法性（失败关闭的纯函数校验器）
   只有在服务层才会跑；裸 INSERT 可以写出「没有步骤」「审批矩阵为空」「步骤既无
   commandType 也无 requiresApproval」的坏定义。
2. **绕过了权限**：谁能写 `workflow_definition` 表，谁就能决定**谁批这件事**。
   审批矩阵是审批面的唯一事实来源 ⇒ 写定义 = 改审批面。

因此本批次不是「补一个 CRUD 页」，而是**把一条原本没有护栏的写路径收进服务层 + admin 门 + 校验器**。

## 2. 交付内容

| 层 | 内容 |
|---|---|
| 后端接口（5 个，全部 `Depends(requireAdmin)`） | `GET /workflow/admin/definitions`（分页，**含定义体**）· `POST /workflow/admin/definitions`（新建 ⇒ DRAFT）· `PUT /workflow/admin/definitions/{id}`（编辑，**仅 DRAFT**）· `POST /workflow/admin/definitions/{id}/publish`（DRAFT→PUBLISHED）· `POST /workflow/admin/definitions/{id}/disable`（PUBLISHED→DISABLED） |
| 后端 schema | `WorkflowDefinitionWrite`（`extra="forbid"`、请求体 ≤ 64KB）· `WorkflowDefinitionAdminRead` / `WorkflowDefinitionAdminPage` |
| 后端服务 | `workflow_definition_service.py`（**零 ORM 写**，全部委托存储层）· `workflow_definition_store.py`（**唯一**允许碰 `workflow_definition` ORM 实体的地方） |
| 前端 | `pages/WorkflowDefinitionsPage.tsx`（列表 + 新建/编辑 Modal + 审批矩阵行编辑 + 行内发布/停用）· `api/workflow.ts` 增 5 个方法 · `types/workflow.ts` 增 admin DTO · i18n `workflowDefinition.*`（zh-CN/en-US） |
| 接线 | `App.tsx` 路由 `/workflow/definitions`；菜单 `seed_menu_config.py` 新增 `item.workflowDefinitions`（bizConfig，379） |
| 框架修复 | `AppLayout.tsx` 菜单选中项改为**最长路径匹配**（见 D-A2） |

## 3. 实施中的关键裁决

### D-A1 定义写路径必须收口到存储层（写闸门 RED ⇒ 拆分）

P5 的写闸门（`app/tests/unit/test_workflow_write_gate.py`）规定「所有 P5 写操作全部经
Command/存储层」：只有存储层允许构造 P5 ORM 实体 / `session.add` / 给被跟踪实体赋属性。
本批次的第一版实现把 ORM 写留在 service 里 ⇒ 闸门 **8 处 RED**（真实失败，不是演练）。

处置：新建 `workflow_definition_store.py`（207 行）承接全部 `workflow_definition` 数据访问与写入，
`workflow_definition_service.py`（343 行）只做编排（校验 → 落库 → 审计）。
写闸门把该文件加入**路径白名单** `_ALLOWED_WRITERS`，并补一句理由：
*定义体决定「谁批」⇒ 写它同样是安全边界操作 ⇒ 必须收口到存储层*。

存储层**不 commit**：与审计写在同一事务里（`AuditService.record` 从不自行提交）。

### D-A2 菜单高亮按**最长路径**匹配（真实浏览器截图抓到的缺陷）

`/workflow` 是 `/workflow/definitions` 的**前缀**。原实现是「先匹配到谁算谁」⇒
在定义页的页头标题与菜单高亮都停在「工作流实例」上（用户在 A 页看到 B 页的名字）。
截图确认后改为**最长路径匹配**（路由语义），并补回归测试
（`AppLayout.test.tsx`「选中的菜单项按**最长路径**匹配」）。

### D-A3 定义体校验复用运行时**纯函数校验器**，不在前端重写一套

定义体的合法性由 `app/services/workflow_definition.py` 里的纯函数判定（失败关闭：
≥1 步；每步须有 `commandType` **或** `requiresApproval=true`；至多 1 个审批步；
矩阵条目 `matrixCode`/`subjectType`/`capabilityCode`/`riskLevel` 唯一且
`approverUser`/`approverRole` 至少一个；`timeoutHours` 1..720，默认 24）。
前端编辑器**不复制**这套规则，只做「能不能填」的提示 ⇒ 判定权始终在后端这一处。

### D-A4 `riskLevel` 服务端绑定，前端不可选

风险等级在提交时由服务端绑定（与运行时矩阵匹配的键之一：`(subjectType=workflow_instance,
capabilityCode=定义.capability_code, riskLevel=定义顶层 riskLevel)`）。
前端表单不暴露风险等级选择 ⇒ 不存在「客户端自选风险等级骗过矩阵匹配」的面。

### D-A5 编辑仅 DRAFT；发布/停用是状态机，不是自由改状态

- 编辑：仅 `DRAFT` 可改（否则 409），且服务端校验当前状态，避免并发覆盖（条件更新）。
- 发布：`DRAFT → PUBLISHED`；受**部分唯一索引** `uq_workflow_definition_published` 约束
  ⇒ 同一 `(domain, capability)` 至多一个 PUBLISHED，冲突返回 409。
- 停用：`PUBLISHED → DISABLED`（不删除历史行，便于对账）。

### D-A6 并发保护用**条件更新**，冲突时 409 而不是静默覆盖

存储层 `_conditionalUpdate` 用 `UPDATE ... WHERE id=? AND status=? RETURNING` 实现
乐观状态机；命中 0 行 ⇒ 抛 `ConflictError`（409），文案明说「状态已被并发改动」。

### D-A7 审计沿用通用 `CREATE`/`UPDATE`，**不新增迁移**

`entity_type="workflow_definition"`。`audit_log` 是**不可变表**（DB 触发器
`prevent_audit_modification`）⇒ 审计只增不改，验收时**不得**清理审计行。

### D-A8 提交前取审计前像（避免 identity map 刷新）

写库会刷新 identity map 里的同一实例 ⇒ 审计「前像」必须在写库**之前**取。
另外错误信息里的标量也要提前取好：**rollback 会 expire 全部 ORM 实例**，
在 `except` 块里读 `row.*` 会触发隐式 IO ⇒ `MissingGreenlet`（生产踩过同类坑）。

## 4. 证据

| 面 | 证据 |
|---|---|
| 写闸门 | `test_workflow_write_gate.py` **27 passed**（拆分前 8 处 RED ⇒ 收口后全绿） |
| 后端新增接口 | `test_workflow_definition_admin_api.py` **13 用例**（列表含定义体 / 新建 ⇒ DRAFT / 校验 422 / extra 字段 422 / 编辑仅 DRAFT / 发布 / 重复发布 409 / 停用 / 非 admin 403 / 状态机非法跃迁 409 / 幂等唯一约束 / 空请求体 422 等） |
| 后端回归 | `test_workflow_*.py` + 菜单相关 ⇒ **395 passed**（全新隔离库 `qa_metadata_test_p5def`、单进程 ⇒ 0 failed） |
| 前端新增 | `src/tests/WorkflowDefinitionsPage.test.tsx`（新建 → 列表出现 / 发布 → 状态变「已发布」/ 校验失败提示 / 矩阵行必填项等） |
| 前端框架回归 | `src/tests/AppLayout.test.tsx` 新增「最长路径匹配」用例 |
| 前端全量 | `npx vitest run`（bare，不用目录过滤）⇒ **238 文件 / 2421 用例全绿** |
| 类型 | `npx tsc -b` **0 错误**（`npm test` 不做类型检查，必须单独跑） |
| 真实浏览器 E2E | 一次性脚本（**不入仓**）打真实本地栈：登录 → `/workflow/definitions` → 新建定义（填域/能力/版本/步骤/命令类型/需要审批/矩阵/审批角色/超时）→ 确 定 → 列表出现新行 → 发 布 → 状态「已发布」；**6 步全过，0 console error** |
| 运行时可用性 | 前台建出的定义可直接用于提交：产出的实例为 `WAITING_APPROVAL` 且审批任务正确命中矩阵 `m1`（编辑器按定义自身的能力码 + 风险等级推导匹配键） |
| 菜单 seed | `python -m scripts.seed_menu_config` 实跑 ⇒ **59 rows upserted** |
| 本地栈部署 | 2026-11-17 已用本分支代码重建 `qa-system-backend:latest` / `qa-system-frontend:latest`（前端 `--build-arg VITE_API_BASE_URL=` 显式留空），backend/frontend 容器重建，worker 用同镜像独立进程重启；DB 在 `0152` |

> 新增测试文件全在 `unit/` 与 `src/tests/`：integration 分片按「排序下标 mod 4」轮转，
> 新建 integration 文件会把既有文件挤到别的分片 ⇒ 假 net-new（D-12）。
> `test_app_wiring.py` 只**改**既有 integration 文件的期望清单（14 条路由 + MCP 挂载守卫），
> 未新建 integration 文件。

## 5. 已知遗留（明确不做 / 后续）

| 项 | 处置 |
|---|---|
| 审批矩阵的独立管理页 | 不做。矩阵在**定义编辑器内**以「矩阵行」编辑（矩阵是定义体的一部分） |
| 审批矩阵的人工兜底（A4-0） | 矩阵项目前只能指定 `approverUser`（具体人）或 `approverRole`；A4-0 把指定审批人落到任务行、放开 `approval_task.instance_id` 后再谈人工改派 |
| 定义体 JSON 的可视化编辑（嵌套结构） | 当前是结构化表单（步骤行 + 矩阵行），不是任意 JSON 编辑器；`payload` 仍是自由 JSON 文本 |
| 定义的删除 | 不做。只有状态机（DRAFT/PUBLISHED/DISABLED），不物理删除历史行（对账需要） |
| 提交权收口到角色/域 | 仍未做（见验收手册 §6 边界 3）：任何登录用户可提交任何已发布能力 —— 产品决策项 |
| 生产部署 | 未部署；步骤见 `../2026-11-16-p5-workflow-command/deploy-prod-1117.md` —— **两个窗口**：① 迁移链收口（`0143`→`0148`，源 **只认 `86ff4a1f`**，同 head 0148 的 `68398378`/`75a435a3` 不含生产热修 ⇒ 会回退）② P5 本体 + **前端镜像**（源 `1e04b46e`；前端必发，`VITE_API_BASE_URL` 显式留空）③ 开启开关 + Worker（独立决策） |

## 6. 合并仲裁记录（PR #150，2026-11-17）

闸门（`merge-gate / failure-set-diff`）实测判 **exit 5**：基线 run 44053446789（main @ `a217fef0`）失败 130 条，
PR run 44093051740（head `2f067856`）失败 129 条 ⇒ **净新增 0 条 / 净丢失 1 条** ⇒ owner 越权口仲裁合并
（`gh pr merge --squash --admin --delete-branch` ⇒ `main @ 1e04b46e`）。

**那 1 条「净丢失」不是藏失败**：`test_app_wiring.py::TestRealAppRouteTable::test_no_route_registered_after_catch_all_mount`
在基线红因是 `AssertionError: 未在真实 app 路由表中找到 catch-all Mount（MCP 挂载点）` / `assert None is not None`
—— 守卫只认 `Mount(path=="")`，而 app 的 MCP 早已挂在 `Mount("/mcp")`（`main.py:803`）⇒ **恒红的假失效**，
从未触达它自己的真不变量。本批把守卫对齐现实形态（接受 `""` 或 `"/mcp"`），实测：真值 mount 位于 373 条
真实路由的**最后一条**（下标 372）⇒ 断言通过；**注入**一条 mount 之后的路由 ⇒ 守卫立刻抓到 ⇒ **护栏真实有效**，
不是把断言放松成空断言。本地复跑 `pytest app/tests/integration/test_app_wiring.py` ⇒ 4 passed。

其余四件套：净新增 0（闸门 + 本地同款工具**双路独立复算一致**）· 9 个 job 全部 usable 且两侧 job 集合一致 ·
未新建/删除任何 integration 测试文件（分片归属未变）· seed 改动属 CI 相关面（净丢失若为空本会判 exit 6，
两种情形都需 owner 仲裁，一次 bypass 覆盖）。

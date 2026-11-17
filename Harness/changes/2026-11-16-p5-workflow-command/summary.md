# P5 Workflow / Command / Approval —— 变更记录（**已实现**）

> 变更：[`plan.md`](./plan.md) · 部署：[`deploy-prod-1117.md`](./deploy-prod-1117.md)
> 上位：[`docs/update/1109/智能采购平台升级计划_日级排程_1109.md`](../../../docs/update/1109/智能采购平台升级计划_日级排程_1109.md) §11、§16
> 契约：[`docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md`](../../../docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md) §6 P5

| 项 | 值 |
|---|---|
| **状态** | **implemented**（W0–W4 已提交；W5 评审整改 R1–R10 全部完成；待 PR 合并） |
| **规模** | L（唯一 L 级事项） |
| **分支** | `feat/workflow-command`（worktree `/private/tmp/qa-p5`，从 `origin/main` 新开） |
| **合并** | PR #<<PR_NO>> squash ⇒ `main @ <<MERGE_SHA>>`（合并后回填） |
| **迁移** | **0149–0152**（4 个：0149 定义/实例 · 0150 步骤/命令 · 0151 审批/outbox · 0152 放宽 `audit_log` CHECK）⇒ **WP-ONT-AI 从 `0153` 起** |
| **开关** | `ENABLE_WORKFLOW`（默认 **false**；不进生产 `.env`） |
| **门禁** | **G6**：写操作全走 Command；Agent 仍只读；默认关闭可上线；回滚 = 关开关 + 停 Worker |

## 1. 本记录是什么

P5 是 1109 契约 D1–D62 的**最后一个**未完成阶段（L 级：工作流 / Command / 审批子系统）。
本记录是**已实现**状态的事实台账：范围、关键裁决（实施中新增的也记在这里）、W0–W5 的
实际产出、证据（测试 / 性能 / 评审）、以及**已知遗留**。

规划阶段的版本见 git 历史（`git show 385287be:Harness/changes/2026-11-16-p5-workflow-command/summary.md`）。

## 2. 一句话范围

新增工作流表族（definition / instance / step / business_command / approval_task / outbox）+ 状态机 +
幂等与行锁并发 + **数据驱动的采购审批矩阵** + Command 执行器（模拟适配器先行）+ 补偿钩子 +
审计每步可追溯 + 独立进程 Worker + compose v1 部署片段；默认关闭上线，回滚 = 关开关 + 停 Worker。

## 3. 侦察结论（决定方案的形状）

| 结论 | 处置 |
|---|---|
| 仓内**零** workflow/approval 实现、**零**队列库依赖 | 新造业务表 + 复用骨架；**不引入** celery/arq/APScheduler |
| 已有 **5 个独立进程 Worker**（轮询 PG 表） | Worker **照抄**该模式；不复用请求内 fire-and-forget（那是尽力而为） |
| 已有 outbox 两套 + 毒丸防护 + 同事务 enqueue | `workflow_outbox` 新建表（契约禁共用），结构照抄 |
| 已有纯函数状态机矩阵（fail-closed，未知状态抛 409） | 工作流状态机照抄（纯函数 ⇒ 可单测） |
| 审计动作有白名单 | 必须扩 `APPROVE/REJECT/COMPENSATE/WORKFLOW_*`（**服务层与 DB CHECK 必须同步**，迁移 0152） |
| 幂等键只在 JSONB，无唯一约束列 | 新造独立列 + `UniqueConstraint`（ORM 与迁移 DDL **同形**） |
| 行锁 + `skip_locked` + `populate_existing` 先例齐全 | 照抄；**不做**乐观锁（仓内无先例） |
| 生产 compose 是 **v1 1.29.2**，禁 `!override`/`profiles`/`docker compose` 子命令；禁新增中间件与镜像 | worker **复用现有 backend 镜像** + 追加 v1 兼容片段 |
| 迁移规范：≤32 字符 revision、对称 downgrade、迁移与代码同批上线、幂等 DDL、每阶段一个小迁移 | 6 表 **拆 3 个迁移**（+1 个 CHECK 放宽）；先在测试库演练 |
| 集成测试按「排序下标 mod 4」分片 ⇒ 禁新建 integration 文件 | 集成用例并入既有文件；新文件只放 `unit/` |

## 4. 实施中新增的关键裁决（规划时未预见 ⇒ 必须记）

### D-13 **步骤↔命令的链接**：`commandType` 落在**定义体**里，运行期认**快照**

**问题**：`store.enqueueCommand` 原先**没有任何生产调用方**（只有测试）⇒
Worker 会永远空转，G6 第 1 条「写操作全部经 Command」**落空**（判据真空）。
根因有两条：① 定义体里没有「这一步对应哪条命令」；② 没有驱动者。

**处置**：

- `workflow_definition.definition_json.steps[].commandType` = 该步要执行的**业务命令类型**
  （对应 `workflow_command_ports` 的注册表键）；为空 ⇒ 该步是「纯审批门」。
- **失败关闭**：一步**必须**有 `commandType` **或** `requiresApproval=true`，两者皆无 ⇒
  定义**解析期拒绝**（ValidationError），绝不「悄悄跳过」（跳过的步骤会让实例假成功）。
- **快照**：提交时把 `{commandType, requiresApproval, payload}` 写进
  `workflow_step.input_json`；运行期**只认快照**。
  为什么：`workflow_definition` 没有不可变触发器 ⇒ 若运行期回头读定义，定义一改，
  **在跑的实例**行为会跟着变。

### D-14 **编排器**（`workflow_orchestrator.py`）：唯一驱动者

`WorkflowOrchestrator.drive(session, instanceId=…)`：
锁实例 → PENDING→RUNNING → 按 `step_order` 列出步骤 → 取第一个未完成步：

| 情形 | 动作 |
|---|---|
| 该步 `requiresApproval` 且无审批任务 | 发起一轮审批 ⇒ **停**（等人工） |
| 该步 `requiresApproval` 且审批 PENDING | **停** |
| 该步 `requiresApproval` 且审批 REJECTED/TIMEOUT | **停**（实例收敛） |
| 该步 `requiresApproval` 且审批 APPROVED | 继续（过门） |
| 该步有 `commandType` | 入队命令（幂等键 `wf-{instanceId}-step-{stepId}`）⇒ 步 RUNNING ⇒ 停（等 Worker） |
| 该步是纯审批门（无命令） | 步 PENDING→RUNNING→SUCCEEDED ⇒ 继续下一步 |
| 全部步骤完成 | 实例 COMPLETED |
| 无步骤 | 实例直接 COMPLETED |

**入队幂等键**：`wf-{instanceId}-step-{stepId}` ⇒ 重复驱动**不会**重复入队（唯一约束兜底）。

### D-15 **命令状态机新增 `CANCELLED`**（终态，不可领取）

「取消 = 真的不再写外部系统」⇒ 待执行命令必须能落到**一个终态**（否则 Worker 仍会领走）。
`CANCELLED` 加进 `business_command` 的状态机与 **DB CHECK**。
⚠️ 实现方式：**直接改未发布的迁移 `0150`**（P5 全链只在分支上，生产在 `0143`），
**不伪造 `0153`**（迁移号不可回插，见 §5 占号）。

### D-16 **步骤状态机新增 `RUNNING → SKIPPED`**

命令在执行前被取消 ⇒ 这一步**其实没跑** ⇒ 步不能是 SUCCEEDED 也不能是 FAILED ⇒ `SKIPPED`。

### D-17 **取消面**（`POST /instances/{id}/cancel`）

- 实例 → CANCELLED；待审批任务 → CANCELLED（事件 `approval_cancelled`）；
  待执行命令 → CANCELLED；未开始的步骤 → SKIPPED。
- 权限：**提交人**本人，或**真实** admin；已终态 ⇒ 409；重复取消 ⇒ 幂等 200。
- ⚠️ **真实身份要求**：`DEFAULT_STUB_ROLES = ("user", "admin")`（`app/dependencies.py:67`）
  ⇒ stub 身份**默认就是 admin** ⇒ 伪造 `X-User-Roles: admin` 头即可取消**他人**实例。
  ⇒ 跨用户取消必须 `dbUserId is not None`（DB 用户 / JWT）。
  **自取消（提交人本人）保持放行**（自作用域，无需真实身份）。

### D-18 **幂等键归属校验**（`IDEMPOTENCY_KEY_CONFLICT`）

`submitInstance` 幂等重放时若 `instance.created_by != actor` ⇒ 409。
否则用户 B 用同幂等键会拿到**用户 A 的实例**（IDOR）。

### D-19 **执行器的「先提交再驱动」与补偿**

命令成功后：**先提交**（适配器写 + 命令终态 + 步骤成功 = 一个事务，原子），
**再**在独立事务里驱动下一步。驱动失败 ⇒ **不回滚已发生的外部写**（回滚后重领会重复写）
⇒ 改为**补偿实例**。
⚠️ 补偿必须**连带取消该实例的 PENDING 命令** ⇒ 否则「补偿」只是纸面动作，
Worker 照样领走待执行命令去写外部系统。

## 5. 迁移号占号（串行分配，与 WP-ONT-AI）

| 线 | 占用 | 备注 |
|---|---|---|
| **P5** | **0149 / 0150 / 0151 / 0152** | 0149 定义+实例 · 0150 步骤+命令 · 0151 审批+outbox · 0152 放宽 `audit_log` CHECK |
| **WP-ONT-AI** | **从 0153 起** | A2 / A3 / A4 依次占号，**不得**回插 0153 以下的号 |

已同步落档：[`../2026-11-16-ontology-discovery-ai/plan.md`](../2026-11-16-ontology-discovery-ai/plan.md)。

## 6. W0–W5 实际产出

| 批次 | 产出 | 关键文件 |
|---|---|---|
| **W0 表族与骨架** | 4 个迁移 + ORM（6 表）+ 迁移规范钉死（表/列/CHECK/唯一/注释**双向逐字**比对） | `alembic/versions/0149~0152*.py`、`app/domain/workflow_models.py`（518 行）、`app/tests/unit/test_workflow_models.py` |
| **W1 状态机与并发** | 纯函数状态机矩阵（fail-closed）+ 幂等键独立列 + 行锁推进 + 条件 UPDATE claim（`skip_locked`） | `app/services/workflow_state_machine.py`、`app/services/workflow_store.py`（788 行） |
| **W2 审批面** | 数据驱动审批矩阵（`definition_json.approvalMatrix`）+ 审批 API + **七守卫**（越权/重复审批/超时/对象级不可见/禁止自审批/真实身份/矩阵未覆盖） | `app/services/workflow_definition.py`、`app/services/workflow_approval_service.py`（634 行）、`app/api/v1/workflow.py` |
| **W3 Command 执行器** | 执行器骨架 + `Protocol` 端口层 + 模拟适配器 + 补偿钩子 + 失败隔离 + 重试语义 + **G6 反向守卫** | `app/services/workflow_command_ports.py`、`app/services/workflow_command_executor.py`、`app/tests/unit/test_workflow_write_gate.py` |
| **W4 审计与 Worker** | 审计接入（每步可追溯事件流 + `audit_log` 治理动作）+ 独立进程 Worker（开关门控）+ compose v1 片段 + 部署 runbook | `app/services/workflow_audit_service.py`、`app/workers/workflow_worker.py`、`compose.workflow-worker.centos7.fragment.yml` |
| **W5 评审与收口** | **编排器**（D-13/D-14）+ 取消面（D-15~D-18）+ 全路径 e2e + 性能核对 + 回归 + 文档 | `app/services/workflow_orchestrator.py`、`app/tests/unit/test_workflow_e2e_path.py`、`app/tests/unit/test_workflow_cancel*.py` |

## 7. 证据

### 7.1 测试

- 工作流全域单测（含状态机 / 存储 / 审批 / 执行器 / 端口 / 审计 / Worker / 部署片段 /
  写入口守卫 / 编排器 / 取消 / 全路径 e2e / 模型契约）**全绿**。
- **真实 PostgreSQL**（`qa_metadata_test_p5`，5434）+ 完整 API 链路（无 sqlite 内存库、
  不直接调 service 冒充集成）。
- **变异测试**：临时短路 `CommandExecutor._driveNext` ⇒ 全路径 e2e 文件 **3/4 失败**
  ⇒ 证明该文件对「上一棒跑完下一棒没人接手」有牙；随后从备份**字节还原**。

### 7.2 性能（20k 实例 + 20k 命令合成数据，实测 EXPLAIN）

| 路径 | 计划 | 耗时 |
|---|---|---|
| 领命令（claim） | PK Index Scan | **0.022 ms** |
| 审批超期清扫 | Bitmap Index Scan（`ix_approval_task_status`） | **0.617 ms** |

> ⚠️ 清扫当前**加载全部超期 PENDING 行**（部分索引 `due_at WHERE status='PENDING'`
> 是可选的后续优化，不阻塞本批）。合成数据已 TRUNCATE。

### 7.3 迁移纪律

- ORM ↔ 迁移的 CHECK 表达式**逐字一致**（单测钉住；`alembic check` 只比名不比式 ⇒ 只补名 = 假绿）。
- 每个迁移**只建自己的新表**，不 alter/drop 既有表（纯加法 DDL）。
- `downgrade()` 与 `upgrade()` **对称**（单测钉住）。

### 7.4 W5 评审整改台账（R1–R10）

> 每一轮都带 **RED→GREEN 证据**（先写守卫/用例看它红，再改实现看它绿），
> 关键项另做**变异取证**（把实现改坏 ⇒ 用例必须判红 ⇒ 用例非空转）。

| 轮 | 主题 | 关键内容 |
|---|---|---|
| **R1–R6** | W5 评审 **CRITICAL / HIGH** | R1 命令无调用方 ⇒ 补编排器（提交即入队）· R2 审批决策与推进改**两段事务**（决策是事实，不被推进失败抹掉）· R3/R4 审批与决策面**真实身份校验** · R5 命令载荷**命名空间化** `{request, step}`（用户输入顶不掉流程字段、注入不了适配器控制键）· R6 幂等键**按提交人私有**（消除 DoS 与 IDOR） |
| **R7** | G6 写入口守卫加固 | 守卫从「调用形态」扩到**属性赋值 / delete / merge / 裸字符串 / 带命名空间的 update**；扫描范围扩到 `backend/scripts/`；白名单改按**路径**；适配器**注册点收口**到组装根；同步**收回执行器 docstring 的过度声明**（库表写唯一入口 ≠ 外部写唯一入口） |
| **R8** | 存储层拆分 | `workflow_store.py` 1010 行 ⇒ 底座 + 三子存储 + 门面（多继承 ⇒ 调用点零改动）；补「P5 模块单文件 ≤ 800 行」机械守卫 |
| **R9** | 运维硬化 | Worker 清扫与执行**各自独立 try/except**（清扫失败不再拖垮命令领取）· `workerId` 按 `executed_by` **实际列宽 64** 截断（照 128 截 ⇒ 超长主机名写不进 ⇒ 命令永远执行不了）· 部署片段 `ENABLE_WORKFLOW` 改 `${ENABLE_WORKFLOW:-false}`（追加片段零副作用）· runbook 补 `APP_ENV=production` 断言与 `umask 077` |
| **R10** | MEDIUM / LOW 收尾 | **MEDIUM-7** 定义必须声明至少一步（缺步骤 ⇒ 零步骤实例 ⇒ **外部写零发生却报成功 = 假成功**）· **MEDIUM-6** 终态集合**单一出口**（只以 `workflow_state_machine` 为准）· **MEDIUM-5** 全部 >50 行函数拆分（存储层 5 + 审批面 2 + 服务层 2 + 执行器 2）· **LOW-13** 删死列 `workflow_instance.current_step`（迁移/ORM/DTO/存储层同删）· **LOW-14** 删死配置 `WORKFLOW_LEASE_SECONDS`（无人读）· **LOW-15/16** 幂等入队与「重复取消不重复留痕」的**变异取证** · **新守卫** 三个子存储**方法名（含私有）互不遮蔽**（门面是多继承 ⇒ 同名私有方法会被 MRO 静默遮蔽 ⇒ 本批真的撞过一次） |

**R10 变异取证（用例非空转的证据）**：

| 编号 | 变异 | 预期 | 实测 |
|---|---|---|---|
| LOW-15 | 删 `enqueueCommand` 的 `on_conflict_do_nothing` | 幂等用例报 `IntegrityError`（重复键 `wf-1-step-1`） | ✅ 判红 |
| LOW-16 | 往 `cancelInstance` 幂等短路分支塞一条 `recordCancel` | 服务层 + API 层两处审计行数断言失败 | ✅ 判红 |
| 新守卫 | 把命令子存储的私有方法改成与审批子存储同名 | 结构守卫判红 | ✅ 判红（`子存储方法名互相遮蔽：_recordApprovalRequestedEvent（…）`） |

**R10 回归**：workflow 单测 **292 passed**；CI 工具单测 **674 passed / 19 skipped**
（macOS bash 3.2 ⇒ 4 条 `mapfile` 用例本机跳过，CI 必跑）；`ruff check` 通过；
P5 模块**全部函数 ≤ 50 行、单文件 ≤ 800 行**（机械扫描 + 守卫用例）。

## 8. 风险与缓解（实施后状态）

| 风险 | 等级 | 缓解 | 状态 |
|---|---|---|---|
| 写操作安全面 | **高** | 独立开关（默认关）+ 独立回滚 + 安全评审 ×2 + 模拟器先行 + 幂等 + 审计 + 补偿 | 已做 |
| stub 身份默认 admin ⇒ 越权取消 | **高** | 跨用户取消绑定真实身份（D-17）；审批决策同样要求真实身份 | 已修 |
| 幂等键跨用户撞 ⇒ IDOR | **高** | 重放校验归属，不匹配 409（D-18） | 已修 |
| 补偿不取消待执行命令 ⇒ 纸面回滚 | 高 | 补偿先取消 PENDING 命令（D-19） | 已修 |
| 步骤定义含糊 ⇒ 实例假成功 | 高 | 失败关闭的解析期规则（D-13） | 已修 |
| 审批面写死 ⇒ A4 返工 | 高 | 预留 subject 型引用；A4 只保留业务审核记录 | 已做 |
| 一次上 9 个未验证迁移 | 高 | 先做「迁移链收口」批次（生产必须先到 0148） | **未做（部署前置）** |
| compose v1 写法踩坑 | 中 | 硬约束 + `config --quiet` + 复刻既有 v1 兼容片段 + 片段守卫单测 | 已做 |
| 并发丢步（identity map 不刷新） | 中 | `populate_existing=True` + 并发用例钉住 | 已做 |
| 既有红基线掩盖新失败 | 中 | 净新增 0 判据 + 失败集合逐片对差 | 回归时判定 |

## 9. 已知遗留（**不在本批**）

1. **真实 ERP/SRM 适配器**：只注册了 `po.write` 模拟适配器（只回显，不写外部系统）
   ⇒ **未注册的 `commandType` 会失败关闭**（`UnknownCommandTypeError` ⇒ 终态失败 ⇒ 触发补偿）。
2. **审批清扫的部分索引**（`due_at WHERE status='PENDING'`）未做。
3. **`workflow_outbox` 尚未被消费方使用**：表与毒丸防护已建，消费路径（发布事件）未接。
4. **Worker 未在生产部署**：生产默认无 Worker（`deploy-prod-1117.md` §9 是独立窗口）。
5. **迁移链收口批次未发**：生产 alembic 仍在 `0143`，本批上生产前必须先到 `0148`。

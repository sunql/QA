# P5 Workflow / Command / Approval —— 变更记录（**已实现**）

> 变更：[`plan.md`](./plan.md) · 部署：[`deploy-prod-1117.md`](./deploy-prod-1117.md)
> 上位：[`docs/update/1109/智能采购平台升级计划_日级排程_1109.md`](../../../docs/update/1109/智能采购平台升级计划_日级排程_1109.md) §11、§16
> 契约：[`docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md`](../../../docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md) §6 P5

| 项 | 值 |
|---|---|
| **状态** | **implemented**（W0–W4 已提交；W5 评审整改 R1–R11 全部完成；**已合入 main**） |
| **规模** | L（唯一 L 级事项） |
| **分支** | `feat/workflow-command`（worktree `/private/tmp/qa-p5`，从 `origin/main` 新开） |
| **合并** | PR **#148** squash ⇒ `main @ a217fef0`（2026-11-17）；**两批前端**另见 [`../2026-11-17-p5-workflow-ui/summary.md`](../2026-11-17-p5-workflow-ui/summary.md)（PR #150）· [`../2026-11-17-p5-workflow-definition-admin/summary.md`](../2026-11-17-p5-workflow-definition-admin/summary.md)（PR #150） |
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

### D-18 **幂等键按提交人隔离**（**不是** 409）

幂等作用域 = `(definition_id, created_by, idempotency_key)`（**库层唯一约束**）
⇒ 别人用同一个幂等键**只会拿到自己的实例** ⇒ 不存在「拿到别人的实例」的路径（IDOR）。

⚠️ **不再采用「键归属不符 ⇒ 409 `IDEMPOTENCY_KEY_CONFLICT`」**：那等于把「这个键存在」
泄露给所有人（**存在性预言机**）⇒ 任何人都能探测别人的键空间，也能把别人的键「占死」
（DoS）。**按提交人隔离后，这两条担心同时消失** ⇒ 库层约束与读路径一起改。
（全仓**没有** `IDEMPOTENCY_KEY_CONFLICT` 这个错误码。）

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

### 7.4 W5 评审整改台账（R1–R11）

> 每一轮都带 **RED→GREEN 证据**（先写守卫/用例看它红，再改实现看它绿），
> 关键项另做**变异取证**（把实现改坏 ⇒ 用例必须判红 ⇒ 用例非空转）。

| 轮 | 主题 | 关键内容 |
|---|---|---|
| **R1–R6** | W5 评审 **CRITICAL / HIGH** | R1 命令无调用方 ⇒ 补编排器（提交即入队）· R2 审批决策与推进改**两段事务**（决策是事实，不被推进失败抹掉）· R3/R4 审批与决策面**真实身份校验** · R5 命令载荷**命名空间化** `{request, step}`（用户输入顶不掉流程字段、注入不了适配器控制键）· R6 幂等键**按提交人私有**（消除 DoS 与 IDOR） |
| **R7** | G6 写入口守卫加固 | 守卫从「调用形态」扩到**属性赋值 / delete / merge / 裸字符串 / 带命名空间的 update**；扫描范围扩到 `backend/scripts/`；白名单改按**路径**；适配器**注册点收口**到组装根；同步**收回执行器 docstring 的过度声明**（库表写唯一入口 ≠ 外部写唯一入口） |
| **R8** | 存储层拆分 | `workflow_store.py` 1010 行 ⇒ 底座 + 三子存储 + 门面（多继承 ⇒ 调用点零改动）；补「P5 模块单文件 ≤ 800 行」机械守卫 |
| **R9** | 运维硬化 | Worker 清扫与执行**各自独立 try/except**（清扫失败不再拖垮命令领取）· `workerId` 按 `executed_by` **实际列宽 64** 截断（照 128 截 ⇒ 超长主机名写不进 ⇒ 命令永远执行不了）· 部署片段 `ENABLE_WORKFLOW` 改 `${ENABLE_WORKFLOW:-false}`（追加片段零副作用）· runbook 补 `APP_ENV=production` 断言与 `umask 077` |
| **R10** | MEDIUM / LOW 收尾 | **MEDIUM-7** 定义必须声明至少一步（缺步骤 ⇒ 零步骤实例 ⇒ **外部写零发生却报成功 = 假成功**）· **MEDIUM-6** 终态集合**单一出口**（只以 `workflow_state_machine` 为准）· **MEDIUM-5** 全部 >50 行函数拆分（存储层 5 + 审批面 2 + 服务层 2 + 执行器 2）· **LOW-13** 删死列 `workflow_instance.current_step`（迁移/ORM/DTO/存储层同删）· **LOW-14** 删死配置 `WORKFLOW_LEASE_SECONDS`（无人读）· **LOW-15/16** 幂等入队与「重复取消不重复留痕」的**变异取证** · **新守卫** 三个子存储**方法名（含私有）互不遮蔽**（门面是多继承 ⇒ 同名私有方法会被 MRO 静默遮蔽 ⇒ 本批真的撞过一次） |
| **R11** | 独立复审 **A–M**（本批） | **A** 消息模板占位符与调用点对不上 ⇒ 请求面 500（新增 **AST 守卫**钉住 `MSG_*.format(...)` 的实参覆盖）· **B** 生效版本改由**服务端**按 `(domain_code, capability_code)` 唯一确定（客户端自选 ⇒ 可绕过审批层级）+ 库层**部分唯一索引** `uq_workflow_definition_published` · **C** 跨用户**读**面同样要求**真实身份**（stub 身份默认带 admin ⇒ 能读穿工作流面）· **D** 补偿时被取消命令的步骤要记 **SKIPPED**（否则步骤吊在 RUNNING）· **E** 命令失败时**保留 claim**（用 SAVEPOINT 取代整事务回滚；整回滚会抹掉归属与尝试次数 ⇒ 出现「没人执行过」的假象）· **F** 状态机补 `EXECUTING → ABORTED`（终态失败落库即终态；DB CHECK 早已允许）· **G** 审批超时清扫**批量上限** + 并发决策**不拖垮整批**（锁序恒为 instance → task）· **H** Worker 一轮**排空**未领取命令（否则吞吐被 2s 轮询间隔压死 ⇒ ≈30 条/分钟）+ 可重试失败**不在同轮热转**（毒丸防护）· **I** 审批审计的 `before` 必须是**决策前**快照 · **J** 审批任务未新建时**不重复留审计** · **K** 删死列 `workflow_instance.business_key` / `result_json`（全仓无人写 ⇒ 永远 NULL ⇒ 接口恒 null）· **L** D-18 文档与实现对齐（幂等键**按提交人隔离**，不是 409）· **M** outbox 注释纠偏（`workflow_outbox` **无消费者**，不再写「worker 据此路由」） |
| **R12** | 独立复审（**MEDIUM / LOW / 文件规模**） | **R12-1** 补偿审计 `before` 写死 `FAILED` ⇒ **假审计**（审计说实例补偿前已失败，而库里它还在跑）⇒ 改记**补偿前的真实状态** · **R12-2** 审批决策「越权」回 **403** ⇒ **存在性预言机**（拿到任务 id 就能枚举出「哪些审批任务存在」）⇒ 统一 **404**（与「任务不存在」**逐字同形**）；「矩阵条目失效」的 409 同样收敛成 404（配置漂移只进**服务端日志**）；**自审批仍 403**（actor 本就能看见它 ⇒ 不是存在性泄露，且必须与「你不是审批人」可区分） · **R12-3** 超时清扫**整批共用一个事务** ⇒ 一条脏数据把**本轮所有**超期任务全部作废（实例永远卡在「等待审批」）⇒ 改**逐条 SAVEPOINT**（只回滚那一条） · **R12-4** 适配器错误消息**原样落库** ⇒ **凭据 / PII 进库**（导出 / 备份全带着，**不可逆**） ⇒ 落库前**净化**（抹值留键 / 压控制字符 / 截断） · **R12-5** 错误**不分类** ⇒ 基础设施错误与业务拒绝混成一句话 ⇒ **处置动作反了** ⇒ `classifyError`（INFRA / ADAPTER），**超时**归 INFRA（可重试） · **R12-6** 适配器**无时限** ⇒ 一个卡死的下游能把 Worker 那一轮**永远占住**（事务不提交 ⇒ 连接不释放 ⇒ 连接池耗尽 ⇒ 全部命令停摆）⇒ 加超时 · **R12-7** 失败写回可能带**旧次数** ⇒ 次数**回退** ⇒ 毒丸命令永远达不到上限 ⇒ 无限重试 ⇒ 次数**只增不减** · **R12-8** Worker **一轮排空**无上限 ⇒ 加 `drainBatchSize` · **R12-9** 超时审计 `before` 写死 `PENDING` ⇒ 记**真实前态**（审批矩阵里 TIMEOUT 只能来自 PENDING ⇒ 今天**恰好**同值 ⇒ 钉的是**契约**） · **R12-10** SAVEPOINT 回滚可能**抹掉 claim 事件** ⇒ 取证后钉成**契约**（claim 写在 SAVEPOINT 之外 ⇒ 幸存） · **R12-11** P5 单文件逼近 **800 行** ⇒ 拆 4 个文件（执行器 / 补偿 / 清扫 / 失败收敛）；行数守卫**只扫 `app/`** ⇒ 扩到 `app/tests/`（并用临时 801 行文件验证守卫**非空转**） |

**R10 变异取证（用例非空转的证据）**：

| 编号 | 变异 | 预期 | 实测 |
|---|---|---|---|
| LOW-15 | 删 `enqueueCommand` 的 `on_conflict_do_nothing` | 幂等用例报 `IntegrityError`（重复键 `wf-1-step-1`） | ✅ 判红 |
| LOW-16 | 往 `cancelInstance` 幂等短路分支塞一条 `recordCancel` | 服务层 + API 层两处审计行数断言失败 | ✅ 判红 |
| 新守卫 | 把命令子存储的私有方法改成与审批子存储同名 | 结构守卫判红 | ✅ 判红（`子存储方法名互相遮蔽：_recordApprovalRequestedEvent（…）`） |

**R11 证据（RED→GREEN；变异取证用于「用例非空转」）**：

| 编号 | 主题 | RED 观测 | GREEN |
|---|---|---|---|
| D | 补偿取消命令的步骤 | 步骤停在 `RUNNING`（没跑过却像跑过） | ✅ 记 `SKIPPED`；已补偿过的步骤仍记 `COMPENSATED` |
| E | 失败时 claim 不被回滚 | 观测到 `['PENDING']`（期望 `['EXECUTING']`）⇒ 整事务回滚把 claim 抹了 | ✅ SAVEPOINT 只回滚适配器那一段 |
| F | 终态失败同样保留 claim | 同上（`FAILED` 路径） | ✅ 落 `ABORTED`，claim 保留 |
| G | 清扫批量上限 / 并发决策 | 无上限；并发决策时整批抛 `ConflictError`（APPROVED → TIMEOUT） | ✅ 上限 200；并发的那条**跳过**，其余照扫 |
| H | 一轮排空多条命令 | **变异取证**：把实现退回「单条领取」⇒ 第 2 条仍 `PENDING` | ✅ 一轮排空 3 条 |
| H′ | 可重试失败不在同轮热转 | **变异取证**：把排空改成含 `FAILED` ⇒ 同轮被领 3 次 ⇒ 落 `ABORTED` | ✅ 一轮只试一次（`attempts == 1`） |
| I | 审计 `before` 是决策前快照 | `before.approverUser` 里出现**变更后**的签署人 | ✅ 决策前取值传入 |
| J | 任务未新建不重复留审计 | 并发窗口下多留一条 `WORKFLOW_APPROVAL_REQUESTED` | ✅ 只在 `created` 时留 |
| K | 删死列 | 期望列集合与 ORM 不符（`business_key` / `result_json` 在列） | ✅ 迁移 / ORM / DTO / 存储层同删 |

⚠️ **H 的一个假绿陷阱（记录下来，别再踩）**：断言从**同一个会话**读回命令行时，
ORM 的 **identity map** 里还留着入队时那个「`PENDING`」对象 ⇒ 不加
`populate_existing=True` 就会读到**旧值** ⇒ 用例判红但**实现是对的**。
（症状：Worker 日志明明打了「命令 3 执行结果 SUCCEEDED」，断言却看到 `PENDING`。）

**R10 回归**：workflow 单测 **292 passed**；CI 工具单测 **674 passed / 19 skipped**
（macOS bash 3.2 ⇒ 4 条 `mapfile` 用例本机跳过，CI 必跑）；`ruff check` 通过；
P5 模块**全部函数 ≤ 50 行、单文件 ≤ 800 行**（机械扫描 + 守卫用例）。

**R11 回归**：workflow 单测 **317 passed**（真实 PG `qa_metadata_test_p5`，5434）；
`ruff check`（本批改动文件）通过；`alembic check` **无漂移**（改过的 0149 / 0151
原地改后**重建测试库**再验 ⇒ alembic 会比**列注释**，注释改了也必须重建）；
P5 模块**全部函数 ≤ 50 行、单文件 ≤ 800 行**（机械扫描 + 守卫用例）。

**R12 证据（RED→GREEN；**变异取证**用于「用例非空转」）**：

| 编号 | 主题 | RED 观测 | GREEN |
|---|---|---|---|
| R12-1 | 补偿审计前态 | 审计 `before` = `{"status": "FAILED"}`，而库里实例当时是 `RUNNING` | ✅ 记 `{"status": "RUNNING"}`（实测取值） |
| R12-2 | 越权 = 不可见 | 越权回 **403** + `APPROVAL_NOT_AUTHORIZED` + `details.taskId` ⇒ 状态码即预言机 | ✅ 越权 **404** 且响应体与「任务不存在」**除 id 外逐字相同**；**变异取证**：把分支改回 403/409 ⇒ **5 条用例判红**（含 2 条 API 层 + 3 条服务层 + 1 条矩阵失效） |
| R12-3 | 清扫逐条隔离 | 注入的脏数据**打掉整轮**（抛 `RuntimeError` ⇒ 一条不落、其余也丢） | ✅ 失败那条仍 `PENDING`，其余**照常**落 `TIMEOUT` |
| R12-4 | 错误消息净化 | 落库消息里**含** `s3cr3tPwd` / `hunter2` / 换行 | ✅ 抹值留键（`password=***`）、压平换行、截断到上限 |
| R12-5 | 错误归类 | `TimeoutError` 与 `ValueError` **同归一类** | ✅ INFRA（可重试）vs ADAPTER（业务拒绝）可区分 |
| R12-6 | 适配器超时 | 卡死适配器 ⇒ Worker 那一轮**永不返回** | ✅ 超时落 `FAILED` 且 `isTerminal=False`，命令不留在执行中 |
| R12-7 | 次数只增不减 | 并发失败写回把次数**改回 1** | ✅ 仍是 3（毒丸防护的前提） |
| R12-8 | 排空上限 | 一轮领走 3 条（超 `drainBatchSize=2`） | ✅ 一轮 2 条，第 3 条留 `PENDING` |
| R12-9 | 超时审计前态 | 写死 `PENDING` | ✅ 取**落 TIMEOUT 前**的真实状态（契约钉） |
| R12-10 | claim 事件幸存 | （取证）SAVEPOINT 在 claim 内 ⇒ 事件被抹 | ✅ `command_claimed` / `command_failed` **都在**事件流里 |
| R12-11 | 文件规模 | 执行器 / 审批 / 补偿三个测试文件逼近 800 行 | ✅ 拆 4 个文件（541 / 617 / 292 / 370 行）；守卫扩到 `app/tests/`；**变异取证**：塞一个 801 行文件 ⇒ 守卫判红 |

**R12 回归**：workflow 单测 **324 passed**（真实 PG `qa_metadata_test_p5`，5434）；
`ruff check`（本批改动文件）通过；P5 模块**全部函数 ≤ 50 行、单文件 ≤ 800 行**
（机械扫描 + 守卫用例，覆盖 `app/` **与** `app/tests/`）。

⚠️ **R12 附带纠正一条「假前提」（别再踩）**：迁移 0149 / 0151 的 docstring 与
`test_workflow_models.py` 曾写着「**0149 未在任何持久环境执行 ⇒ 可原地改**」——
**这句话是错的**：0149–0151 **早已在开发库（5433）执行过**（开发期反复
`upgrade head` 建表 + 跑用例）⇒ 随后的原地编辑**没有回灌**那个库
⇒ 开发库的 P5 表结构**已陈旧**（多 3 列 / `created_by` 仍可空 / 唯一约束少
`created_by` 一列 / 3 处列注释过期）。
⇒ 生产**不受影响**（生产停在 `0143` ⇒ 从未跑过 0149）；测试库**不受影响**
（R11 改完后**重建过**）；**只有开发库**需要一次性重建（已执行 ⇒ P5 漂移 = 0，
脚本与实测见 `deploy-prod-1117.md` §14）。

### 7.5 既有红基线取证（净新增失败 0 判据）

跑邻接套件时捞到 **1 条红**：
`app/tests/integration/test_app_wiring.py::TestRealAppRouteTable::test_no_route_registered_after_catch_all_mount`
⇒ 断言「真实 app 路由表里找不到 catch-all Mount（MCP 挂载点）」。

**根因（与 P5 无关）**：守卫找的是 `Mount("")`（catch-all），
而当前装配是 `app/main.py:803` 的 `Mount("/mcp", ...)`（`fastmcp http_app(path="/mcp")`）
⇒ **守卫的前提已过期**（`main.py:778-781` 的注释还写着 `Mount("")` ⇒ 注释也一起漂了）。
P5 只挂 `workflow.router`（`include_router`，非 Mount）⇒ **不触碰挂载面**。

**取证（证明是既有红基线，不是本批净新增）**：

| 步骤 | 命令 / 观测 | 结果 |
|---|---|---|
| ① 建基线 worktree | `git worktree add /private/tmp/qa-p5-baseline origin/main`（detached `86ff4a1f`） | 基线树独立于本批工作区 |
| ② 建隔离库 | `CREATE DATABASE qa_metadata_test_p5base`（5434 同容器，**不复用** `qa_metadata_test_p5`） | 基线的 alembic 链止于 `0148` ⇒ 复用会报 `Can't locate revision '0152'` ⇒ 必须另起库 |
| ③ 基线跑同一条用例 | `pytest app/tests/integration/test_app_wiring.py::TestRealAppRouteTable::test_no_route_registered_after_catch_all_mount -q` | **FAILED**，断言文本与本批工作区**逐字相同** |
| ④ 清理 | `git worktree remove --force` + `DROP DATABASE qa_metadata_test_p5base` | 无残留 |

⇒ 结论：**同一断言在 `origin/main` 上就已经红** ⇒ 属**既有红基线**
⇒ 本批对该项的**净新增失败 = 0**（不构成合并闸门的阻断项）。
⚠️ 取证口径提醒：基线 worktree 的迁移链与本批不同 ⇒ **必须另起隔离库**，
否则失败原因会被迁移链差异顶掉 ⇒ 取证结论失真。

## 8. 风险与缓解（实施后状态）

| 风险 | 等级 | 缓解 | 状态 |
|---|---|---|---|
| 写操作安全面 | **高** | 独立开关（默认关）+ 独立回滚 + 安全评审 ×2 + 模拟器先行 + 幂等 + 审计 + 补偿 | 已做 |
| stub 身份默认 admin ⇒ 越权取消 | **高** | 跨用户取消绑定真实身份（D-17）；审批决策同样要求真实身份 | 已修 |
| 幂等键跨用户撞 ⇒ IDOR | **高** | 幂等作用域**按提交人隔离**（库层唯一约束含 `created_by`）⇒ 撞键各得各的实例，不存在 IDOR，也不泄露键空间（D-18） | 已修 |
| 补偿不取消待执行命令 ⇒ 纸面回滚 | 高 | 补偿先取消 PENDING 命令（D-19） | 已修 |
| 步骤定义含糊 ⇒ 实例假成功 | 高 | 失败关闭的解析期规则（D-13） | 已修 |
| 审批面写死 ⇒ A4 返工 | 高 | 预留 subject 型引用；A4 只保留业务审核记录 | 已做 |
| 一次上 9 个未验证迁移 | 高 | **已拆两个窗口**：① 迁移链收口 `0143→0148`（源只认 `86ff4a1f`）② P5 本体 + 前端（源 `1e04b46e`）—— 见 `deploy-prod-1117.md` §0.2 / §0.3 | **待生产执行（部署前置）** |
| compose v1 写法踩坑 | 中 | 硬约束 + `config --quiet` + 复刻既有 v1 兼容片段 + 片段守卫单测 | 已做 |
| 并发丢步（identity map 不刷新） | 中 | `populate_existing=True` + 并发用例钉住 | 已做 |
| 既有红基线掩盖新失败 | 中 | 净新增 0 判据 + 失败集合逐片对差 | 回归时判定 |

## 9. 已知遗留（**不在本批**）

> 口径：**已实现但有意不闭合**的缺口。每条写清「现象 ⇒ 影响 ⇒ 当前处置 ⇒ 何时闭合」，
> 免得下一批读文档时把「没做」误读成「忘了」。

### 9.1 能力与授权

1. **真实 ERP/SRM 适配器**：只注册了 `po.write` 模拟适配器（只回显，不写外部系统）
   ⇒ **未注册的 `commandType` 会失败关闭**（`UnknownCommandTypeError` ⇒ 终态失败 ⇒ 触发补偿）。
2. ⚠️ **能力授权缺口（需产品决策）**：仓内**没有** (domain, capability) 维度的权限模型
   ⇒ 现状是「**任何已登录用户**都可以提交**任何已发布能力**的工作流」。
   审批矩阵只管「提交之后谁来批」，**管不了「谁有权提交」**。
   ⇒ 需产品明确：(a) 是否按域/能力做授权；(b) 授权数据落在哪（RBAC 扩展 / 独立表）。
   在此之前**不要**把 P5 暴露给非试点用户。

### 9.2 一致性与恢复

3. **崩溃窗口 ⇒ 外部写可能重复**：claim → 适配器写 → 终态落库是**一个事务**；
   Worker 在中间死掉 ⇒ 事务回滚 ⇒ 命令回到 `PENDING` ⇒ **被重领 ⇒ 外部写再来一次**。
   缓解只能靠 `idempotency_key`（写外部系统时**必须**带上，外部侧要幂等）。
   ⇒ 真实适配器接入时，「外部系统按 `idempotency_key` 幂等」是**接入前置条件**，不是可选项。
4. **实例可能停在 `RUNNING`/`FAILED` 且无可执行命令**：终态失败会走补偿把实例收敛到
   `COMPENSATED`，但**补偿本身失败**（外部系统不可用）时，实例可能停在 `RUNNING`/`FAILED`
   而没有可领取的命令 ⇒ 当前**没有 reconciler**（自动收敛器）⇒ 需人工处置。
   reconciler 属后续批。

### 9.3 审批与审计

5. ~~**审批任务存在性预言机**~~ ⇒ **R12 已闭合**：审批决策面「任务存在但越权」现在
   返回 **404**，且响应体与「任务不存在」**除 id 外逐字相同**（无 `detail`、无 `details`）
   ⇒ 差分探测不出来。（取消面**本来就没有**这个问题：不可见一律 404。）
   ⚠️ **唯一保留的 403 是「自审批」**（四眼）：actor 本来就是这条任务的审批人 ⇒
   他**已经看得见它** ⇒ 明说「不能审批自己发起的」不是存在性泄露，且**必须**与
   「你不是审批人」可区分（否则调用方分不清是「没权限」还是「规矩不让批」⇒ 会去找错人）。
6. **审批人集合在**决策时**从当前生效定义重解析**：定义在「发起审批」与「决策」之间改了审批
   矩阵 ⇒ 以**决策时**的矩阵为准 ⇒ 有意为之（矩阵是安全边界 ⇒ 用最新边界），
   但**审批任务行里存的是发起时的 `approver_role`** ⇒ 事后看任务行会与实际判据不一致。
7. **实例终态流转不进 `audit_log`**：`audit_log` 只记治理动作（提交 / 发起审批 / 决策 / 超时 /
   补偿 / 取消），实例与步骤的状态流转在 `workflow_outbox` 事件流里
   ⇒ 查「实例为什么变成 FAILED」要同时看两处。
8. **审计无法区分伪造的 stub 身份**：`audit_log` 只有 `actor` 字符串，**没有「这条是不是真实
   身份」的列** ⇒ 事后分不出「真用户」与「伪造 `X-User-Id` 的客户端」。
   缓解：跨用户写面（审批决策 / 取消他人实例）**本身**要求真实身份 ⇒ 那些行必然是真身份；
   但**自作用域**的行仍可能是 stub。

### 9.4 表与部署

9. **`workflow_outbox` 没有消费者**：表与毒丸防护已建，但**消费路径（发布事件）未接**
   ⇒ 这张表**只写不读** ⇒ 事件流是**给人看 / 查 SQL** 的追溯记录，**不是**机器路由表
   ⇒ **行数无上限增长**（无归档策略）⇒ 上生产前需定「保留期 + 归档」。
10. **审批清扫的部分索引**（`due_at WHERE status='PENDING'`）未做。
11. **Worker 未在生产部署**：生产默认无 Worker（`deploy-prod-1117.md` §9 是独立窗口）。
12. **迁移链收口批次未发**：生产 alembic 仍在 `0143`，本批上生产前必须先到 `0148`。
13. **开发库（5433）的 P5 表结构曾陈旧**：0149–0151 在开发期**已执行过** ⇒ 随后的原地
    编辑（删 3 个死列 / `created_by` 改 NOT NULL / 幂等唯一约束加 `created_by`）**没回灌**
    ⇒ 开发库结构 ≠ 当前迁移文件（**已一次性重建** ⇒ P5 漂移 = 0，脚本见
    `deploy-prod-1117.md` §14）。⇒ **教训**：「迁移没在持久环境执行过」这句话**必须实测**
    （查 `alembic_version` + 比列），不能凭「刚写的迁移」推断 ——
    开发期反复 `upgrade head` 就已经把迁移跑进开发库了。
14. **既有红基线：catch-all Mount 守卫已过期**（**不是本批引入**，取证见 §7.5）：
    `test_app_wiring.py::test_no_route_registered_after_catch_all_mount` 断言「真实 app 路由表
    里有 catch-all `Mount("")`」，而当前装配是 `Mount("/mcp")` ⇒ 该守卫**永远红**
    ⇒ `origin/main` 上同样红（基线 worktree + 隔离库实测）。
    ⇒ 本批**不改**：① 修它要动 `app/main.py` 的挂载面（改回 `Mount("")` 会让
    **其后所有路由被遮蔽** —— 那正是 2026-09-25 `/api/v1/health` 静默 404 的根因）
    ⇒ 属**独立议题**；② 改既有 integration 测试 = 改 CI 面 ⇒ 触发闸门 exit 6 仲裁面。
    ⇒ 建议：**单开一批**，把守卫改成「最后一个 Mount 之后不得再注册路由」
    （`Mount("/mcp")` 只遮蔽 `/mcp` 前缀 ⇒ 原语义需重新界定），并同步 `main.py:778-781` 的注释。

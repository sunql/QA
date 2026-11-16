# P5 Workflow / Command / Approval —— 执行计划（D38–D57）

> **状态**：**planned**（本文件是**规划**，尚未开工；本批无代码）
> 上位：[`docs/update/1109/智能采购平台升级计划_日级排程_1109.md`](../../../docs/update/1109/智能采购平台升级计划_日级排程_1109.md) §11 ·
> 契约来源：[`docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md`](../../../docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md) §6 P5
> 兄弟线：[WP-ONT-AI](../2026-11-16-ontology-discovery-ai/plan.md)（A2/A3/A4，见 §8 排序裁决）

| 项 | 值 |
|---|---|
| **变更** | P5 Workflow / Command / Approval：工作流子系统（表族 + 状态机 + 审批 + Command 执行器 + Worker） |
| **分支** | 待开（建议 `feat/workflow-command`，**从 `origin/main` 新开 worktree**） |
| **迁移版本** | **待分配**（仓内 head 现为 `0148`；本批预计占用 **3 个**迁移 ⇒ 见 §3，**编号必须与 WP-ONT-AI 串行分配**） |
| **开关** | `ENABLE_WORKFLOW`（默认 **false**；关 ⇒ 路由 404 + Worker 不轮询） |
| **规模** | **L**（唯一 L 级事项 ⇒ 独立立项 / 独立开关 / 独立回滚 / 独立评审） |
| **净开发日** | **17 天**（计划表 20 天含 D55–D57 三天缓冲） |
| **前置** | ① 迁移链上生产（生产现 `0143`，仓内 head `0148`）② 部署线空闲（1114/1116 收口）③ 迁移号串行分配 |

---

## 1. G6 判据（本批的完成定义）

> **G6**：写操作全部经 Command；Agent 仍只读；默认关闭可上线；回滚 = 关闭开关 + 停 Worker。

| 判据项 | 口径（可机械判定） |
|---|---|
| 写操作全部经 Command | 守卫：除 Command 执行器与其适配层外，**任何服务层不得直接写** `workflow_*` 表 ⇒ 源码级反向守卫（照 `Test生产侧不许绕过闸门` 的形态） |
| Agent 仍只读 | 守卫：Agent 工具层**不得**暴露 Command/审批写入口（只读工具清单断言） |
| 默认关闭可上线 | `ENABLE_WORKFLOW` 默认 `false`；关时路由 404、Worker 不建会话、不轮询 |
| 回滚 = 关开关 + 停 Worker | 回滚手册可执行；`downgrade()` 可**独立**撤掉本批迁移 |
| 状态机全路径 | 创建 / 推进 / 暂停 / 完成 / 取消 / 失败重试 / 超时 / 重复提交 / 并发推进 —— 全部有用例 |
| 审批三守卫 | 越权 / 重复审批 / 超时 三类用例通过 |
| 净新增失败 0 | CI 失败集合对差（不是全绿） |

---

## 2. recon 事实（动手前先查出来的，不是猜的）

| # | 事实 | 影响 |
|---|---|---|
| 1 | 仓内**零** workflow / approval / business_command 实现（`grep -ril approval backend` 无命中）；**无任何队列库依赖**（`pyproject.toml` 无 celery/arq/dramatiq/APScheduler） | P5 = 新造业务表 + **复用**既有骨架；**不引入新依赖**（D-12） |
| 2 | 既有 **5 个 Worker 是独立进程**轮询 PG 表（`workers/agent_orchestration_worker.py:1-19`、`agent_scheduler_worker.py:1-12`、`audit_worker.py`、`evaluation_report_scheduler_worker.py`、`knowledge_reconcile_worker.py`），flag 关即 return（`agent_orchestration_worker.py:119-123`） | Worker **照抄这个模式**新建，不造调度器 |
| 3 | 既有 outbox 两套：`outbox_service.py:38-97`（审计，**只 add 不 commit**，同事务原子提交）与 `research_event_outbox.py:38-91`（单调 seq + 游标回放）；`AuditOutbox` ORM 含 `attempts`/`last_error`/`processed_at` 毒丸防护（`domain/models.py:858-905`） | `workflow_outbox` **新建表**（契约明令「不共用表」），但**照抄**同事务 enqueue + 毒丸防护结构 |
| 4 | 既有状态机是**纯函数矩阵**：`kpi_status_machine.py:29-42`（`_KPI_TRANSITIONS` MappingProxyType）+ `:79-116`（`assertKpiTransition` 抛 `ConflictError(409, "INVALID_STATUS_TRANSITION")`，未知状态 fail-closed） | 工作流状态机照此写（纯函数 ⇒ 可单测，无需 DB） |
| 5 | 审计动作有**白名单**：`audit_service.py:33-48` `_VALID_ACTIONS`（CREATE/UPDATE/DELETE/RESTORE/REUSE/auth.*），`record()` 只 add 不 commit（`:62-123`） | P5 新动作（APPROVE/REJECT/COMPENSATE/WORKFLOW_*）**必须扩白名单**，否则 ValueError |
| 6 | 幂等键现状：只在 JSONB 里（`multi_step_models.py:48` `idempotency_keys: JSONB`），**无独立唯一约束列**；HTTP 头幂等先例 `chat.py:200-204`、`research.py:460-468` | 幂等**新造独立列 + `UniqueConstraint`**（D-2）；ORM 与迁移 DDL **必须同形**（`models.py:455-456`、`:542`、`:2570` 的既有坑） |
| 7 | 并发件齐全：`with_for_update(skip_locked=True)`（`agent_orchestration_store.py:111`、`:246`）与条件 UPDATE claim（`:193`、`:652`、`:682`）；**坑**：SQLAlchemy 不刷新 identity map，须 `execution_options(populate_existing=True)`（`multi_step_persistence.py:206-225`） | 推进/claim 用行锁 + `populate_existing`；**不新造乐观锁**（仓内无先例） |
| 8 | 迁移链：最新 `0148_audit_reuse_action.py`（`down_revision="0147"`），下一号 **0149**；单链递增；规范硬约束：revision ≤ 32 字符、`downgrade` 对称、**迁移与代码同批上线**、幂等 DDL、每阶段最多一个小迁移（`Harness/rules/数据库环境使用规范.md:56-81`） | **6 张表拆 3 个迁移**；编号与 WP-ONT-AI **串行分配** |
| 9 | 开关形态：`config.py` camelCase + `alias="ENABLE_*"`，`default=False`（`:175`/`:181`/`:189`/`:197`/`:200`），`AGENT_ORCHESTRATION_*` 参数组在 `:201-209`；路由注册模板 `main.py:649-666`（`if settings.enableX:` 内才 import + include_router） | `enableWorkflow` + `WORKFLOW_*` 参数组照抄；**不进生产 .env**（`SECRET_KEY` 是 Fernet 主密钥，改了旧 token 全失效） |
| 10 | 生产部署硬约束：入口唯一权威 = 运行中容器 label 读出的 `/opt/qa-system/docker/docker-compose.centos7.yml`（**Compose v1 1.29.2**）；`!override` 需 ≥2.24（v1 解析即炸，`docker/docker-compose.centos.yml:8`）；现有 worker 用 `profiles:`（v2 特性，`docker-compose.yml:186-207`）⇒ **禁止照抄**；部署零新增中间件、镜像版本不动（`跨板块扩展_1109.md:59`、`:195`） | P5 worker **必须复用现有 backend 镜像**（照 `:195-198` 不写 `image:` 键的做法），片段 **v1 兼容**，禁 `!override`/`profiles`/`docker compose` 子命令 |
| 11 | 有 DDL ⇒ 上线前必须 `pg_dump` 备份（`数据库环境使用规范.md:15`、`:93`）；零 DDL 批次才可不备份 | P5 上线 = **有 DDL 批次** ⇒ pg_dump 是硬前置 |
| 12 | 计划里的版本号会过期：计划写「head 0142」，实际仓内 head `0148`、生产 `0143` | **D38 第一件事 = 确认当时真实 head**，不照抄计划 |
| 13 | 端口/适配抽象先例：`research_agent_ports.py:309`（`UsageRecorder(Protocol)`）、`:325`（`Reporter(Protocol)`）、`:344`、`:446`（`MeteredClient`）；契约测试先例 `tests/unit/test_knowledge_facade_contract.py` | ERP/SRM 适配 = Protocol + 模拟器 + 契约测试，照抄 |
| 14 | 现有 KPI 向量/图同步是**请求内 asyncio fire-and-forget**（`kpi_vector_graph_sync.py:614-615`，进程内、无持久化、无重试） | **不可复用**为 P5 执行器；P5 要有持久化 + 重试（这是「可靠执行」与「尽力而为」的本质差别） |
| 15 | 测试硬约束：真实 PostgreSQL + 完整 API 链路，禁 sqlite 内存库/直接调 service（`CLAUDE.md` 规则 7）；禁新建 integration 测试文件（CI 分片按排序下标 mod 4 轮转 ⇒ 假 net-new） | 集成用例**并入既有文件**；新文件只放 `unit/` |

---

## 3. 范围拆分（W0–W5 ← D38–D57）

| 批次 | 对应日次 | 内容 | 完成定义 |
|---|---|---|---|
| **W0 表族与骨架** | D38–D39 | 3 个迁移（表族分组）+ ORM + 迁移规范钉住 | upgrade/downgrade 往返通过；`alembic check` 绿 |
| **W1 状态机与并发** | D40–D41 | 状态机矩阵（纯函数）+ 幂等键列 + 行锁推进 + 条件 UPDATE claim | 全路径用例通过；重复提交只执行一次；并发推进不丢步 |
| **W2 审批面** | D42–D43 | 审批任务生成 + **数据驱动审批矩阵**（`definition_json`）+ 审批 API + 三守卫 | 矩阵变更只改数据；三守卫用例通过 |
| **W3 Command 执行器** | D44–D46 | 执行器骨架 + 模拟适配器 + 补偿钩子 + 失败隔离 + 重试语义 + 外部写接口抽象（Protocol） | 注入失败后补偿执行；新适配器不改核心 |
| **W4 审计与 Worker** | D47–D48 | 审计接入（每步可追溯）+ Worker 集成（开关门控）+ **compose v1 片段** + 部署 runbook | `docker-compose config --quiet` 通过；默认不启动；开启后跑通 |
| **W5 评审与收口** | D49–D57 | 安全评审 ×2 + 全路径 e2e + 性能容量核对 + 回归 + 文档 + PR（+ 3 天缓冲） | CRITICAL/HIGH 清零；净新增失败 0；演练手册可执行 |

---

## 4. 关键裁决（实施前先读，避免走偏）

### D-1 审批对象模型必须**开放**（为 WP-ONT-AI A4 预留）

`approval_task` 的被审对象**不能**写死成工作流实例 ⇒ 用 **subject 型引用**：
`subject_type`（字符串，如 `workflow_instance` / `ontology_proposal`）+ `subject_id`（UUID）
+ 审批矩阵以 `(subject_type, capability_code, risk_level)` 为键。
**为什么**：WP-ONT-AI 的 A4「四眼审核」是同构的审批需求；若 P5 把审批面写死，
A4 落地时要么造第二套审批栈、要么回头改 P5 的表 ⇒ **要么返工要么重复**。
⇒ **在 P5 就把审批面做成可复用的通用面**（A4 复用它，A4 只保留「谁签的字」的业务记录）。
**反向风险**：过早泛化 ⇒ YAGNI。⇒ 折中：**只泛化一维**（subject_type/subject_id），
审批矩阵的键固定三段，不做可编程规则引擎。

### D-2 幂等：新增独立唯一约束列，不复用 JSONB

`business_command` 加 `idempotency_key` **独立列** + `UniqueConstraint`
（作用域 = `(command_type, idempotency_key)`）。
**为什么**：JSONB 里存幂等键无法做唯一约束 ⇒ 并发下「重复提交只执行一次」保证不住。
**坑**：ORM 与迁移 DDL 必须**同形**（`UniqueConstraint` vs `Index(unique=True)` 不同形 ⇒ alembic 提议重建表）；
迁移用**幂等 DDL**（prod 是 dump 恢复来的库）。

### D-3 并发：行锁 + `populate_existing`，不做乐观锁

推进/claim 一律 `with_for_update()`；多 worker 抢同一条用 `skip_locked=True`；
**读 ORM 后必须 `execution_options(populate_existing=True)`**（否则拿到 identity map 里的旧值 ⇒ 并发下丢步）。
**不做乐观锁**：仓内无 `version_id` 先例 ⇒ 新造会引入无参照的语义。

### D-4 状态机：纯函数矩阵，未知状态 fail-closed

照 `kpi_status_machine.py`：转移矩阵是 `MappingProxyType`（不可变）+ `assert*Transition()` 抛
`ConflictError(409, "INVALID_STATUS_TRANSITION")`；**未知状态 ⇒ 失败关闭**（不静默不动）。
纯函数 ⇒ 不吃 DB，可单测；DB 层只做「读当前状态 + 校验转移 + 写」。

### D-5 `workflow_outbox` 新建表（不共用），结构照抄既有

契约明令「异步沿用 outbox 模式（可复用 `ResearchEventOutbox` 的设计，**不共用表**）」。
结构照抄 `AuditOutbox`：`attempts` / `last_error` / `processed_at`（毒丸防护）+ 部分索引；
写入用 `session.add` **不 commit**，与业务同事务原子提交。

### D-6 审计：扩动作白名单 + 两段式

`audit_service._VALID_ACTIONS` 扩 `APPROVE` / `REJECT` / `COMPENSATE` / `WORKFLOW_*`。
每步可追溯 = `OutboxService.enqueue` + `AuditService.record` **二段式**，均只 add 不 commit ⇒ 与业务同事务。

### D-7 Worker：独立进程轮询 PG 表（不引入队列库）

照 `agent_orchestration_worker.py`：独立进程、**不得随 API 启动**、feature flag 关则 `run()` 直接 return、
SIGTERM 优雅停机、条件 UPDATE claim。
**不引入** celery/arq/dramatiq/APScheduler（仓内零队列依赖 ⇒ 引入即新运维面）。
**不复用** 请求内 fire-and-forget（`kpi_vector_graph_sync.py:614-615`）⇒ 那是尽力而为，P5 要可靠。

### D-8 外部写：先接口 + 模拟器（Protocol + 契约测试）

照 `research_agent_ports.py` 的 Protocol + 默认实现 + 契约测试三件套。
**Agent 仍只读**；写操作一律经 Command（Command 是唯一写入口）。
真实 ERP/SRM 适配后置（仓库内无任何写接口证据：`config.py:117` 只提到 ERP 名字）。

### D-9 部署：compose v1 片段追加 + 复用现有镜像

- 片段**追加**到 `/opt/qa-system/docker/docker-compose.centos7.yml`（v1 1.29.2 兼容写法）；
- **禁** `!override` / `profiles` / `docker compose` 子命令（v1 直接炸）；
- worker **复用现有 backend 镜像**（照 `docker-compose.yml:195-198` 不写 `image:` 键）⇒ **不新增中间件、不新增镜像**；
- 首次部署前 `docker-compose -p qa-system --env-file /opt/qa-system/docker/.env -f /opt/qa-system/docker/docker-compose.centos7.yml config --quiet` 通过；
- 离线镜像 `docker load` + `docker tag` + 回滚锚点 `docker tag …:pre-<批次>`；
- **不改** `/opt/qa-system/docker/.env`（`SECRET_KEY` = Fernet 主密钥）；
- 有 DDL ⇒ **先 `pg_dump`** 备份受影响对象。

### D-10 迁移与代码同批上线

`数据库环境使用规范.md:74-77`：DDL 与依赖它的代码不得分开部署 ⇒ 本批**开关默认关**，
新迁移与新代码**同一个镜像**上线；启动期 alembic head vs DB 三级漂移校验（`工程结构.md:42-72`）
**版本滞后是 blocking RuntimeError** ⇒ 迁移必须先在测试库演练，并在生产按 runbook 执行。

### D-11 不新增依赖、不新增中间件、不改既有镜像版本

`跨板块扩展_1109.md:195`「部署零新增中间件：只复用现有 compose 服务与 Worker，镜像版本不动」。

### D-12 集成用例并入既有文件

CI 集成分片按「排序下标 mod 4」轮转归属 ⇒ **新建 integration 文件会假 net-new**。
⇒ 新用例**并入既有集成文件**；新文件只放 `unit/`（路径哈希分片，不受影响）。

---

## 5. 前置依赖（开工前必须先落地的三件事）

| # | 前置 | 为什么 | 怎么做 |
|---|---|---|---|
| 1 | **迁移链上生产**（0144–0148） | 生产现 `0143`、仓内 head `0148`；P5 的迁移必须接在**已验证**的链尾，否则一次上线要连跑 7 个未验证迁移 | 先发一个「迁移链收口」批次（只含既有 0144–0148 的代码 + 迁移），演练 + pg_dump + 生产验证 ⇒ P5 从 0149 起 |
| 2 | **部署线空闲** | 1114 发版线 + 1116 热修线未收口（PR #143 未合） | 两条线收口后 P5 才排 D48 的部署段 |
| 3 | **迁移号串行分配** | P5（≈3 个）与 WP-ONT-AI A2/A3（各 1–2 个）都要 0149+ ⇒ 并行开发会**分叉 alembic 链** | 约定：**先合并者占号**，另一条线 rebase 后改号；或预先分配区间（P5: 0149–0151 / WP: 0152+） |

---

## 6. 门禁与回归口径

- **CI 判据**：失败集合对差**净新增 0**（不是全绿）；仓内有 28 条红基线 ⇒ 不要追求全绿。
- **本机全量 unit 不作门禁**（35 分钟到 83% 且阻塞在 Milvus/PG I/O）⇒ 走 CI 分片 + 定向回归。
- **定向回归的盲区**：跨模块结构守卫（照 P4 教训：SQL 路直接调渲染器被 CI 抓出，
  定向知识回归看不见）⇒ 守卫类用例必须**同时**纳入 CI 分片覆盖。
- **真实 DB**：状态机 / 并发 / 审批用例走真实 PostgreSQL（`qa_metadata_test*`）+ 完整 API 链路。

---

## 7. 风险与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| Workflow 写操作安全面 | **高** | 独立立项 + 独立开关（默认关）+ 独立回滚 + 安全评审 ×2 + 模拟器先行 + 幂等 + 审计 + 补偿 |
| 审批面写死 ⇒ A4 返工 | 高 | D-1 预留 subject 型引用；A4 只保留业务审核记录 |
| 迁移一次上 7 个未验证迁移 | 高 | 前置 1：先做「迁移链收口」批次 |
| compose v1 写法踩 `!override`/`profiles` | 中 | D-9 硬约束 + `config --quiet` 校验 + 复刻既有 v1 兼容片段 |
| 并发丢步（identity map 不刷新） | 中 | D-3 `populate_existing=True` + 并发用例钉住 |
| Worker 与 API 同进程启动 | 中 | D-7 独立进程；部署片段单列 service |
| 仓内既有红基线掩盖新失败 | 中 | 净新增 0 判据 + 失败集合逐片对差 |

---

## 8. 与 WP-ONT-AI 的排序裁决（为什么 P5 先做）

**结论：P5 先，WP-ONT-AI A2/A3/A4 后（A2 → A3 → A4 内部顺序不变）。**

| # | 理由 |
|---|---|
| 1 | **依赖就绪度**：P5 规格完备、无外部业务依赖、仓内 5 套骨架可复用 ⇒ 今天就能开工；WP-ONT-AI 剩余三个批次**各自**卡在一份业务答复（Q3 采样白名单 / Q4 审核分派 / Q5 模型与预算）⇒ 开工时间不可控 |
| 2 | **不造第二套审批栈**：A4 的「四眼审核」与 P5 的审批面同构 ⇒ 先 P5，A4 复用（前提是 D-1 的开放模型）；反序则 `ontology_review` 变死重，A4 完成后 P5 还要回头迁移 |
| 3 | **战略缺口**：设计目标文档的成熟度阶梯里 L3「智能执行」（Workflow / 审批 / ERP-SRM / 自动任务）目前是 0；P5 是 1109 契约 D1–D62 的**最后一个**未完成阶段 ⇒ 先做完 ⇒ **G1–G7 全绿、契约闭合** |
| 4 | **部署窗口现实**：P5 的尾部（D48 部署）需要生产部署线空闲 + 迁移链先上生产 ⇒ 这段时间正好用来征集 Q3/Q4/Q5 业务答复与写 A2/A3/A4 任务卡 |

**并行三件事（不等 P5 完成）**：① 业务答复征集（Q3/Q4/Q5 清单见 WP-ONT-AI 计划 §A2/A3/A4「阻塞解除清单」）
② 迁移链收口 + 部署线收口（§5 前置 1/2）③ 迁移号串行分配（§5 前置 3）。

**若业务答复提前回来**：A2 可先做「不依赖采样白名单的部分」（表 + 剖析服务 + 脱敏，采样默认关闭），
但**不得**在 Q3 未回前把采样打开；A3 的「按用途选模型路由 + Prompt 版本落库」是**代码缺口**（Q5 里明确的三项缺口中两项）⇒ 可与 P5 并行做。

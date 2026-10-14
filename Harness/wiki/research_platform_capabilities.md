# 研究平台级能力（环节二）

**日期**：2026-10-13 · **变更**：[feat-research-platform](../changes/feat-research-platform/summary.md)
· **计划**：`docs/superpowers/plans/2026-10-12-research-session-evidence-and-platform.md`（环节二，Task 10–16）
· **前置**：[[research_evidence_contract]]（环节一，任务级证据契约）

环节一给了研究**证据契约**（版本化、可恢复、读数可信）。环节二在其上补**平台级能力**：
SSE **断线可回放**、进程崩溃**自愈**、**mode 差异化**规划、证据面板**结构化呈现**、会话**成本闸门**。
本页是这层的 **SSOT 摘要**；实现细节以代码与 `changes/` 为准。

## 为什么需要这层能力

改前的四类**平台级**缺陷（全部有对应回归测试，且多为**跑起来才看得见**的类型）：

1. **断线即丢**：SSE 连接一断，漏掉的事件永远看不到（本项目反复踩的 SSE 静默失败类）。
2. **崩溃即死路**：进程重启时正跑的任务成僵尸，会话 `active_run_id` 永不释放 ⇒ 每次提交 409、只能删会话
   （见 [[qa-system-research-claim-invariant-create-only]]）。
3. **mode 无差别**：三种研究方式跑出来一个样；且**指引若与执行面冲突会让计划变差**。
4. **无成本上限**：一条会话可无限花钱。

## 事件台账与断线回放（Task 12）

改法：**先落库、再广播**。

- 事件写 `research_event_outbox`（`BIGSERIAL seq` **单调游标**），再 `bus.publish(..., seq)`；
  帧带 `id: <seq>`，客户端据此记游标、重连带 `?lastEventId=` 回放错过的事件。
- **游标选 `seq` 而非 `created_at`**：`created_at` 同刻并列会**漏/重**；单调整数无歧义（这是计划里的 bug）。
- **写用独立短会话**：避免状态机 rollback 把**已广播**的事件从台账里抹掉 ⇒ 事件活过 rollback。
- **`payload` 先过 `research_json` 再落库**：回放的 `Decimal` 仍是 **str**，与实时帧同型
  （不踩「两套 JSON 安全映射」坑，见 [[qa-system-two-json-safe-mappings]]）。
- **回放收尾口径**：对回放中**任意**终态帧就 `return` 会**漏掉当前轮**（多轮会话里，旧游标会先撞上
  **上一轮**的 `done`）⇒ 改为「按**最后一帧** + 会话终态收尾并**翻页**」（T12 fix1）。这正是本特性要
  解决的断线场景本身。

## 任务台账与启动自愈（Task 10+11）

形态裁定（用户 AskUserQuestion）：**进程内持久队列**（**不**新增独立 worker 进程、**不**改 compose）。

- `research_task` 台账：提交先落队，执行体**原子条件 UPDATE** 认领（无 TOCTOU），执行后回写终态。
- 启动期 **reaper**（`lifespan` 内）把租约超期的 `leased` 孤儿任务翻 `orphaned`，并**释放会话
  `active_run_id`** ⇒ 堵「崩溃后 claim 永久占用 ⇒ 每次提交 409」的洞。**不**自动重跑状态机（防双执行）。
- **为何不用独立 worker**：独立进程的帧进不了 web 进程的**进程内** `bus`（内存 asyncio.Queue）⇒ 前端只剩
  connected + 心跳、UI 永久转圈；且测试 app 无 lifespan ⇒ 大量 research HTTP 用例会集体变「提交了没人跑」。
- **边界（遗留）**：reaper **只**回收 `leased`。`pending`（崩溃窗口内尚未认领）与「启动时 `awaiting_user`
  且持权」是**合法态**（checkpoint 续跑依赖它），不回收 —— naive 修法会杀掉跨重启的合法暂停会话 ⇒ 需人类裁定
  （[[qa-system-research-task-reaper-scope]]）。

## mode 差异化规划（Task 13）

规划阶段按 `mode` 注入研究指引（`planQuestionForMode`；未命中 **逐字返回**、不抛）。

- 注入点复用**既有** `planQuestionWithFeedback` 的**问题文本** —— 计划里的「planner prompt 后缀形参」虚构
  （`_plan_by_llm(question, client, model_name)` 无 suffix；planner prompt 是固定常量）。
- 回落到 `singleStepPlan(question)` 时仍用**未注入**的 `question` ⇒ 拆步指令不泄进 NL2SQL。
- **指引文本必须与执行面同向**（T13 评审 M1）：planner 系统提示要求「只拆**彼此独立**、一条 SQL 算不完的子问题」，
  且执行面**无步间数据依赖**（每步 SQL 只由本步 `sub_question` 生成，见 [[qa-system-multistep-no-data-dependency]]）。
  ⇒ 指引一律去掉「逐层拆解 / 对称取数 / 逐层下钻」等**顺序依赖**措辞，改为「覆盖哪些**互相独立**的口径/分面」；
  `compare` 甚至显式写「若一条 SQL 即可算出全部对比，就不要拆分」。否则**本特性会让计划变差**。

## 证据面板结构化呈现（Task 14）

前端 `EvidencePanel` 此前对 `summary` 只渲染标量，**静默丢弃** `topN` / `timeRange`（`typeof value !== "object"`）。
改后：纯函数 `topNTableShape` / `timeRangeLabels`（**不改入参**，构造新对象）如实呈现：

- **`topN`** = `_topNRows` 在**全量**行上按度量**全排序**取前 N 并打 `rank` 1..N ⇒ 是**确定性排名**，
  **不是采样**（真采样是 `sample_rows = rows[:maxSampleRows]`）。文案不得说「采样」（T16 纠正）。
- **`timeRange`** = 时间轴列的区间标签。
- **未做图表**：报告 evidence 顶层仍恰 7 字段（无度量/标签列身份）⇒ 后端契约扩展（`metricColumn`/`labelColumn`
  + 行数）是**登记后续项**。

## 会话级预算闸门（Task 15）

**只读**闸门：`_runFrom` 在**每个相位边界**读会话累计成本，`cost >= SESSION_BUDGET` ⇒ 抛
`SessionBudgetExceeded` ⇒ `research.error{code=turn_failed}`（消息带真实成本/预算）+ 会话 `failed`。

- **绝不换/选模型**：`selectModelForPhase`（按阶段改派模型）**违反**记忆 [[qa-system-no-model-override]]
  ⇒ 计划该部分**删除**，只保留只读闸门。
- **`<= 0` ⇒ 关闭闸门**：**有意**与路由器 `selectModel` 的 `>=`（`budget=0` 视为必然超预算 ⇒ 降级最便宜模型）
  **相反** ⇒ 同一配置值两处含义不同，**如实登记为分歧**（路由器侧改动需人类裁定）。
- **软上限、非硬顶**：只在**相位边界**检查 ⇒ 单相位内（尤其 `execute` 多步）可超预算一段才被拦下 ——
  **有意**设计（不半途打断一条相位、避免半截状态）。
- **静默失效（fail-open）**：`_readSessionCost` 读失败 best-effort 放行；**持续**失败 ⇒ 该会话预算静默失效、
  无用户可见信号（已知限制，非本批修复）。

## 部署形态

**进程内**执行（无独立 worker / 无 compose 改动）。`qa-backend` 是**烘焙镜像** ⇒ 部署新代码必须
`docker compose build backend`（对照 [[qa-system-frontend-deploy-build-required]]）。

## 迁移

| 文件 | 内容 |
|---|---|
| `alembic/versions/0120_research_event_outbox.py` | `research_event_outbox` 建表 |
| `alembic/versions/0121_research_task.py` | `research_task` 建表 |

链：`0117 → b1ea6cb32588_run_state → 0118 → 0119 → 0120 → 0121`；生产库 head `0121`。

## 相关条目

- [[research_evidence_contract]]（环节一：任务级证据契约）
- [[data-model]]（表结构与字段）
- [[api-reference]]（`/api/v1/research/*`，含 `?lastEventId=`）
- [[architecture]]（进程内 BackgroundTasks 与状态机分层）
- [[chat_multi_step_persistence]]（同源的「续跑」机制，在 chat 侧）

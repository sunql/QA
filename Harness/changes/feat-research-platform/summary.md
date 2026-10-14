# 变更：feat-research-platform

- **日期**：2026-10-13
- **作者**：Claude / 启琳
- **Phase**：**环节二** 研究平台级增强（Task 10–16）
- **状态**：in-review（13 个提交在分支 `feat/multi-step-persist`，HEAD `09173fd35b6322df705f278d8cfd8a604416272`；**未合并 main**）
- **关联变更**：
  - predecessor：环节一 [feat-research-evidence-contract](../feat-research-evidence-contract/summary.md)（Task 1–9，已合入本分支 `0eef8f4`）
  - 邻近变更（同分支、不同域）：`feat-multi-step-persist`（chat 侧多步落库/续跑）
- **迁移版本**：`0120_research_event_outbox`（29 字符）, `0121_research_task`（21 字符）（均 ≤ 32）
- **MEMORY**：[qa-system-research-task-reaper-scope.md](../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-research-task-reaper-scope.md)

> **交付边界**：本变更 = 计划 `docs/superpowers/plans/2026-10-12-research-session-evidence-and-platform.md`
> 的**环节二全部 7 个任务**（T10–T16）。计划是**建议**不是契约 —— 经 recon 证明 T10/T11/T13/T14/T15
> 的计划伪代码**系统性虚构或违反硬约束**，均按真实代码重塑；逐条证伪记录见
> `.superpowers/sdd/2026-10-12-research-session-evidence-and-platform/`（brief + progress ledger）。
> **已登记的遗留项见 §7/§9，记录不代表已修。**

---

## 1. 需求

**背景**：研究会话（`/research/*`）在环节一有了「可恢复、可复现、可引用」的**证据契约**后，仍有三类
**平台级**缺口（用户在真实使用中会遇到）：

| # | 缺陷（用户可见现象） | 验收标准（可测量） |
|---|---|---|
| 1 | **断线/刷新即丢** —— SSE 连接一断，漏掉的事件永远看不到（只看到一条静默死流） | 事件**先落库再广播**；重连带 `?lastEventId=` 能**重放出错过的事件**（含终态帧），帧带 `id:` 游标 |
| 2 | **崩溃后会话被永久占用** —— 进程重启时正跑的任务成僵尸，会话 `active_run_id` 永不释放 ⇒ 每次提交都 409、只能删会话 | 任务落**台账**（`research_task`）+ 启动期 **reaper** 把租约超期的孤儿任务翻 `orphaned` 并**释放会话执行权** ⇒ 会话可再用 |
| 3 | **模式无差别** —— research / attribution / compare 三种研究方式跑出来的计划与报告一个样 | 规划阶段按 mode 注入**彼此独立**的研究指引；报告章节已按 mode 差异化（环节一已有） |
| 4 | **证据面板丢结构化信息** —— `topN` / `timeRange` 被前端静默丢弃，只显示标量 | 面板如实呈现 `topN`（按度量排名）与 `timeRange`；文案不得把「确定性排名」说成「采样」 |
| 5 | **会话无成本上限** —— 一条会话可无限花钱 | 相位边界的**只读**预算闸门：累计成本 ≥ `SESSION_BUDGET` ⇒ 终止该轮（`research.error{turn_failed}`） |

**做完是什么样（用户视角）**：研究跑到一半断线，重连能看到错过的每一步；后端崩了重启，卡住的会话能继续用；
不同研究方式给不同的规划指引；证据面板显示排好名的 Top-N 与时间范围；超预算的会话会被停下来。

**来源**：环节一交付后，用户在「B（环节一）已完成」的基础上要求「按实际情况开展剩余（环节二）」。
环节二形态经用户 AskUserQuestion 裁定：T10+11 走**「进程内持久队列（推荐）」**（保内联执行 + 内存
SSE 总线，加 `research_task` 台账 + 启动 reaper，**不**新增独立 worker 进程、**不**改 compose）。

## 2. 设计评审

### 2.1 关键取舍（每个都曾有 ≥2 个候选，且多数是**证伪计划伪代码**后的重塑）

| 议题 | 候选 A | 候选 B（**采纳**） | 定案理由 |
|---|---|---|---|
| **T10「ResearchRun 版本化快照」** | 照计划**新建** `research_run_snapshot` 表 + 服务 | **不做**（表/服务/消费方全无，与环节一 `research_run_state` 职责重叠） | 计划自述用途依赖 T11 ⇒ 单独做是 YAGNI；唯一真缺口（崩溃时不知跑到哪个相位）改用**相位游标落 T11 的任务行**。计划里那张表**根本不存在**（T16 实现者 `grep` 证实） |
| **T11 执行形态** | 照计划起**独立 worker 进程** | **进程内持久台账**（`research_task` + 原子认领 + 启动 reaper） | 独立 worker 的帧进不了 web 进程的**进程内** `bus`（内存 asyncio.Queue）⇒ 前端只剩 connected + 心跳、UI 永久转圈；且测试 app 无 lifespan ⇒ 88 个 research HTTP 用例会集体变「提交了但没人跑」。⇒ 保留内联执行，只补**持久化 + 自愈** |
| **事件游标** | 计划用 `created_at` 作 SSE 游标 | **`BIGSERIAL seq` 单调游标** | `created_at` 同刻并列会**漏/重**事件；单调整数游标无歧义。计划写法有 bug |
| **T15 阶段模型能力** | 照计划 `selectModelForPhase`（按阶段**改派模型**） | **删除该构想**，只做**只读**会话级预算闸门 | `selectModelForPhase` **违反硬约束** memory `qa-system-no-model-override`（禁止后端覆盖用户 modelId），而计划自身第 24 行就声明了这条约束 ⇒ 必删。计划里的 `state["budget"]` / `DEFAULT_BUDGET` / `phaseBudgets` 全虚构 |
| **T13 指引注入点** | 计划「给 planner prompt 加后缀形参」 | 复用**既有** `planQuestionWithFeedback` 的问题文本注入 | 计划的「prompt 后缀形参」虚构 —— `_plan_by_llm(question, client, model_name)` **无** suffix；planner prompt 是固定常量 |
| **T14 图表与证据回放** | 计划「修 `chartType` 硬编码 bug + 渲染 LineChart/BarChart」 | **只补证据面板的结构化呈现**（`topN` / `timeRange`） | 计划 Step 1 修的是**不存在的 bug**（`chartType` 早已由 ChartService 决定）；仓内**无** `LineChart/BarChart`，只有通用 `EChart.tsx`。真缺口 = 面板丢弃结构化摘要 |
| **T13 指引文本内容**（评审 M1 后） | 保留「沿维度**逐层拆解** / 对称取数 / 逐层下钻」 | 改写为「覆盖哪些**彼此独立**的口径/分面」 | 三段原文与 planner 系统提示（`只拆彼此独立、一条 SQL 算不完的子问题`）+ 执行面（**无步间数据依赖**，见 memory `qa-system-multistep-no-data-dependency`）**相冲突** ⇒ 本特性可能让计划**变差**。裁定**做修复**而非仅登记 |

### 2.2 多视角意见（SDD 三关）

每个任务都走「独立实现 → orchestrator **独立验证**（自跑回归 + 自放**反向守卫** + 字节级还原）→
独立只读**评审**」。评审结论：

- T10+11：`APPROVE-WITH-FOLLOWUPS`（0C / 2 Important **均为覆盖缺口非缺陷** / 2 Minor）；评审**独立复现**了
  controller 的两条反向守卫（均红），并确认迁移与 ORM **逐列/逐索引一致**、单 head、文件名 ≤32。
- T12：双评审发现 **D1（数据丢失）** —— outbox 按会话累计、回放对**任意**终态帧 `return` ⇒ 多轮会话里
  带旧游标重连会撞**上一轮**的 `done` 立刻收尾、当前轮事件一条都收不到（**正是本特性要解决的断线场景本身**）
  ⇒ fix1 改为「按最后一帧 + 会话终态收尾并翻页」。
- T13：评审 M1（指引与系统提示/执行面冲突，见上）、L1（`originalQuestion` 落进持久化 checkpoint options）、
  L2（丢基数守卫）⇒ 一轮 bounded fix。**明令不得碰 `prompts_zh.py`**（该常量被 research 与 chat 共享）。
- T14：`APPROVE`（0/0/0 + 3 LOW）。
- T15：`APPROVE`（0/0/2/2）；SSOT 反证成立 —— 闸门**不触**模型路由（`router.calls == []` 有正向对照）。
- T16：`APPROVE`（0/0/0 + 2 LOW）；实现者抓到 brief 的**两处错**（不存在的 `research_run_snapshot` 表；
  J2「首边界即终止」会**假绿**）。

### 2.3 定案

按上述 B 列落地；所有评审结论一律 APPROVE 或 APPROVE-WITH-FOLLOWUPS，**零 must-fix 遗留**（LOW 当场修或登记）。

## 3. 数据模型变更

新增 **2 张表**，见 [data-model.md §环节二](../../wiki/data-model.md#phase-环节二-增量研究平台级表)。无修改既有列。

| 迁移文件（**≤32 字符**已校） | 字符数 | 内容 |
|---|---|---|
| `0120_research_event_outbox.py` | 29 | `research_event_outbox` 建表（`BIGSERIAL seq` PK + `session_id` 索引） |
| `0121_research_task.py` | 21 | `research_task` 建表（任务台账 + `lease_until` 索引） |

- **`research_event_outbox`**：`seq` BIGSERIAL PK、`session_id` UUID、`event` VARCHAR(50)、
  `payload` JSONB NOT NULL DEFAULT '{}'、`created_at`。索引 `ix_research_event_outbox_session_id`。
  **刻意无 `run_state_id`**（emit 绑定早于 run_state 创建，回放按会话）、**无 UUID event_id**（冗余）。
  `payload` **先过 `research_json` 再落库** ⇒ 回放的 Decimal 仍是 **str**，与实时帧同型（不踩「两套 JSON 安全映射」坑）。
- **`research_task`**：`id` UUID PK、`session_id`/`turn_id` FK(CASCADE)、`kind`、`status`、`attempt`、
  `max_attempts`、`worker_id`、`lease_until`、`payload` JSONB、`error`、时间戳。索引 `session_id` / `turn_id` / `lease_until`。
- **无 CheckConstraint**；无数据回填。
- **生产库 head 实测 `0121`**（见 §8；环节一后曾停在 `0119`，本次应用 `0120`+`0121`）。
- 迁移链（**注意不是线性的 0117→0118**）：`0117 → b1ea6cb32588_run_state → 0118 → 0119 → 0120 → 0121`
  —— `0118` 的 `down_revision` 是 hash 命名的 `b1ea6cb32588`（T16 抓到的 doc 事实错误）。

## 4. 接口契约变更

| 方法 + 路径 | 变更 |
|---|---|
| `GET /api/v1/research/stream` | 新增查询参数 **`?lastEventId=<seq>`**（可选）：带它时**先回放**该 seq **之后**的事件再续订阅。每帧新增 **`id: <seq>`**（客户端据此记游标）。回放对**任意终态帧**收尾会漏当前轮 ⇒ 改为「按**最后一帧** + 会话终态收尾并翻页」（T12 fix1） |
| 事件落库口径 | 所有广播改走 **`_persistAndPublish`**：**先**用**独立短会话**写 `research_event_outbox`，**再** `bus.publish(..., seq)` ⇒ 事件**活过状态机 rollback**。自记账失败不抛（不杀研究）。**不新增 error code** |
| 预算闸门 | `SESSION_BUDGET > 0` 且在**相位边界**累计成本 ≥ 预算 ⇒ 抛 `SessionBudgetExceeded` ⇒ 发 `research.error{code=turn_failed}`（消息带**真实成本/预算**）+ 会话落 `failed`（单条 UPDATE 同时**释放** `active_run_id`）。`SESSION_BUDGET <= 0` ⇒ **关闭**闸门 |
| 规划指引 | 规划阶段按 `mode` 注入研究指引（`planQuestionForMode`）；未命中 mode **逐字返回**、不抛 |
| 报告 / 面板 | 报告 `payload.evidence` 契约**未变**（仍顶层 7 字段）；前端 `EvidencePanel` **新增**如实呈现 `topN`（含合成 `rank` 列）与 `timeRange` |

**状态机变化**：`active_run_id` 的**释放**多了一条路径 —— 除会话终态外，**启动 reaper** 回收 `leased` 孤儿任务时
一并释放。**已知边界（遗留）**：reaper **只**回收 `leased`；`pending`（崩溃窗口内尚未认领）与「启动时
`awaiting_user` 且持权」是**合法态**，不在本批范围（见 §7 遗留 F1，需人类裁定）。

## 5. 实现要点

**新增模块**（均小函数拆分，单函数 < 50 行）：

| 文件 | 行数 | 职责 |
|---|---|---|
| `app/services/research_event_outbox.py` | 91 | `persistEvent` / `fetchEventsAfter`（单调 `seq` 游标读写） |
| `app/services/research_task_service.py` | 219 | 台账：`add_task` / `claimTask` / `finishTask` / `reapOrphanedTasks`（原子条件 UPDATE 认领） |
| `frontend/src/components/research/evidenceSummary.ts` | 63 | 纯函数 `topNTableShape` / `timeRangeLabels`（**不改入参**，构造新对象） |

**改动模块**：`api/v1/research.py`（`_persistAndPublish` / `_fetchMissed` 翻页 / `_eventFrames` / `_encodeFrameData`
序列化守卫 / `streamSessionEvents` 的 `lastEventId`）、`services/research_agent_service.py`（预算闸门
`_sessionBudgetExceeded` / `_runFrom` 循环顶的闸门 / `markFailed`→`failSession`）、`services/research_agent_phases.py`
（mode 指引接线）、`main.py`（lifespan 接线 reaper）、`domain/research_models.py`（2 个模型）、
`services/research_session_service.py`（`failSession` 单条 UPDATE）、`research_event_bus.py`（`Event` 拓宽为三元组带 `seq`）、
`research_evidence_summary.py`（`_topNRows` 确定性全排序语义确认）。

**关键算法/接缝**：
- **先落库再广播**：`_persistAndPublish` 用**独立短会话**写 outbox（避免状态机 rollback 把已广播事件从台账抹掉），
  再广播带 `seq` 的帧。
- **回放翻页**：`_fetchMissed` 按 `seq > lastEventId` **分页**取全部事件，物化为纯元组（脱离 ORM 会话）。
- **预算闸门只读**：`_sessionBudgetExceeded` 只读 `session_token_usage` 的既有列，**绝不**换/选模型；
  `budget <= 0` ⇒ 闸门 OFF（**有意**与路由器的 `>=` 读 0 语义相反，见 §7 遗留）。
- **reaper**：`reapOrphanedTasks` 在 `lifespan` 内跑一次 `UPDATE … WHERE status='leased'` ⇒ `orphaned`
  + 释放对应会话 `active_run_id`（**不**自动重跑状态机，防双执行）。

## 6. 测试

TDD（先 RED 再 GREEN），真实 PostgreSQL（5434 `qa_metadata_test`）+ 完整 API 链路；**无 sqlite 内存库、无直调 service 替代 HTTP**。

| 测试文件 | 覆盖 |
|---|---|
| `integration/research/test_event_outbox.py`（578） | outbox 落库 / `?lastEventId=` 回放 / 翻页 / Decimal 回放仍 str（Task 12，12 条） |
| `integration/research/test_research_task.py`（479） | 台账认领 / 释放 / `test_reaper_frees_crashed_session_end_to_end` / **`test_reaper_does_not_reap_pending_task`**（边界）（Task 10+11） |
| `integration/research/test_session_budget_gate.py`（280） | 超预算终止 + **不触路由器** + `<=0` 关闸门（Task 15） |
| `integration/research/test_plan_mode_guidance.py`（370） | 三 mode 指引互异 / 键集匹配 / 不泄进子问题（Task 13） |
| `integration/research/test_platform_enhancements_e2e.py`（397） | **T16 跨特性交汇点** J1（T15×T12）/ J2（T15×释放不变式） |
| `unit/services/test_research_event_outbox.py`（134）/ `test_research_session_budget.py`（148）/ `test_research_plan_mode_guidance.py`（88） | 单测 |
| `unit/test_main_lifespan_wires_reaper.py`（58） | reaper **接线**守卫（tokenize 骨架，防「import 在但调用删了」） |
| `frontend/src/tests/evidenceSummary.test.ts`（75）/ `EvidencePanel.test.tsx` / `ResearchSessionPage.test.tsx`（293）/ `researchStore.test.ts`（209） | 前端 |

**实测计数（HEAD `09173fd`）**：
- `integration/research/`：**79 passed**（T15 后基线 77 + T16 两条）
- `unit/services`（研究相关）：**225 passed**（T15 后）
- 前端 `EvidencePanel.test.tsx + menuI18n.test.ts`：**77 passed**

**反向守卫（证明用例真咬，均实测 RED + 逐字节还原）**：
- **T12** J1 类：`emit=partial(_persistAndPublish, sessionId)` → 直连 `bus.publish` ⇒ 实时终态帧 `id` 变 `None` ⇒ 红。
- **T10+11** reaper 接线最难形态：`main.py` **保留 import、只删调用** ⇒ 接线守卫红；reaper `WHERE status==LEASED`
  → `in_([LEASED, PENDING])` ⇒ 边界用例红。
- **T15**：`if budget <= 0` → `< 0` ⇒ `test_budget_zero_or_negative_disables_the_gate[0.0]` 红；
  删 `_runFrom` 两条闸门行 ⇒ 2 红。
- **T16**：J1（emit 不落 outbox ⇒ 帧 `id` 为 `None` 红）、J2（`markFailed` 不释放 ⇒ 后置 `202` 变 `409` 红）。

## 7. 安全审查

**触发**：是（改动认证路由的**查询面** —— SSE 回放；新增表读写；序列化边界；启动期 DB 写）。

**结果**：`security-reviewer`（只读，钉 `09173fdb`）⇒ **APPROVE-WITH-FOLLOWUPS**，
**0 CRITICAL / 0 HIGH / 2 MEDIUM / 2 LOW**。

| 级别 | 位置 | 处置 |
|---|---|---|
| MEDIUM | `research_event_outbox` **无 TTL、无会话删除级联**（`0120_research_event_outbox.py:52-57` 只建 `session_id` 索引**无 FK**；模型 docstring 自认「删会话留孤儿行属后续」；全仓 grep `outbox` + `delete/ttl/purge/retention` **零命中**）。失败场景：① 每条 research.* 事件落一行、**单调只增永不回收** ⇒ 存储/成本无界；② 删会话后其事件载荷（可能含业务查询结果）**仍永久留存**，与 `research_task` 的 `ondelete=CASCADE` 语义不一致 | **登记遗留**（非本批修复）。**可达性边界**：孤儿行经 API **不可达**（回放先过 `_ownedSession` ⇒ 会话已删 ⇒ 404）⇒ 是**留存/静态机密性**问题，**不是**访问控制洞。建议补 retention job 或给 `session_id` 补 `ondelete=CASCADE` |
| MEDIUM | 启动 reaper 用 `WHERE status='leased'`（**不按 `lease_until` gate**）标**全部** `leased` 为 `orphaned` 并 `failSessionIfActive`（`research_task_service.py:198-219` + `main.py:236-252`）⇒ **multi-worker / 多副本**下副本 A 启动会**误杀副本 B 正在跑**的任务、把 B 的活动会话打成 `failed` | **登记遗留（潜伏）**：**当前单进程部署不可达**（`Dockerfile.backend` 单 worker，模块 docstring 已登记该限制）。仅多副本下升级为 HIGH。与已登记「多副本 reaper」项同源 |
| LOW | SSE 端点（含回放）**无限流**（`research.py:656-680`）；单次回放**有界**（`_REPLAY_MAX_ROWS = 10000`，到顶停翻页 + WARNING） | 登记；与既有 SSE 端点同类。认证用户可反复请求「回放 10000 行 JSONB」放大 DB/内存 ⇒ 建议限流 |
| LOW | 序列化降级帧的数值类型（`Decimal`→**float**，`_encodeFrameData` 降级到 `_coerceJsonSafe`） | **设计内 fail-soft**，仅 NOTE：降级只发生在**永不落库**的事件上 ⇒ 无可观测的「实时 float / 回放 str」漂移 |

**独立复核（非取信于报告）**：**越权/IDOR** —— `streamSessionEvents`（`api/v1/research.py:657`）带
`Depends(_requireResearchUser)`，回放前 `:675` 先 `_ownedSession`（`:1102-1109`，非属主 **404** 不泄露存在性）；
`lastEventId` 仅**整数游标**，回放查询恒带 `session_id == <已校验会话>` ⇒ **无法跨会话拉他人事件**，
无游标 ⇒ 不回放全量（既有 `test_stream_404_for_other_user_session` 仍生效，**无新增 IDOR**）。
**注入** —— 全部 SQLAlchemy Core `select/update` + **绑定参数**；对改动文件 grep `text(` / f-string SQL /
`literal_column` **零命中**。**竞态** —— 认领原子条件 UPDATE（`rowcount==1`，挡 TOCTOU）+ 认领后**立即 commit**
（后续 rollback 不撤租赁）+ 收尾 `WHERE status='leased'`（不覆盖终态）+ `finishTask(failed)` 与 `markFailed`
挂**同一次 commit** ⇒ 单进程无「终态被 reaper 误伤」窗口。**密钥/日志** —— 无硬编码密钥
（`encryptApiKey("test-password")` 仅在测试夹具）；新增日志只记 id / 计数 / 事件名，**不记** payload。
**「两套 JSON 安全映射」漂移核实** —— 同一条事件的落库与实时帧路径**都**调 `research_json(同一 payload)` ⇒
要么都用 `Decimal→str`（逐字节一致），要么**两条路径同时失败**（该事件不落库、无回放帧）⇒ 同事件类型漂移**不可能**。

> **评审过程注记**：评审结束时报树变 ` M Harness/wiki/data-model.md` —— 是**我在评审期间并发落的
> §环节二 表结构文档**（同一「评审读移动树」情形，见环节一 §7 的先例），**非缺陷**；评审**未回退、未触碰**它。

**已知限制 / 需人类裁定**（来自 **Task 15 代码评审**，非本次安全评审；登记，非本批修复项）：
- **`SESSION_BUDGET` 双语义分歧**：本闸门 `<=0 ⇒ 关`；而路由器 `ModelRouterService.selectModel`
  （`model_router_service.py:71`）用 `>=` ⇒ `budget=0` 时 `0 >= 0` 恒真 ⇒ **每个自动路由会话被降级到最便宜模型**。
  **同一配置值两处含义相反**。属**路由器**改动且贴近「模型选择」硬约束 ⇒ **需人类裁定**。
- **预算闸门静默失效（观察性）**：`_readSessionCost` best-effort 失败 ⇒ 放行；若 `getSessionCost`
  **持续**失败，闸门对所有轮次**静默失效**且无用户可见信号 ⇒ 建议对该 WARNING 加指标/告警。

## 8. 部署验证

**冒烟命令与真实输出**：`docker/docker-compose.yml` 全栈。**本次重建并重部署 backend + frontend**
（旧 `qa-backend` 是 4 小时前的**烘焙镜像**，不含本分支 T12–T16 代码 ⇒ 不重建则冒烟测不到真实代码）。
产物 [`deploy-smoke.txt`](deploy-smoke.txt)（钉 `git HEAD 09173fd`，tracked 树干净）：

```
--- 1) 容器状态 ---    qa-backend Up 35s  qa-system-backend / qa-frontend Up 3s  qa-system-frontend
--- 2) 后端 openapi --- HTTP 200  http://localhost:8000/openapi.json
--- 3) research 路由 --- 8 条（sessions / turns / abort / report / reports / stream / checkpoints/answer）
--- 3b) /research/stream 参数 --- sessionId(必) + lastEventId(选) + 认证头   ← T12 契约在线
--- 4) 前端 --- HTTP 200  http://localhost:5173/
--- 5) 生产库 alembic head --- 0121（9 张 research_* 表，含 research_event_outbox / research_task）
--- 6) 新表结构 --- seq BIGSERIAL PK / 见 deploy-smoke.txt
--- 7) 启动 --- drift blocking=0；Application startup complete；**reaper 实测触发**（UPDATE … 'orphaned' WHERE status='leased'）
```

**真实数据验证**：环节二**未**另写 `scripts/<feature>_realdata.py`（如实登记，异于环节一）。证据 = ①
环节一脚本 `research_evidence_realdata.py` 在**环节二 HEAD 重跑**（**14/14 PASS**，证明证据契约未被打破）
+ ② 环节二集成套件 `integration/research/`（**79 passed**，真实 PG + 完整 HTTP）。产物 [`realdata-run.txt`](realdata-run.txt)。

## 9. 关联

- 计划：`docs/superpowers/plans/2026-10-12-research-session-evidence-and-platform.md`（环节二 Task 10–16）
- 灰度验收记录：`docs/superpowers/plans/2026-10-13-research-platform-phase2-acceptance.md`（供人接管）
- 执行台账：`.superpowers/sdd/2026-10-12-research-session-evidence-and-platform/`（brief + progress）
- Wiki：`Harness/wiki/research_platform_capabilities.md`、`Harness/wiki/data-model.md`（§环节二增量）
- Rules：`Harness/rules/变更记录强制规范.md`、`Harness/rules/测试规范.md`
- Memory：`qa-system-research-task-reaper-scope` / `qa-system-no-model-override` /
  `qa-system-multistep-no-data-dependency` / `qa-system-two-json-safe-mappings` / `qa-system-alembic-targets-prod`
- 关联变更（predecessor）：[feat-research-evidence-contract](../feat-research-evidence-contract/summary.md)

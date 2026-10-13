# 环节二（research-session-evidence-and-platform）灰度验收记录

- 日期：2026-10-13
- 分支：`feat/multi-step-persist`
- 范围：T10–T15 落地的平台级能力（台账/回收、快照、outbox 回放、mode 指引、会话级预算闸门）
  与 Task 16 的跨特性交汇点验收
- 形态裁定：**进程内**执行（无独立 worker 进程、无 compose 改动）

> 本文是**供人接管**的验收记录：如实登记 migration 版本、环境开关、已知限制与冒烟清单。
> 反例/缺陷一律写明，不做美化。

---

## 1. 数据库迁移（**验收前必须先应用**）

新增三张表 + 一列，落在 4 个 migration 上（`backend/alembic/versions/`，链式
`0117 → b1ea6cb32588_run_state → 0118 → 0119 → 0120 → 0121`；`0118` 的 `down_revision`
是 hash 命名的 `b1ea6cb32588`，**不**是 `0117`）：

| migration | 内容 | 说明 |
|---|---|---|
| `0118_research_active_run` | **列** `research_session.active_run_id`（FK → `research_run_state`） | 会话级执行权占用标记（互斥 + 释放不变式） |
| `0119_research_step_result` | 表 `research_step_result` | 每步结果的**按 turn/版本**落库（报告数据源） |
| `0120_research_event_outbox` | 表 `research_event_outbox`（`BIGSERIAL seq` 主键） | 事件先落库再广播 ⇒ SSE 断线可回放（`?lastEventId=`） |
| `0121_research_task` | 表 `research_task` | 任务台账（认领/回收/reaper） |

**T15（会话级预算只读闸门）没有 migration** —— 它只读 `session_token_usage` 的既有列，
不新增表/列/索引。

### 当前本地 prod（5433）落后 2 个 —— 验收前必须显式应用

实测（2026-10-13，只读 `SELECT`，未跑 alembic）：

```
$ PGPASSWORD=… psql -h localhost -p 5433 -U qa_user -d qa_metadata -tAc "SELECT version_num FROM alembic_version;"
0119
```

⇒ 本地 prod 停在 `0119`，`0120` / `0121` **未应用**。

> ⚠️ **alembic 默认打 prod**（memory `qa-system-alembic-targets-prod`）。验收前必须**显式**
> 传 `DATABASE_URL` 把 `0120` + `0121` 应用上去，否则后端启动时的 **schema-drift 闸门**
> 会挡住进程（`research_event_outbox` / `research_task` 表缺失）。命令示意（以实际部署
> 脚本为准，**不要**用 alembic 的默认连接）：

```
DATABASE_URL="postgresql+asyncpg://qa_user:<pw>@<host>:5433/qa_metadata" \
  python -m alembic upgrade head      # 从 0119 → 0121
```

---

## 2. 环境开关 / feature flags

| 变量 | 默认 | 语义 | 备注 |
|---|---|---|---|
| `SESSION_BUDGET` | `1.0`（`app/config.py:58`；`docker/.env` = `1.0`，`.env.example` = `0.1`） | 会话累计成本上限（美元）。`_runFrom` 在**每个相位边界**读累计成本，`cost >= budget` ⇒ 抛 `SessionBudgetExceeded` ⇒ `research.error{turn_failed}` + 会话 `failed` | **`<= 0` ⇒ 关闭闸门**（恒不超限）。⚠️ 与路由器 `ModelRouterService.selectModel` 的语义**相反**：那边把超预算（`>=`）当作「降级到最便宜模型」。同一配置值两处含义不同，**如实登记为分歧**（本任务不改路由器） |
| `RESEARCH_TASK_LEASE_SECONDS` | `1800`（`research_task_service.DEFAULT_LEASE_SECONDS`） | 任务租约秒数；env 非法只告警并回落默认 | **仅记录**：reaper **不**按它 gate（见 `research_task_service.py` 模块 docstring）；本任务不改行为 |

---

## 3. 已知限制（**必须在运维侧知情**）

### 3.1 预算闸门是**软上限**，不是硬顶（T15 评审 LOW-1）

闸门**只在相位边界**检查。因此：

- **单个相位内部**（尤其 `execute` 会连跑多步 LLM 调用 / NL2SQL / 出图）**可以超预算一段
  距离**，直到下一个相位边界才被拦下；
- 这是**有意**设计 —— 不半途打断一条相位，避免留下「半截状态」（相位中途终止会让 run
  state / 步结果停在不可解释的位置）。

> 运维口径：`SESSION_BUDGET` 是**软上限**（best-effort 停止），**不是**硬性费用封顶。
> 需要硬顶请在模型/网关侧限流，而非依赖本闸门。

### 3.2 成本读取持续失败 ⇒ 闸门**静默失效**（fail-open，T15 评审 MEDIUM-2）

`_readSessionCost` 读 `session_token_usage` 失败时**只留 WARNING 并按「未超限」放行**
（best-effort —— 计量打嗝不得杀研究）。若某会话的成本读取**持续**失败，则该会话的预算
**静默失效**（守门形同虚设），且**没有任何用户可见信号**。

> 这是**已知限制**，**非本任务修复项**。若要可观测，后续可在连续 N 次读失败后发一条
> 降级类 `research.error`（当前**未**实现）。

---

## 4. 形态裁定：进程内执行（无 compose 改动）

- 后台状态机在 **API 进程内**经 FastAPI `BackgroundTasks` 运行（`_runTurnInBackground` /
  `_resumeTurnInBackground`），**无**独立 worker 进程 / 队列消费者；
- 台账（`research_task`）的**认领**（原子条件 UPDATE）+ 启动期 **reaper**（`lifespan` 内）
  提供了「进程重启后回收 `leased` 僵尸任务」的自愈，无需外部调度器；
- 因此本任务**不改** `docker/docker-compose.yml` —— 与 Task 10+11 的裁定一致。

> 边界（如实登记，见 memory `qa-system-research-task-reaper-scope`）：reaper **只**回收
> `leased`；`pending`（崩溃窗口内尚未认领）与「启动时 `awaiting_user` 且持权」是**合法态**，
> 不在本任务范围。

---

## 5. 冒烟清单（人工，验收时逐条勾）

> 前置：§1 的 `0120` / `0121` 已应用、`schema-drift` 闸门通过、后端已起。

1. [ ] **后端起来**：进程无 schema-drift 报错；`GET /api/v1/research/sessions` 200（带
       `X-User-Id`）。
2. [ ] **建会话带 mode**：`POST /api/v1/research/sessions`（带 `question` + `mode`）⇒ 201。
3. [ ] **跑一轮**：`POST /api/v1/research/sessions/{sid}/turns` ⇒ 202；SSE
       `GET /api/v1/research/stream?sessionId={sid}` 收到 `research.connected` → 各相位事件
       → `research.done`。
4. [ ] **断线**：中途关闭 SSE 连接（或断开网络）。
5. [ ] **带 `lastEventId` 重连见回放**：`GET /api/v1/research/stream?sessionId={sid}&lastEventId={seq}`
       ⇒ 帧带 `id: {seq'}`，重放出「错过」的事件（含终态帧）。**T16 已由 J1 自动化覆盖**（见 §6）。
6. [ ] **造超预算会话**：把某会话累计成本推过 `SESSION_BUDGET` ⇒ 下一轮在相位边界终止
       （`research.error{turn_failed}`，消息带真实成本/预算），会话 `failed`。
7. [ ] **超预算后仍可提交**：同一 `failed` 会话 `POST /turns` ⇒ **202**（不是 409），执行
       权已释放。**T16 已由 J2 自动化覆盖**（见 §6）。
8. [ ] **（可选）进程重启回收**：置一条 `research_task` 为 `leased` ⇒ 重启后端 ⇒ reaper 把
       其翻 `orphaned`，对应会话释放。

---

## 6. Task 16 验收产出（本任务）

| 项 | 内容 |
|---|---|
| 新增用例文件 | `backend/app/tests/integration/research/test_platform_enhancements_e2e.py` |
| J1 | `test_over_budget_terminal_error_is_persisted_and_replayable`（T15 × T12） |
| J2 | `test_over_budget_abort_releases_session_for_reuse`（T15 × 释放执行权不变式） |
| 新增文件跑绿 | `2 passed`（`python -m pytest app/tests/integration/research/test_platform_enhancements_e2e.py -q`） |
| 回归（本目录，**分进程**） | `79 passed`（T15 后基线 77 + 2） |
| 前端 | `vitest run src/tests/EvidencePanel.test.tsx src/tests/menuI18n.test.ts` ⇒ `77 passed` |
| 真实缺陷修复 | `topNReplay` 文案「采样」→「结果行按度量排名」（zh/en + 组件注释 + 前端测试） |

### 6.1 反向守卫（证明用例真咬）

- **J1**：把 `_runTurnInBackground` 的 `emit=partial(_persistAndPublish, sessionId)` 临时改成
  直连 `bus.publish`（不落 outbox）⇒ 实时终态帧 `id` 变 `None` ⇒ 用例 **RED**（在
  「超预算终止帧必须落 outbox（帧上带 seq）」处失败）。**已逐字节还原**（`shasum` 一致）。
- **J2**：把 `ResearchAgentService.markFailed` 临时改成只 `updateSessionStatus`
  （**不**清 `active_run_id`）⇒ 终止后 `active_run_id` 仍非空 ⇒ 用例 **RED**（前置断言
  `active_run_id IS NULL` 失败；若跳过该前置，则后续 `POST /turns` 得 **409** 而非 202）。
  **已逐字节还原**（`shasum` 一致）。

> **重要口径（与 brief 的差异，如实登记）**：brief 的 J2 原设想是「成本预置超预算 ⇒ **首个**
> 相位边界即终止」。实测该场景下执行权（`active_run_id`）**从未被 commit**（事务回滚后库里
> 本就是 NULL）⇒ 释放与否对 DB 无影响，**反向守卫会假绿**。故 J2 改为把终止**推迟**到
> `execute` 相位提交之后的 `hypothesis` 边界（经 `tokenUsage=` seam 抬高后段成本）——
> 这才是「终止必须释放执行权」真正承重的场景。

---

## 7. 关联目录 / SSOT

- outbox 回放契约：`backend/app/api/v1/research.py`（`_persistAndPublish` / `_eventFrames` /
  `_fetchMissed` / `_isSessionTerminal`）
- 预算闸门：`backend/app/services/research_agent_service.py`（`_sessionBudgetExceeded` /
  `_runFrom` / `markFailed` / `failSession`）
- 台账与回收：`backend/app/services/research_task_service.py`
- 事件摘要（topN 语义）：`backend/app/services/research_evidence_summary.py`（`_topNRows` /
  `sample_rows`）

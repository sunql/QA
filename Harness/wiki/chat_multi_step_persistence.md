---
created: 2026-10-06
updated: 2026-10-06
sources:
  - docs/superpowers/specs/2026-10-05-multi-step-persist.md
  - docs/superpowers/plans/2026-10-05-multi-step-persist.md
  - backend/app/domain/multi_step_models.py
  - backend/app/services/multi_step_persistence.py
  - backend/app/services/multi_step_persist_hooks.py
  - backend/app/services/multi_step_compressor.py
  - backend/app/services/multi_step_retry.py
  - backend/app/services/multi_step_resume.py
  - backend/app/jobs/cleanup_multi_step_runs.py
  - backend/app/api/v1/chat.py
  - backend/alembic/versions/0114_multi_step_persist.py
  - backend/alembic/versions/0115_multi_step_run_session_id_text.py
  - frontend/src/components/chat/ResumeRunButton.tsx
tags:
  - multi-step
  - chat
  - persistence
  - resume
  - compression
  - retry
---

# 多步问答落库、重试、压缩与续跑

把多步（拆步）问答的每一步执行结果**落库**，让一次拆步跑可审计、失败可续跑、上下文超限可压缩、终态 run 可回收。

> **状态**：Task 1-9 已落地（2026-10-06）。spec §8 的 UI 只完成了**聊天面板**部分
> （失败步续跑按钮 + 压缩徽章，§8.2）；session 列表「未完成」徽章（§8.1）与续跑弹窗
> （§8.3，选步下拉 + `compressAgain` 复选框）**未做**。
> 保留期清理函数已交付，但**没有任何调度入口** —— 30/7 天只是代码里的两个常量，
> 本机不会自动回收（详见 `Harness/changes/feat-multi-step-persist/summary.md` 遗留项 13）。
>
> 对应 spec：`docs/superpowers/specs/2026-10-05-multi-step-persist.md`
> 对应 plan：`docs/superpowers/plans/2026-10-05-multi-step-persist.md`
> 变更 SSOT：[[Harness/changes/feat-multi-step-persist/summary.md|Multi-Step Persist]]

---

## 0. 一句话结论

多步执行链路（[[nl2sql-engine]] 的拆步入口）在 `_executeDataStep` 前后插入
**落库钩子**（`MultiStepPersistMixin`）：每一步的 SQL / 数据 / 用量 / 错误实时写进
`multi_step_step`，run 级状态写进 `multi_step_run`；失败时前端凭 `runId` 调
`POST /chat/multi-step/{runId}/resume` 从失败步续跑，而不是整跑重来。

---

## 1. 两张表与它们的关系

| 表 | 粒度 | 关键列 |
|---|---|---|
| `multi_step_run` | 一次拆步跑 | `id`(UUID PK)、`session_id`、`question`、`model_id`、`datasource_id`、`status`、`total_steps`、`completed_steps`、`current_step_idx`、`compressed_count`、`resume_count`、`version`、`idempotency_keys`(JSONB)、`error_summary`、`started_at` / `updated_at` / `finished_at` |
| `multi_step_step` | run 里的一步 | `id`(UUID PK)、`run_id`(FK → `multi_step_run.id`，`ondelete="CASCADE"`)、`step_index`、`status`、`sub_question`、`sql`、`sql_hash`、`data`(JSONB)、`data_compressed`(JSONB)、`chart_option`(JSONB)、`model_used`、`tokens_used`、`cost`、`attempt_count`、`last_error` / `last_error_kind`、时间戳 |

- 关系：一个 `multi_step_run` 对多个 `multi_step_step`（`uq_multi_step_step_run_index` 约束 `(run_id, step_index)` 唯一；删 run 级联删步）。
- **`session_id` 是自由字符串 `String(64)`，无外键**（0115 迁移）。原 0114 设计是 `UUID + FK → research_session.id`，但多步落库的唯一消费方是 **chat 链路**，而 chat 会话 id 是 `chat-<uuid>` / `docqa-<uuid>` 这类自由字符串、**从不创建** `ResearchSession` —— 该外键在唯一消费方里永远悬空（详见 [[qa-system-chat-session-id-contract]]）。
- 索引：`multi_step_run` 上 `(session_id)`、`(session_id, updated_at)`、`(status, updated_at)`；`multi_step_step` 上 `(run_id)`、`(status, updated_at)`。清理任务按 `(status, updated_at)` 找过期行。
- 状态列是 `String(20)`，**没有 DB 层 CHECK 约束**，取值由应用层常量（`RUN_STATUS_*` / `STEP_STATUS_*`）约束。
- 表的详细字段清单见 [[data-model]]。

## 2. 状态机

**run**（`app/domain/multi_step_models.py`）：

```
running ──▶ succeeded          全部步 succeeded
        ├─▶ failed             某步 failed 且无 skipped 兜底
        └─▶ partially_failed   存在 skipped 步，但后续步跑完
```

**写侧不变量（IMP-1 方案 B）**：

- **终态只在 `_closeRun` 落**，且 `status` 与 `finished_at` 由**同一条 UPDATE** 落下；
  `updateRun` 对「终态而无 `finished=True`」结构性抛 `ValueError`（唯一写点收口）。
- **单步的 per-attempt 失败（`_recordStepFailure`）不写 run 终态** —— 只记该步这次尝试的
  错误与进度指针 `current_step_idx`，run 保持 `running`（计划可能继续）。
  早期实现反手写了 `status=failed` 却不落 `finished_at`，而循环中途
  `token_usage_service.recordUsage` 的 `commit()` 会把这个中间态持久化 ⇒ 活着的 run 在库里
  已是 `failed`：并发续跑据此劫持它，且清理谓词（要求 `finished_at < cutoff`）永不回收。
- 续跑重开走 `updateRun(..., status=running, finished=False)`（`finished` 三态：`None` 不动 /
  `True` 落 / `False` 清空），不再直接改 ORM 属性绕过写点。

**step**：

```
pending ─▶ running ─ok─▶ succeeded
             │
             ├─err(transient)─重试─▶ running        # attempt_count++
             ├─ctx > 70% ─压缩─▶ compressed ─rerun─▶ running
             ├─err(permanent)─▶ failed
             └─压缩后仍 > 95% ─▶ skipped             # 后续不再补（spec 定义；触发点未实现）
```

## 3. 自动重试（`multi_step_retry.py`）

- **3 次尝试封顶**（`MAX_ATTEMPTS = 3`），尝试之间只等 **2 次**：`TRANSIENT_WAITS = (1, 2)` —— 即 1s → 2s → 第 3 次失败即转人工。
- 瞬态判定：HTTP `{429, 500, 502, 503, 504}` + `httpx.TransportError` / `ConnectionError` / `asyncio.TimeoutError`；`Nl2SqlError` / `LLMUnavailableError` 归**永久**（NL2SQL 自带重试/降级，plan 校验失败重试白烧 token）。
- 分类**沿整条 `__cause__` 链**判定 —— provider 失败都被 `LlmClientError(...) from exc` 包住，只看最外层会把「不可达 / 超时」误判为永久（见 [[qa-system-llm-error-cause-chain]]）。
- 重试路径**不新发** `_recordUsage`，用量在既有累加器里累加，避免 token 双计。

## 4. 上下文压缩（`multi_step_compressor.py`）

- 触发：`estimatedTokens > model.max_input_tokens * COMPRESS_THRESHOLD`，阈值 **0.7**。
- 压缩发生在「某步跑完、下一步 prompt 构造前」；被压缩的是**更早**的步，其 `data` 原样保留、摘要写进 `data_compressed`（spec §5.3：`data` 永不删除）。
- 压缩后补发 `step_compressed` SSE 事件，前端把该步徽章覆盖成「已压缩」。
- **续跑重置时必须显式清 `data_compressed`**：`multi_step_persistence.resetStepsFrom` 只回退状态、不清 `data_compressed`，故 `prepareResume` 里那段显式清空是**承重的** —— 少了它会留下「`status=pending` 但 `data_compressed` 非空」的非法态，且压缩钩子见非空即跳过，该步此后永远无法再压缩（见 [[qa-system-resetstepsfrom-data-compressed]]）。

## 5. 续跑端点与幂等

```
POST /api/v1/chat/multi-step/{runId}/resume
  body: ResumeRequest { from_step_index?: int, model_override?: int, compress_again?: bool }
  header: Idempotency-Key: <uuid>
  → SSE 流（与 POST /chat/stream 同一套事件序列）
```

- 校验（`multi_step_resume.prepareResume`）：run 必须存在（否则 404）、`status ∈ {failed, partially_failed}`（否则 409）、`from_step_index` 之前的所有步必须是 `succeeded` / `compressed`（否则 409）、`from_step_index` 必须落在 `0..len(steps)-1`（越界 409）。
- 默认起点 = 首个 `failed` / `skipped` 步。
- 幂等：`Idempotency-Key` 记进 `multi_step_run.idempotency_keys`（JSONB），重复 key 直接 409；并发靠 `version++` 乐观锁（无 `SELECT … FOR UPDATE`，极端并发下仍可能双跑，见遗留项 12）。
- 起始步的**唯一事实来源是 DB**（`run.current_step_idx`），不经 DTO 传递；执行侧 `adoptRunForResume` 从那里读，且**只在本次计划形状与原 run 逐字一致时**沿用，形状变了（换模型 / 重新规划出不同子问题）就归零整跑。
- 越界指针（写侧 `_closeRun` 落的是 `len(plan.steps)` 这个越过末尾的哨兵）在读侧被钳制为「无可续跑进度」，否则续跑会整跑跳过还把它封成终态（见 [[qa-system-multistep-current-step-idx-sentinel]]）。
- 路由在 `StreamingResponse(background=...)` 上挂 `_sealAbandonedResume`：续跑被重新路由成单步或流中断时，run 仍停在 `running`，由它显式封口成 `failed`（**不是**生成器 `finally` —— 断连主情形不触发）。
- **续跑不做前序结果回灌**（spec §7.2 step 5「plan 重放」未做）：从起点起跑、跳过更早的步（仍计入 `completed`），不把前序 `data` 拼回 prompt —— 依据是「多步之间无步间数据依赖」已被证伪（见 [[qa-system-multistep-no-data-dependency]]）。

## 6. 保留期

- `succeeded` 保留 **30 天**，`failed` / `partially_failed` 保留 **7 天**（`SUCCEEDED_RETENTION_DAYS` / `FAILED_RETENTION_DAYS`）。
- **非终态（`running`）的 run 刻意永不删除** —— spec 没给它们定规则；被 kill 后残留的 `running` 行会无限累积（见遗留项 14）。
- **终态但 `finished_at IS NULL` 的行也**不回收（谓词两分支都要求 `finished_at < cutoff`）。方案 B 之后生产侧不再产生该形态；历史遗留行需运维一次性回填 `UPDATE multi_step_run SET finished_at = updated_at WHERE status IN ('succeeded','failed','partially_failed') AND finished_at IS NULL`（见遗留项 16）。**不加** `COALESCE(finished_at, updated_at)` 兜底臂是显式裁定：兜底臂会在未来回归时把证据行级联删掉、掩盖回归。
- 实现：`backend/app/jobs/cleanup_multi_step_runs.py` 的 `async def cleanupMultiStepRuns(session, *, succeededRetentionDays=30, failedRetentionDays=7, now=None) -> int`。
- ⚠️ **没有调度入口**：无 `__main__`、无 `scripts/cron_*.sh` 包装、无 crontab / launchd 注册，全仓生产代码里没有任何调用点（只有它自己的集成测试 import）。**30/7 天当前不会自动执行**。

## 7. Feature Flag

- 环境变量 `MULTI_STEP_PERSIST_ENABLED`，配置字段 `multiStepPersistEnabled`，默认 `True`（`backend/app/config.py`）。
- `True`：完整功能（落库 + 重试 + 压缩 + 续跑）；`False`：退回现状（不落库、不重试、不压缩），`/resume` 端点失效。
- 关闭时**仍做错误分类**（只是不落库），便于 kill switch 下行为一致。

## 8. 前端接线（聊天面板）

- `step_compressed` 事件 + 压缩徽章；失败步渲染 `ResumeRunButton`（`data-testid="resume-run"`），**仅当该步带 `runId` 时**渲染（单步路径不发 `runId`）。
- 续跑经 store 的 `resumeRun` 统一发起，与首发共用同一套 SSE handler；`Idempotency-Key` 由前端 `crypto.randomUUID()` 生成。
- 续跑的写入**按目标消息定向**：只更新被续跑的那条 assistant 消息，不动最新那条（见变更记录 Task 9 的续跑定向写入修复）。
- 非流式渲染下**没有**续跑入口与压缩徽章（`runId` / 「已压缩」只走 SSE 增量，非流式 `steps` 负载不带），见遗留项 8。

## 相关条目

- [[data-model]] — `multi_step_run` / `multi_step_step` 两张表挂在数据模型上
- [[chat-service-capabilities]] — 多步是 `ChatService` 的能力之一（`MultiStepPersistMixin` 经 MRO 合并）
- [[nl2sql-engine]] — 每一步的 SQL 由它产出，本设计在 `_executeDataStep` 前后插入钩子
- [[Harness/changes/feat-multi-step-persist/summary.md|Multi-Step Persist]] — 本变更 SSOT
- [[qa-system-resetstepsfrom-data-compressed]] — `resetStepsFrom` 不清 `data_compressed`，`prepareResume` 的显式清空是承重的
- [[qa-system-multistep-no-data-dependency]] — 多步之间无步间数据依赖，故续跑不回灌
- [[qa-system-chat-session-id-contract]] — `session_id` 是自由字符串、无 FK 的契约来源
- [[qa-system-multistep-current-step-idx-sentinel]] — `current_step_idx` 越界哨兵与读侧钳制
- [[qa-system-llm-error-cause-chain]] — 错误分类沿整条 `__cause__` 链判定

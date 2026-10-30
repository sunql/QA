# 变更：2026-10-31-replay-plan-cards

- **日期**：2026-10-31（同日实施 + 部署完成）
- **作者**：Claude（与用户三项需求驱动）
- **状态**：**已实施；prod 已部署（backend + frontend 镜像滚动，迁移 0138 已落地）+ 单步冒烟通过**
- **关联 PR**：
  - PR #77 `feat/chat-card-order` → main `50d7fa82`（卡片固定阅读顺序）
  - PR #78 `feat/history-replay-plan-cards` → main `37f72a76`（历史回放还原计划卡）
- **迁移版本**：`0138_session_message_replay_plan_cards`（单 head；prod 已于 2026-10-31 迁移）
- **发版源**：`/private/tmp/qa-deploy-1031-replay`（保留作回退基线；上一发版源 `/private/tmp/qa-deploy-1031-l4fix` 惯例保留）
- **关联变更**：
  - predecessor：[feat-multi-step-persist](../feat-multi-step-persist/summary.md)（multi_step_run/step 续跑 SSOT——本批刻意不依赖它做回放）
  - predecessor：[fix-unanswerable-and-detached-client](../fix-unanswerable-and-detached-client/summary.md)（「无法回答」分支——本批同型补快照）

---

## 1. 需求（用户原文三项）

| # | 原文 | 落点 |
|---|---|---|
| 1 | 「需要还原计划卡，单独开分支处理」 | PR #78：刷新/重开会话后还原多步计划卡（分析计划/查询计划/分步 SQL） |
| 2 | 「单个问题……顺序是查询计划、分析计划、查看SQL，调整为分析计划、查询计划、查看SQL，单独开个分支」 | PR #77：MessageItem 渲染顺序调整 |
| 3 | 「L4 纯探索问题单次成本仍偏高的问题应该如何解决给个建议」 | 纯咨询，另出建议，不改码（见本文末尾 §9） |

## 2. PR #77：卡片固定阅读顺序

**问题**：流式事件到达顺序（L4 顶层 QueryPlanCard 先、multi_step_plan 后）决定了卡片出现顺序，与用户期望的阅读顺序相反。

**修法**：渲染顺序与事件到达顺序解耦——`MessageItem.tsx` 固定输出 分析计划(MultiStepPlanCard) → 查询计划(QueryPlanCard) → SQL(SqlPreview)；QueryPlanCard 保持 `!isStreaming` 门控（流式期间顶层计划卡不闪现）。

**测试**：`MessageItem.test.tsx` 新增 describe「MessageItem 卡片顺序」，`compareDocumentPosition & DOCUMENT_POSITION_FOLLOWING` 断言三卡先后；前端全量 2234/0。

## 3. PR #78：历史回放还原计划卡

### 3.1 取证结论（为什么不是从 multi_step_step 回放）

| 缺口 | 证据 |
|---|---|
| multi_step_step 缺 description / aggregation_only / 每步 queryPlan | 表结构无此三列 |
| 汇总步（aggregation_only）从不进 completed、从不落行 | 多步状态机：汇总步只在事件流出现 |
| multi_step_run/step 是**续跑** SSOT，形状是 DB 口（snake_case、不含产物负载） | 回放需要的是**前端口**快照 |

⇒ 决策：**回放快照方案**——`session_message` 加两个可空 JSONB 列，与续跑表解耦。

### 3.2 数据模型（0138）

- `session_message.query_plan JSONB NULL`：单步顶层 NL2SQL 计划（`outcome.plan.to_dict()`）
- `session_message.steps_snapshot JSONB NULL`：回放快照（数组，形状对齐前端 `MultiStepStep` camelCase，14 键：stepIndex/description/subQuestion/aggregationOnly/status/sql/summary/error/chartType/chartOption/tableOption/visualRationale/queryPlan/runId）
- `data` **有意排除**：SQL 结果行由 chartOption 的 TABLE rows 承载（与 0105 落库口径一致），避免快照体积爆炸
- runId 盖到每步：前端失败步续跑按钮需要（顶层不存 runId）

### 3.3 快照构造器（`chat_helpers.py`）

- `buildStepsSnapshot(*, plan, stepsByIdx, completed, runId)`：三源合流——步身份来自计划（description/aggregationOnly/subQuestion）、终态以 DB 行为准（compressed 只有 DB 知道；`_REPLAY_STATUS_BY_DB = {"succeeded":"done","failed":"error","compressed":"compressed"}`，无 DB 行按 result.error 推断 error 否则 done）、产物来自 StepResult（SQL/summary/chart/table/visualRationale；无 StepResult 但 DB 有 sql 时保 SQL）
- `buildSingleStepSnapshot(*, description, subQuestion, sql, summary="")`：单步/无法回答变体最小形状
- **出口统一过 `jsonSafe`**：集成测试实锤——每步 chartOption/tableOption 带 SQL 结果 Decimal → `Decimal is not JSON serializable` → PendingRollbackError 炸整条 INSERT。这是本批抓到的真 bug

### 3.4 写入点

- 多步：`chat_stream_multistep.py` 汇总落库点带 `steps_snapshot=buildStepsSnapshot(...)`
- 单步：`chat_stream_query.py` 正常分支带 `query_plan` + `steps_snapshot=buildSingleStepSnapshot(...)`；「无法回答」分支同型（description="无法回答"）

### 3.5 读取与前端收窄

- 后端：`ChatMessageRead` 加 `query_plan` / `steps_snapshot`（CamelModel → camelCase）；`session_history_service.loadFullMessages` 透传
- 前端：`chatHistory.ts` 两字段 `unknown`（系统边界原始 JSON，不硬 cast）；`chatStore.toChatMessage` 经 `asReplaySteps` 收窄——status 白名单 `REPLAY_STEP_STATUSES`（白名单外→"done"）、逐项结构校验、无合法项返回 undefined（=「这轮没有分析计划卡」，不渲染空卡）

## 4. 部署记录（2026-10-31）

- 迁移：`docker compose run --rm backend alembic upgrade head`（旧容器 exec 看不到新迁移文件——`docker compose exec` 空跑的坑重演）；0137→0138，prod `alembic_version=0138`
- frontend build：`docker compose build --build-arg VITE_API_BASE_URL= frontend`——compose 的 `${VAR:-default}` 把空 shell 变量当未设置吃进 .env 默认值，**必须用 `--build-arg` 显式传空**；bundle `grep -c localhost:5173` = 0 验证
- .env secrets 全量复用（从 1031-l4fix 发版源复制；SECRET_KEY/JWT_SECRET 未重新生成）

## 5. 冒烟证据（prod，2026-10-31）

- 会话 `chat-smoke-1031-replay`（问法「请分步查询 2024 和 2025 年的采购金额并对比」，datasourceId=1）：
  - 路由为 **L2 单步**（一次出 SQL 合并两年对比，路由决策合理，未走多步拆分）
  - DB：assistant 行（id=1661）`query_plan=有`、`steps_snapshot` 1 步（14 键齐全、status=done、SQL 保真、runId=null）
  - 回放端点 `GET /sessions/chat-smoke-1031-replay/messages`：assistant 行返回 `queryPlan=true`、`stepsSnapshot` 结构与 DB 一致
- 会话 `chat-smoke-1031-replay-ms`（明确双任务问法验证多步快照路径）：见同目录 smoke 输出（多步链路慢，>180s 属正常）
- 调试 JWT 在 qa-backend 容器内签发（jti d3d451d3…，token 存容器 /tmp/tok；`app.config.getSettings`、`import jwt as pyjwt`、user_sessions 列是 `created_time`）

## 6. 测试与门禁

- 后端：新增 `test_chat_steps_snapshot.py`（10 用例）+ 集成 `TestReplayPlanCardSnapshot`（2 用例）；回归 45 passed；基线红 6 不变（test_chat_service.py TestBuildAnswerPrompt 4 + test_chat_service_stream.py TestStreamingPipeline 2）——**净新增失败 0**
- 前端：2236/0、tsc 0 错误；chatStore.test.ts 2 新用例
- alembic：单 head 0138

## 7. 已知边界 / 未做

- 历史回放**不还原** L4 Agent Loop 的过程轨迹（工具调用/中间推理）——PR #76 的「历史回放不还原 steps」待立项项仍在，本批只还原计划卡与产物
- 快照是**写时快照**：落库后回放永远是当时的形状，不随前端组件升级重解释（unknown 收窄保证旧快照不炸新前端）
- 用户任务 3（L4 成本建议）为咨询项，结论在会话回复中，未落码

## 8. MEMORY 关联

- `qa-system-l4-hijacks-multistep.md`：未修缺口 #1/#2（回放侧）由本批关闭
- `qa-system-chat-chart-rendering.md`：发版源指针更新为 `/private/tmp/qa-deploy-1031-replay`（`37f72a76`）
- 新增：compose `:-` 空值陷阱、`docker compose run --rm backend alembic` 模式

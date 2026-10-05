# 多步问答「落库 + 自动重试 + 动态压缩 + 手动续跑」设计

- 日期：2026-10-05
- 状态：brainstorming 通过，待实现
- 关联 spec：`Harness/changes/feat-qwen-multistep-uplift/summary.md`（已完成的 qwen 路由 hook，**与本设计无功能耦合**——本设计独立运行）

## 1. 背景

多步问答（含显式分步「第一步…第二步…」与自动拆解的复杂问题）在执行过程中存在两类高发失败：

1. **网络中断 / 服务端超时**：第 1、2 步成功，第 3 步时 oMLX 或 deepseek 端 5xx / timeout，整次任务失败，前 N 步的工作全部丢失
2. **上下文过大**：多步累积的实体列表 / 聚合值塞入后续步 prompt，超过 model.max_input_tokens，模型返回空 / 截断 / 报 plan 错误

现状痛点：
- 无任何 step 状态持久化，前 N 步的 SQL / data / 错误信息全在内存
- 失败后只能整次重跑（重跑成本 = N 步 token + N 步延迟）
- 上下文超限没有降级路径

本设计目标：每一步实时落库 → 失败可定位 → 临时错误自动重试 → 永久错误手动续跑 → 上下文临界动态压缩。

## 2. 决策记录（与用户对齐结果）

| 决策点 | 选项 | 选择 | 理由 |
|---|---|---|---|
| 落库范围 | 多步 / 多步+研究 / 全 chat | **只覆盖多步（显式 + 自动拆解）** | 最小改造，与 qwen-uplift 同链路；不污染单轮 |
| 重试机制 | 全手动 / 临时自动+永久手动 / 全自动 | **临时错误自动重试，永久错误手动** | 满足「网络问题自动续」+「配置问题人工定」语义 |
| 存储形态 | 独立表 / session_message JSON / 磁盘文件 | **独立表 `multi_step_run` + `multi_step_step`** | 可查询、可对账、状态机清晰；与 0113 ResearchSession 风格一致 |
| 上下文压缩 | 动态压缩 / 硬阈值截断 / 不处理 | **动态压缩（超 70% model.max_input_tokens 时）** | 不放过任何「跑到一半 ctx 撑爆」的失败 |
| 压缩策略 | 行截断+聚合 / LLM 摘要 / **行截断+关键列+极值点** | **方案 ③：行截断 + 关键列提取 + 极值点** | 零额外 LLM 调用、不扩张错误面、对业务分析题信号量最高 |

## 3. 数据模型

### 3.1 `multi_step_run`

每多步请求一行。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | run 主键 |
| `session_id` | UUID | 关联 `research_session.id`（已有表，0113） |
| `question` | TEXT | 原始问题快照（用于审计） |
| `model_id` | INT | 用户请求的 model_id（model 可删，会话要记事实） |
| `status` | VARCHAR(20) | `running` / `succeeded` / `failed` / `partially_failed` |
| `total_steps` | INT | 总步数（plan 解析后写入） |
| `completed_steps` | INT | 已成功完成步数（用于 UI 进度） |
| `current_step_idx` | INT | 当前执行到的步号；失败时 = 首个未成功步的 index |
| `compressed_count` | INT | 走过压缩的步数（用于诊断「上下文压力」） |
| `resume_count` | INT DEFAULT 0 | 该 run 被续跑次数（每次成功续跑 +1） |
| `version` | INT DEFAULT 0 | 乐观锁版本号；续跑 +1；并发续跑冲突时返 409 |
| `idempotency_keys` | JSONB DEFAULT '[]' | 已处理的续跑幂等键列表（JSON 数组） |
| `started_at` | TIMESTAMPTZ | run 起始 |
| `updated_at` | TIMESTAMPTZ | 最近一次状态变更 |
| `finished_at` | TIMESTAMPTZ NULL | 终态时填 |
| `error_summary` | TEXT NULL | run 级致命错误的简短描述（不存 traceback） |

**索引**：
- `(session_id, updated_at)`：按会话查 run 历史
- `(status, updated_at)`：找失败 / 部分失败的 run

### 3.2 `multi_step_step`

每步一行。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | step 主键 |
| `run_id` | UUID | 关联 `multi_step_run.id` |
| `step_index` | INT | 步号 0-based |
| `status` | VARCHAR(20) | `pending` / `running` / `succeeded` / `failed` / `skipped` / `compressed` |
| `sub_question` | TEXT | 改写后的子问题（StepSubquestionRewriter 改写后的版本） |
| `sql` | TEXT NULL | 生成的 SQL |
| `sql_hash` | VARCHAR(64) NULL | SQL 文本 hash，续跑命中 LLM cache 的键 |
| `data` | JSONB NULL | 原始执行结果（压缩前的完整 data，**永不删除**） |
| `data_compressed` | JSONB NULL | 压缩版（仅当 status=compressed 时有值） |
| `chart_option` | JSONB NULL | 步骤级图表 spec |
| `model_used` | VARCHAR(64) | 实际执行用的模型（可能因 LLM 端 fallback 与 model_id 不同） |
| `tokens_used` | INT | 本步累计 token（含重试） |
| `cost` | NUMERIC(12,6) | 本步累计 cost（含重试） |
| `attempt_count` | INT | 已重试次数 |
| `last_error` | TEXT NULL | 最近一次错误描述 |
| `last_error_kind` | VARCHAR(20) NULL | `transient` / `permanent` |
| `started_at` | TIMESTAMPTZ | 本步起始 |
| `updated_at` | TIMESTAMPTZ | 最近一次状态变更（重试时刷新） |
| `finished_at` | TIMESTAMPTZ NULL | 本步终态时间 |

**索引**：
- `(run_id, step_index)` 唯一：保证步号连续不重复
- `(status, updated_at)`：找失败步

## 4. 状态机

### 4.1 单步状态

```
        start
          ▼
       pending
          │
          ▼
       running ──ok──▶ succeeded
          │
          ├─err(transient) ──retry──▶ running   # attempt_count++
          │
          ├─ctx > 70% ──compress──▶ compressed ──rerun──▶ running
          │
          ├─err(permanent) ──▶ failed
          │
          └─ctx > 95% after compress ──▶ skipped   # 后续不再补
```

### 4.2 run 状态

- `succeeded`：所有步 `succeeded`
- `failed`：某步 `failed` 且无 `skipped` 兜底
- `partially_failed`：存在 `skipped` 步，但后续步能跑完
- `running`：进行中

## 5. 压缩策略（方案 ③）

### 5.1 触发条件

`_executeDataStep` 跑完后、下一步 prompt 构造前，调用 `_shouldCompress(run_id, next_step_idx, model)`：

```python
estimated = _estimatePromptTokens(run, next_step_idx, model)
return estimated > model.max_input_tokens * 0.7
```

### 5.2 保留内容

`_compressStepData(stepRow)` 对 `stepRow.data` 做列级分析：

| 列类型 | 保留策略 |
|---|---|
| **GROUP BY / 时间列** | 全部 distinct values |
| **数值列** | MAX / MIN / AVG / SUM + **同比/环比绝对值 top 5** |
| **类别列** | distinct value 列表 |
| **元信息** | 总行数、压缩前 token、压缩比 |

### 5.3 写回

- 原 `data` 字段保留不动（**永不删除**，可逆）
- `data_compressed` 写入压缩版 JSON
- `status` 标 `compressed`

### 5.4 前端展示

渲染 step 卡片时，若 `data_compressed is not None`，顶部加：

> ⚠️ 数据已压缩（1000 行 → 30 行 + 聚合）。原始数据[点此展开]

点展开后渲染原始 `data`。

## 6. 重试策略

### 6.1 错误分类

`_classifyError(exc) -> "transient" | "permanent"`：

| 错误类型 | 分类 |
|---|---|
| `httpx.ConnectError` | transient |
| `httpx.TimeoutException` | transient |
| HTTP 429（限流） | transient |
| HTTP 500 / 502 / 503 / 504 | transient |
| `LLMUnavailableError`（503 类） | transient |
| `ctx > 95%` 压缩后仍超限 | permanent |
| Plan 校验失败（SQL Guard / 关联路径） | permanent |
| NL2SQL `ValidationError` | permanent |
| 其他 4xx（非超时类，429 除外） | permanent |

### 6.2 自动重试曲线

仅 `transient` 触发，**3 次封顶**：

```
attempt 1 → wait 1s → attempt 2 → wait 2s → attempt 3 → 转 manual
```

每次重试：
- `attempt_count += 1`
- `last_error` / `last_error_kind` 更新
- `tokens_used` / `cost` 累加（不覆盖）
- 不重置 `started_at`

### 6.3 永久错误处理

- `multi_step_step.status = failed`
- `multi_step_run.status = failed`
- 不再触发后续步
- UI 卡片红色 + 「续跑」按钮

## 7. 续跑 API

### 7.1 端点

```
POST /api/v1/chat/multi-step/{run_id}/resume
Authorization: Bearer <jwt>
Content-Type: application/json
Body: {
  "from_step_index": int,         # 从第几步开始重跑（含）；默认 = 首个 failed/skipped 的步
  "model_override": int | null,    # 可选；非空时本次 run 临时换模型（仅本 run，不改 session.model_id）
  "compress_again": bool           # 是否在续跑前再压一遍更早的步（默认 false）
}
Response: SSE stream（与 /api/v1/chat 一致的事件序列）
```

### 7.2 行为

1. 加载 run + 所有 step
2. 校验：
   - `run.status` 必须是 `failed` 或 `partially_failed`
   - `from_step_index` 之前的所有步 `status ∈ {succeeded, compressed}`
   - 当前用户有 session 读权限（与 `/chat` 同 RBAC）
3. 把 `from_step_index` 开始的步 `status` 重置为 `pending`，清空 `last_error*`
4. `run.status = running`，`current_step_idx = from_step_index`
5. 重放 plan（plan 已在 `multi_step_run` 关联的 plan 中，或重新从 sub_question 列表读）
6. 从 `from_step_index` 开始执行每步：先 SQL cache 命中（`sql_hash` 一致）则直接执行，否则重跑 LLM 生成
7. SSE 流式返回事件给前端

### 7.3 幂等

- 续跑请求带 `Idempotency-Key` header（前端生成 UUID），后端在 `multi_step_run.idempotency_keys TEXT[]` 记录已处理 key
- 重复请求（同 key）：若对应续跑已完成 → 返 200 + 已落库结果（不重跑）；若仍在跑 → 返 409 + 提示「续跑进行中」
- 续跑成功提交后 `resume_count += 1`；并发续跑靠 `version` 字段乐观锁保护（冲突返 409）

## 8. UI 改造

### 8.1 session 列表

- 失败的 session 卡片右上角加 `⚠️ 部分失败` 徽章
- 鼠标悬停显示 `已成功 X 步 / 共 N 步`
- 点击「续跑」按钮触发 §7 API

### 8.2 聊天面板

- 多步 step 卡片底部新增状态行：
  - `✓ 成功 · 用时 Xs · X token`
  - `⚠️ 压缩（1000→30 行）· 续跑前可点此[展开原始数据]`
  - `✗ 失败 · 原因：[network] oMLX timeout · [续跑]`
- 永久错误卡片额外显示建议：「网络问题可点续跑；配置/数据问题请修正后重试」

### 8.3 续跑弹窗

- 选 `from_step_index`（默认 = 首个 failed 步；下拉可选任意步）
- 可选 `model_override`（仅显示当前可选模型）
- 复选框「续跑前再压缩一次」（默认 false）
- 「续跑」按钮触发 §7 API，弹窗关闭，进度在 chat 面板流式展示

## 9. 测试

### 9.1 单测

| 用例 | 覆盖 |
|---|---|
| `_compressStepData` 关键列识别 | GROUP BY / 数值 / 类别列分类 |
| `_compressStepData` 极值点 top 5 | 同比环比绝对值排序 |
| `_classifyError` 各异常分类 | transient / permanent 全覆盖 |
| `_estimatePromptTokens` 估算精度 | ±20% 内 |
| `_shouldCompress` 阈值判定 | 0.7 倍临界 |

### 9.2 集成测试

| 场景 | 验证 |
|---|---|
| fake LLM 第 3 步抛 ConnectError | 自动重试 3 次后转 failed，run.status=failed |
| fake LLM 第 2 步 5xx | 自动重试 → 第 2 次成功 → 第 3 步成功 → run.status=succeeded |
| 续跑 from_step=2 | 重新加载前 1 步成功状态，第 2 步从 pending 重跑 |
| 模拟 ctx 临界（fake model.max=1000） | 触发压缩，验证 data_compressed 写入、data 保留 |
| 并发续跑同 run | 乐观锁防止双重续跑（409 Conflict） |

### 9.3 真机冒烟

- 真实 oMLX 模型 + B019 多步题
- 临时 kill oMLX 容器 → 验证自动重试 → 恢复后验证跑通
- 临时把某步 prompt 撑到 ctx 临界 → 验证压缩 + 续跑

## 10. 迁移 & 灰度

### 10.1 alembic 0114

新建：
- `multi_step_run` 表 + 索引
- `multi_step_step` 表 + 索引
- `multi_step_run` 加 `resume_count INT DEFAULT 0` 字段

### 10.2 Feature Flag

环境变量：`MULTI_STEP_PERSIST_ENABLED`（默认 `true`，新功能默认开）

行为：
- `true`：完整功能（落库 + 重试 + 压缩 + 续跑）
- `false`：退回现状（不落库、不重试、不压缩、/resume 端点 404）

### 10.3 兼容

- 不修改 `multi_step_step` 与 `multi_step_run` 之外的表 schema
- 不修改 `/api/v1/chat` 入参 / 出参形状
- 仅追加新端点 `/api/v1/chat/multi-step/{run_id}/resume`

### 10.4 数据保留

- 失败的 `partially_failed` run 保留 7 天
- 成功的 `succeeded` run 保留 30 天
- 定期清理由新 cron 任务执行（`app/jobs/cleanup_multi_step_runs.py`）

## 11. 失败场景表

| 场景 | 现象 | 自动处理 | 用户可见 |
|---|---|---|---|
| oMLX 第 2 步超时 | 1s/2s 自动重试，第 3 次仍败 | 转 manual | 「续跑」按钮亮起 |
| oMLX 全程不可用 | run 整次失败 | 无 | 「网络问题，请稍后重试或点击续跑」 |
| 第 3 步 prompt 撑爆 ctx | 自动压缩第 1、2 步 data | 压缩后重跑 | step 卡片显示「数据已压缩」徽章 |
| 压缩后仍超限（>95%） | 该步 skipped | 后续步继续 | run.status=partially_failed，UI 标「部分失败」 |
| Plan 校验失败（永久） | 该步 failed | 转 manual | step 卡片红 + 「配置/数据问题」提示 |
| 用户主动 kill 浏览器 | run.status=running 残留 | 下次续跑从当前步继续 | session 列表显示「未完成」徽章 |
| 网络瞬断（SSE 中断） | 前端断开连接，run 仍在跑 | run 完成但 SSE 丢弃 | 用户重连看到完整结果 |

## 12. 与已有功能的关系

| 已有 | 关系 |
|---|---|
| `StepSubquestionRewriter`（qwen-uplift 保留部分） | 子问题改写在本设计前发生；改写后的 `sub_question` 落 `multi_step_step.sub_question` |
| `ResearchSession`（0113） | `multi_step_run.session_id` 外键引用；多步 session 的 meta 仍由 ResearchSession 管 |
| `session_message`（已有） | 现有 user/assistant message 落库不变；本设计不复制其数据 |
| `_executeDataStep`（chat_multistep.py） | 本设计在此函数**前后插入落库 + 压缩 + 重试**；不改其业务逻辑 |
| LLM fallback 链 | 本设计复用 `pc.configs` 现有 fallback 顺序；不引入新 fallback |
| `query_pattern_router.py` | **已删除**（与本设计无关） |

## 13. 文件改动清单（概要）

| 文件 | 改动 |
|---|---|
| `backend/app/domain/multi_step_models.py` | 新建：`MultiStepRun`、`MultiStepStep` ORM 模型 |
| `backend/alembic/versions/0114_multi_step_persist.py` | 新建：迁移脚本 |
| `backend/app/services/multi_step_persistence.py` | 新建：step/run 落库 + 查询 |
| `backend/app/services/multi_step_compressor.py` | 新建：`_compressStepData` 实现 |
| `backend/app/services/multi_step_retry.py` | 新建：错误分类 + 重试曲线 |
| `backend/app/services/multi_step_resume.py` | 新建：续跑入口 |
| `backend/app/services/chat_multistep.py` | 改：在 `_executeDataStep` 前后插入落库 / 压缩 / 重试 |
| `backend/app/api/v1/chat.py` | 改：新增 `/multi-step/{run_id}/resume` 端点 |
| `backend/app/jobs/cleanup_multi_step_runs.py` | 新建：cron 清理脚本 |
| `backend/app/tests/unit/test_multi_step_compressor.py` | 新建 |
| `backend/app/tests/unit/test_multi_step_retry.py` | 新建 |
| `backend/app/tests/integration/test_multi_step_persist.py` | 新建 |
| `Harness/wiki/chat_multi_step_persistence.md` | 新建：架构 wiki |
| `Harness/changes/feat-multi-step-persist/summary.md` | 新建：change 记录 |
| `frontend/src/pages/...` | 改：续跑按钮 + 状态徽章 |

## 14. 风险与缓解

| 风险 | 缓解 |
|---|---|
| `data` JSONB 字段过大（万行 data）→ 写入慢 | 仅 `data_compressed` 进 JSONB；`data` 单独进 TOAST；超过 5MB 走外部对象存储（minio，已有） |
| 并发续跑同 run | `multi_step_run` 加 `version INT` 乐观锁；并发请求返 409 |
| 自动重试期间 token 双计 | retry 路径用同 `tokens_used` 累加，不发新 `_recordUsage` 调用 |
| 续跑期间 session.model_id 被改 | run 落库时存 `model_id` 快照；续跑用快照值；`model_override` 才覆盖 |
| 老 run 无 `multi_step_run` 记录 | 续跑端点返 404；前端隐藏续跑按钮 |

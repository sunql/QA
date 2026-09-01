# Change: Agent 批量调度（feat-agent-scheduler，Phase 7 G5）

> **SSOT（Single Source of Truth）**：每个特性的完整事实记录。
> 与 `summary.md §10` 交叉引用；覆盖决策、API、数据模型、测试、验收。

---

## 1. 概述

为 Agent Runtime 添加 **cron 定时调度**能力：
- 用户在 Agent 注册表中配置 cron 表达式（如「每日 09:00」）+ 自然语言输入参数
- 后台独立 worker 进程轮询 PG 表，到期触发 `AgentRuntimeService.run`
- 运行结果（status/answer/error/tokens/cost）写入 `agent_run_log`

## 2. 用户决策（已确认）

| 时间 | 决策点 | 选项 | 选择 | 理由 |
|------|--------|------|------|------|
| G5 规划 | 调度机制 | APScheduler / croniter+worker | **croniter + 独立 worker** | APScheduler 依赖进程生命周期；croniter 纯 Python 无额外进程绑定，更轻量 |

**技术约束**：worker 独立进程，不复用 FastAPI 进程（避免 uvicorn 信号处理冲突）。

## 3. 数据模型

### `agent_schedule`

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | BIGINT PK | |
| `agent_id` | BIGINT FK→agent_definition | ON DELETE CASCADE |
| `cron_expression` | VARCHAR(64) | 5-field（分 时 日 月 周）或 6-field（含秒） |
| `params` | JSONB | `params.input` 为传给 Agent 的自然语言 |
| `is_active` | BOOLEAN | 默认 true |
| `last_run_at` | TIMESTAMPTZ | 最近一次执行时间 |
| `next_run_at` | TIMESTAMPTZ | 下次执行时间（worker claim 目标） |
| `created_time/updated_time` | TIMESTAMPTZ | |

**索引**：`ix_agent_schedule_next_run (is_active, next_run_at)` — worker 轮询走索引扫描。

### `agent_run_log`

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | BIGINT PK | |
| `schedule_id` | BIGINT FK→agent_schedule | ON DELETE SET NULL（删除调度保留历史） |
| `agent_code` | VARCHAR(64) | 反查用 |
| `status` | VARCHAR(20) | `success` / `error` |
| `answer` | TEXT | 成功时 Agent 输出 |
| `error` | TEXT | 失败时错误信息（截断至 500 字符） |
| `tokens_used` | INTEGER | LLM token 消耗 |
| `cost` | NUMERIC(14,6) | 费用 |
| `actor` | VARCHAR(128) | `scheduler:{schedule_id}` |
| `started_at/finished_at` | TIMESTAMPTZ | |

**索引**：`ix_agent_run_log_schedule(schedule_id)`，`ix_agent_run_log_agent(agent_code, started_at)`。

## 4. API

挂载于 `/api/v1/agents`（与 agent_registry、agent_runtime 同前缀）：

| 方法 | 路径 | 说明 | ACL |
|------|------|------|-----|
| POST | `/{agent_code}/schedules` | 创建调度 | admin 或 owner |
| GET | `/{agent_code}/schedules` | 列表 | 任意登录用户 |
| PATCH | `/{agent_code}/schedules/{id}/toggle` | 启停 | admin 或 owner |
| DELETE | `/{agent_code}/schedules/{id}` | 删除 | admin 或 owner |
| GET | `/{agent_code}/schedules/logs` | 运行历史 | 任意登录用户 |

**写入**：cron 合法性校验 → `AclService.assertCanModify` → `next_run_at` 计算 → 落库。
**读**：仅校验 agent 存在性（owner-based ACL 不限制读）。

## 5. 核心逻辑

### 5.1 cron 解析

```python
AgentSchedulerService.computeNextRun(expression, from_time) → datetime
```
- `croniter.is_valid(expression)` 校验（5-field 和 6-field 含秒均支持）
- 非法表达式 → `ValidationError(422)`
- croniter 默认支持秒位（v6.2.4）；若应跑时间已过（late worker），从 `now` 计算下一次以保证 future

### 5.2 Worker claim（并发防护）

```sql
UPDATE agent_schedule
SET next_run_at = :new_next
WHERE id = :id AND next_run_at = :old_next AND is_active IS TRUE
-- rowcount == 1 → claim 成功，执行
-- rowcount == 0 → 已被其他 worker 处理，跳过
```

claim 成功后：`session.expire(schedule)` → `session.get(AgentSchedule, id)` 同步 ORM 状态（raw UPDATE 绕过了 identity-map）。

### 5.3 执行与日志

```python
log = await AgentRuntimeService.run(session, agent_code, input_text, actor=f"scheduler:{schedule_id}")
```
- 成功：status=success，answer=tokens=tokens_used，cost
- 失败：status=error，error=截断异常信息，tokens=0，cost=0
- **不抛异常**：失败写入 error 日志，next_run_at 已前移，避免 crash-loop

## 6. Worker 进程

**启动**：`uv run python -m app.workers.agent_scheduler_worker`
**优雅停机**：SIGTERM/SIGINT → `requestStop()` → 处理完当前批次退出
**轮询间隔**：60s，批次上限 50 条/轮

## 7. 验收标准

| # | 标准 | 状态 |
|---|------|------|
| 1 | cron 表达式合法性校验（5-field / 6-field）→ 422 | ✅ |
| 2 | 未知 agent → 404 | ✅ |
| 3 | 非 owner 非 admin 创建/启停/删除 → 403 | ✅ |
| 4 | owner 创建调度成功 → 201 + next_run_at 非空 | ✅ |
| 5 | 到期调度执行成功 → agent_run_log 落库（actor=scheduler:{id}） | ✅ |
| 6 | 执行失败 → status=error 优雅记录，不抛异常 | ✅ |
| 7 | next_run_at 前移，last_run_at 更新 | ✅ |
| 8 | next_run_at 前移后不再重复执行（claim 幂等） | ✅ |
| 9 | 停用 schedule → 不在 dueSchedules 结果中 | ✅ |
| 10 | 调度执行历史可查询 | ✅ |

## 8. 回归

| 测试集 | 结果 |
|--------|------|
| `test_agent_runtime_api.py` | 12 passed |
| `test_agent_scheduler_service.py` (unit) | 8 passed |
| `test_agent_scheduler_api.py` (integration) | 15 passed |

## 9. 关键决策记录

### 9.1 为什么需要 agent_run_log 表？

原 gap plan 只列了 `agent_schedule`。但 `AgentRuntimeService.run()` 不持久化任何数据（token 计量只在 API 层），验收要求「运行结果与 token 消耗写入 DB」。因此新增 `agent_run_log` 作为调度路径的持久化结果表。

### 9.2 为什么用 raw SQL UPDATE 而非 ORM？

claim 需要原子条件 UPDATE（`WHERE next_run_at = old AND is_active`）。SQLAlchemy ORM 无法表达这类「读后写」原子条件。用 raw UPDATE + `rowcount` 判断 claim 成败。

### 9.3 为什么从 `old_next` 而非 `now` 计算下一次？

**节奏保护（cadence preservation）**：从应跑时间（`old_next`）计算下一次，保证即使 worker 晚跑（如 CPU 争用），相邻两次执行的时间间隔仍符合 cron 语义。若从 `now` 算，worker 延迟会导致 cron 节奏漂移。

但若 `old_next < now`（已逾期），退化到从 `now` 计算（late worker 场景），避免 test 场景 `next_run_at` 仍为过去时间。

### 9.4 6-field 秒位支持

croniter 6.2.4 默认支持 6-field（含秒）：`秒 分 时 日 月 周`。这是 MVP 范围的意外收获——无需额外配置。

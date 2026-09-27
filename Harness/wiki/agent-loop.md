# L4 Agent Loop（Phase 6.4，纯 Python async while loop）

> ⚠️ **2026-09-27 重写**：本文档原基于 LangGraph StateGraph 实现假设撰写（路径、节点定义、Checkpointing、LangSmith tracing 等均为**虚构/设计文档遗留**），实际代码是**纯 Python async while loop**。
>
> **路径更正**：
> - Agent Loop 实现在 `app/services/agent_runtime_service.py:593`（不在 `app/services/agent_loop.py` ——该文件不存在）
> - SQL Guard 实现在 `app/infrastructure/business_db_pool.py`（不在 `app/infrastructure/security/sql_guard.py` ——该文件不存在）
> - L4 由 `IntentType.AGENT_RUN` 意图**单独触发**，非「L2 失败升级」

LangGraph 的设想被压测**否决**：

| 设想理由 | 实测否决 |
|---|---|
| Checkpointing：中途崩溃可从 checkpoint 恢复 | 实测：所有 handler 都是 async，StateGraph node 包装复杂；mock 难，测试覆盖低 |
| LangSmith tracing | 项目未启用 LangSmith；自建日志够用 |
| Deterministic termination | `while state["iterations"] < max_iterations` 足够确定 |

trade-off：去掉 LangGraph 后启动开销 -50ms，冷启动可忽略；换来的是简单可测。

## 调用入口

`chat_service._runL4AgentLoop`（`app/services/chat_service.py:892`）由 `IntentType.AGENT_RUN`
命中后调用（`chat_service.py:842`）；`_handleAgentRun`（`chat_service.py:886`）是分发点。

`_runL4AgentLoop` 调 `agent_runtime_service.run_agent_loop(...)` 拿 `AgentLoopResult`，
落到既有 `_saveQueryState` + `_recordUsage` 收尾链路。

## AgentLoopState / AgentLoopResult

两者均为 frozen dataclass（在 `app/services/agent_runtime_service.py`，**不是 `app/services/agent_loop.py`**）：

```python
# app/services/agent_runtime_service.py:234
@dataclass(frozen=True)
class AgentLoopResult:
    sql: str                          # 最终 SQL（失败时为空）
    success: bool                     # True ⇔ sql 非空且过 SQL Guard
    iterations: int                   # tool-call 轮数
    cost_so_far_usd: float
    tool_call_history: tuple[ToolCall, ...]
    error: str | None
    terminated_reason: str            # "answered" | "max_iterations" | "cost_cap" | "error"
    prompt_tokens: int                # H2 修复后增（H1/H2 批）
    completion_tokens: int            # 同上
    cost_cap_hit: bool                # H2 修复后增
```

**实现要点**：
- State 在循环内是**普通 dict**（不是 dataclass，循环内频繁就地更新；terminate 时才 freeze 为 `AgentLoopResult`）；
- 全程 single-session + `try/except` 在 while 内收敛异常为 `terminated_reason="error"`，不让异常逃逸（否则已花 token 凭空消失，H2 批实测抓出）；
- 终止条件有 4 个：`answered` / `max_iterations` / `cost_cap` / `error`，仅前一个算 `success=True`。

## 5 NL2SQL Tools

5 个 tool 全部实现在 `app/services/agent_tools_nl2sql.py`：

| Tool | 作用 | 何时用 |
|---|---|---|
| `list_tables` | 列当前数据源所有表（table_name / table_type / remark） | LLM 不知道有哪些表 → JOIN 前发现 |
| `describe_table` | 取单表 columns + nullable + PK + FK refs | LLM 要写 WHERE/JOIN 前要列名 |
| `sample_rows` | 抽样 ≤5 行（带 SQL Guard） | LLM 不确定列值形态 |
| `execute_sql` | 执行 SELECT，**过 SQL Guard 后**由 `datasource_service.execute_readonly` 跑 | LLM 写完 SQL 要先验证 |
| `list_joins` | 取两表间 JOIN 边（`ontology_property` FK 图 + information_schema） | LLM 找到两张表但不知怎么连 |

每个 tool 都有 `cost_usd` 字段（按调用的 LLM 估计成本或固定常数），累加到 `cost_so_far_usd`。
`execute_sql` 调 SQL Guard 失败不抛异常，转写 `{"error": "...", "cost_usd": 0.0}` 让 LLM 自我修复。

## BaseLlmClient.complete_with_tools

抽象方法在 `app/infrastructure/llm/base.py`，所有 LLM 客户端必须实现以支持 tool calling。
工具调用协议统一为 OpenAI function_call 形态（Ollama 用 JSON mode 结构化输出兜底）。

## 主循环（伪代码）

```python
# app/services/agent_runtime_service.py:627
state = _initLoopState(question)
while state["iterations"] < max_iterations:
    state["iterations"] += 1
    try:
        response = await client.complete_with_tools(messages, tools, model_config_id)
    except Exception as exc:
        state["terminated_reason"] = "error"
        state["error"] = str(exc)
        break
    # 累加本轮 token（成功路径，H2 修复）
    pt, ct = _usage_tokens(response.usage)
    state["prompt_tokens"] += pt
    state["completion_tokens"] += ct
    state["cost_so_far_usd"] += _cost_for_usage(...)
    # 终止判定：answered / cost_cap / max_iterations
    if response.has_final_sql and not response.tool_calls:
        state["terminated_reason"] = "answered"
        state["sql"] = _extract_final_sql(response)
        break
    if state["cost_so_far_usd"] >= max_cost_usd:
        state["terminated_reason"] = "cost_cap"
        break
    # 执行 tool → 把结果拼回 messages → 下一轮
    for tc in response.tool_calls:
        result = await _execute_tool(tc)
        messages.append(_tool_message(tc, result))
    # max_iterations 兜底（防止 LLM 最后一次响应伴生 tool_calls 被视为 continue）
# freeze
return AgentLoopResult(...)
```

> ⚠️ **`max_iterations` 不是「大点好」**：实测 LLM 反复 `describe_table` 是**prompt 没设预算**的问题（`agent_runtime_service.py:275` 注释），不是上限太小。改 `_L4_SYSTEM_PROMPT` 加探索≤1 轮 + 一次 CTE 引导，`max_iterations` 从 3 提到 5 即可（H2 批前的 L4 iteration budget 修复）。

## Cost Cap 机制

```python
MAX_COST_USD = 5.0  # env MAX_AGENT_LOOP_COST_USD 可覆盖
state["cost_so_far_usd"] += _cost_for_usage(...)
if state["cost_so_far_usd"] >= MAX_COST_USD:
    state["terminated_reason"] = "cost_cap"
    break
```

耗尽时返回当时累计的 best-effort SQL（可能空/无效 → 落 "cannot answer"）。

## SQL Guard 双层检查

虽然 SQL Guard 在 `app/infrastructure/business_db_pool.py`（不在 `security/sql_guard.py`），
但 L4 的 SQL Guard 检查**双层**（这条设计仍成立）：

1. **Tool handler 层**（`agent_tools_nl2sql.execute_sql`）：执行前 `sql_guard.check(sql)`，
   `SqlSafetyError` 转 `{"error": "...", "cost_usd": 0.0}` 给 LLM 自愈；
2. **Executor 层**（`datasource_service.execute_readonly`）：连接池层加 `fetchmany(5000)` 行数兜底
   + 客户端 `asyncio.wait_for(queryTimeoutSeconds)`（**没有**连接级 / 服务端 `statement_timeout`，
   也没有库侧只读账号 —— 库侧只读兜底见提案
   `Harness/changes/2026-09-26-sql-guard-db-side-readonly-proposal.md`，未排期）。

```python
# 在 execute_sql tool 内
try:
    sql_guard.check(sql)
except SqlSafetyError as exc:
    return {"error": f"SQL Guard rejected: {exc}", "cost_usd": 0.0}
return await datasource_service.execute_readonly(datasource_id, sql)
```

双层保证 tool handler 被绕过时畸形 SQL 仍被截在连接池层。

## Tool Selection Prompt（`_L4_SYSTEM_PROMPT`）

```text
你是一个 SQL 专家 agent。可用 5 个工具：list_tables / describe_table / sample_rows /
execute_sql / list_joins。目标：生成能回答用户问题的 SELECT SQL。

策略：
1. 用 list_tables 发现可用表
2. 用 describe_table 理解列 schema
3. 用 list_joins 找外键关系
4. 用 sample_rows 验证数据模式
5. 用 execute_sql 在交付前验证 SQL

每次 tool 调用后分析结果再决策。拿到有效 execute_sql 结果时直接回答 SQL。
execute_sql 不要调超过 3 次 —— 先打磨 SQL 再重试。
预算：{cost_so_far_usd:.4f} USD 已用 / {max_cost_usd} USD 上限。
```

> 关键预算提示（[L4 iteration budget]）：**explore ≤ 1 轮**，避免「反复 describe_table
> 耗尽 max_iterations」陷阱（实测：曾因 LLM 反复 describe_table → answer_text=None → 整步失败）。

## 错误恢复

| 错误 | 恢复 |
|---|---|
| `execute_sql` 返 0 行 | 迭代：精修 WHERE / JOIN 条件 |
| `execute_sql` 抛 timeout | 计入 iteration；≥ 3 次 timeout → end with best effort |
| LLM rate limit | backoff 2s, retry 1 次；仍失败 → end |
| SQL Guard 拒绝 | 跳过 `execute_sql` tool 结果，让 LLM 重新生成 |
| Tool 在该数据源不可用 | 返空结果，LLM 继续 |

## 关联文件（实际路径）

| 组件 | 实际路径 |
|---|---|
| Agent Loop 实现 | `app/services/agent_runtime_service.py:593`（`run_agent_loop`） |
| AgentLoopResult | `app/services/agent_runtime_service.py:234` |
| Agent tool 集合 | `app/services/agent_tools_nl2sql.py`（5 tool） |
| BaseLlmClient | `app/infrastructure/llm/base.py` |
| SQL Guard | `app/infrastructure/business_db_pool.py`（`_assert_read_only` / `_assertNoHiddenWrites`） |
| 数据源只读执行 | `app/services/datasource_service.py:execute_readonly` |
| L4 入口 | `app/services/chat_service.py:842 _runL4AgentLoop` |
| 路由层上下文 | `Harness/wiki/nl2sql-engine.md`（4 层路由章节） |

## 与旧版的差异（按 §2.3 H2 / §15 残差）

- ❌ LangGraph StateGraph（虚构） → ✅ 纯 Python async while loop
- ❌ `app/services/agent_loop.py`（不存在） → ✅ `app/services/agent_runtime_service.py:593`
- ❌ `app/infrastructure/security/sql_guard.py`（不存在） → ✅ `app/infrastructure/business_db_pool.py`
- ❌ Checkpointing / LangSmith tracing（虚构能力） → 无对应实现
- ❌ 连接级 `statement_timeout`（不存在） → 仅客户端 `asyncio.wait_for`
- ✅ AgentLoopResult 加 `prompt_tokens` / `completion_tokens` / `cost_cap_hit` / `terminated_reason` 字段（H2 批修复计量盲区）
- ✅ while 体 `try/except` 收敛异常为 `terminated_reason="error"`，保留已花 token（H2 批防丢账）
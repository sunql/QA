# L4 Agent Loop (Phase 5)

LangGraph `StateGraph` agent that iteratively calls 5 NL2SQL tools to answer complex multi-join questions that L2/L3 cannot resolve.

## Why LangGraph (Deviation from Pure Async While Loop)

The first implementation used a bare `async while` loop with manual state dict management. LangGraph was adopted because:

1. **Checkpointing**: `agent_loop.py` State is persisted between tool calls — if the loop crashes mid-execution (OOM, network timeout), LangGraph can resume from the last checkpoint rather than restarting the entire question.
2. **Tracing**: LangGraph's built-in `LangSmith` integration provides per-iteration tool-call traces without manual instrumentation.
3. **Deterministic termination**: `MaxIterations` and `MaxCost` conditions are declared as graph edges, not scattered `while` guard clauses.

The trade-off: LangGraph adds ~50ms cold-start overhead per question. For L4-only questions (complex multi-join), this overhead is negligible vs LLM tool-call latency.

## AgentLoopState

```python
# app/services/agent_loop.py
from dataclasses import dataclass, field
from typing import FrozenSet

@dataclass(frozen=True)
class AgentLoopState:
    question: str
    generated_sql: str | None = None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)  # immutable list
    iterations: int = 0
    cost_so_far_usd: float = 0.0
    error: str | None = None
    datasource_id: int | None = None

@dataclass(frozen=True)
class ToolCall:
    tool_name: str          # list_tables | describe_table | sample_rows | execute_sql | list_joins
    args: dict              # tool-specific arguments
    result: str | None = None
    cost_usd: float = 0.0
```

State is **frozen** (immutable) — each transition returns a new state instance. This ensures replayability and thread safety.

## 5 NL2SQL Tools

### list_tables

Lists available tables in the current data source.

```python
def list_tables(datasource_id: int) -> list[TableInfo]:
    """Returns table_name, table_type, remark for all tables in the schema."""
    # Executes: SELECT table_name, table_type FROM information_schema.tables
    # Filters by datasource's connection_url schema
```

**When to use**: LLM needs to discover what tables exist before writing a JOIN.

### describe_table

Returns column names, types, nullable, primary key, foreign key refs for a specific table.

```python
def describe_table(datasource_id: int, table_name: str) -> TableSchema:
    """Returns columns: name, data_type, is_nullable, is_primary_key, foreign_key_ref."""
    # SELECT column_name, data_type, is_nullable
    #   FROM information_schema.columns
    #   JOIN pgConstraint meta on column level
```

**When to use**: LLM needs to know column names before generating WHERE or JOIN ON clauses.

### sample_rows

Returns up to 5 sample rows from a table (raw, no aggregation). Used to understand data distribution and value patterns.

```python
def sample_rows(datasource_id: int, table_name: str, limit: int = 5) -> list[dict]:
    # SELECT * FROM table_name LIMIT 5 (with SQL Guard)
```

**When to use**: LLM is unsure about column values (e.g., "what does STATUS look like?").

### execute_sql

Executes a validated `SELECT` SQL statement against the data source. The SQL must pass SQL Guard before execution.

```python
def execute_sql(datasource_id: int, sql: str) -> ExecutionResult:
    """Returns columns, rows, row_count, execution_time_ms."""
    # SQL Guard check first
    # Then: SELECT * FROM (...sql...) LIMIT 5000
```

**When to use**: The LLM has generated a SQL candidate and wants to verify it returns correct results.

### list_joins

Returns known JOIN relationships between tables (from `ontology_property` foreign key graph or `information_schema`).

```python
def list_joins(datasource_id: int, from_table: str) -> list[JoinEdge]:
    """Returns list of (from_table, to_table, via_column, join_type)."""
```

**When to use**: LLM needs to discover how to JOIN two tables it found via `list_tables`.

## BaseLlmClient.complete_with_tools

Abstract method all LLM clients must implement to support tool calling:

```python
# app/infrastructure/llm/base.py
from abc import ABC, abstractmethod

class BaseLlmClient(ABC):
    @abstractmethod
    async def complete_with_tools(
        self,
        messages: list[LlmMessage],
        tools: list[ToolDefinition],
        model_config_id: int,
    ) -> LlmToolCallResponse:
        """
        Sends a messages array + tool definitions to the LLM.
        Returns the LLM's tool call choice(s).
        Raises RateLimitError, LlmError on failure.
        """
        ...
```

Tool calling is implemented for: OpenAI (`function_call` tool type), Ollama (JSON mode structured output). Azure OpenAI uses `function_call` as well.

## AgentLoopResult

```python
# app/services/agent_loop.py
@dataclass(frozen=True)
class AgentLoopResult:
    sql: str                          # final generated SQL (or "" if failed)
    success: bool                     # True if sql is non-empty and passed SQL Guard
    iterations: int                   # number of tool-call iterations
    cost_so_far_usd: float
    tool_call_history: tuple[ToolCall, ...]
    error: str | None
```

## Cost Cap Mechanism

```python
# inside LangGraph node: "llm_decide"
MAX_COST_USD = 5.0  # configurable via MAX_AGENT_LOOP_COST_USD env var

def should_continue(state: AgentLoopState) -> str:
    if state.error and "cost_limit_exceeded" in state.error:
        return "end"
    if state.cost_so_far_usd >= MAX_COST_USD:
        return "end"
    if state.iterations >= 10:  # MAX_ITERATIONS
        return "end"
    if state.generated_sql and state.tool_calls:
        last = state.tool_calls[-1]
        if last.tool_name == "execute_sql" and last.result is not None:
            return "end"
    return "continue"
```

When cost cap is hit, the loop returns the best-effort SQL accumulated so far (may be empty/invalid → surfaces "cannot answer").

## SQL Guard Double-Check Pattern

SQL Guard is applied at **two layers**:

1. **Handler layer** (`agent_loop.py:execute_sql tool`): before executing, `sql_guard.check(sql)` raises `SecurityError` if invalid.
2. **Executor layer** (`datasource_service.py:execute_readonly`): connection-level `statement_timeout` + `fetchmany(5000)` as final backstop.

```python
# In execute_sql tool
try:
    sql_guard.check(sql)
except SecurityError as e:
    return {"error": f"SQL Guard rejected: {e}", "cost_usd": 0.0}
result = await datasource_service.execute_readonly(datasource_id, sql)
```

This double-check ensures a malformed SQL cannot escape even if the tool handler's guard is bypassed.

## LangGraph Graph Definition

```python
from langgraph.graph import StateGraph, END

builder = StateGraph(AgentLoopState)
builder.add_node("llm_decide", llm_decide_node)      # LLM picks next tool
builder.add_node("list_tables", list_tables_node)
builder.add_node("describe_table", describe_table_node)
builder.add_node("sample_rows", sample_rows_node)
builder.add_node("execute_sql", execute_sql_node)
builder.add_node("list_joins", list_joins_node)

builder.set_entry_point("llm_decide")
builder.add_conditional_edges("llm_decide", should_continue, {
    "continue": "llm_decide",
    "end": END,
})
# Tool nodes all route back to llm_decide after executing
for tool_node in ["list_tables", "describe_table", "sample_rows", "execute_sql", "list_joins"]:
    builder.add_edge(tool_node, "llm_decide")

graph = builder.compile()
```

## Tool Selection Prompt (llm_decide_node)

```python
SYSTEM_PROMPT = """You are a SQL expert agent.
You have access to 5 tools: list_tables, describe_table, sample_rows, execute_sql, list_joins.
Your goal: generate a correct SELECT SQL that answers the user's question.

Strategy:
1. Use list_tables to discover available tables
2. Use describe_table to understand column schemas
3. Use list_joins to find foreign key relationships between tables
4. Use sample_rows to verify data patterns
5. Use execute_sql to validate your SQL before reporting

After each tool call, analyze the result and decide the next tool.
When you have a valid SQL result from execute_sql, respond with the SQL.
Do not call execute_sql more than 3 times — refine your SQL before retrying.
Cost budget: {cost_so_far_usd:.4f} USD used / {max_cost_usd} USD max.
"""
```

## Error Handling

| Error | Recovery |
|-------|----------|
| `execute_sql` returns 0 rows | Iterate: refine WHERE or JOIN condition |
| `execute_sql` raises timeout | Count toward iteration; if ≥ 3 timeouts → end with best effort |
| LLM rate limit | Backoff 2s, retry once; if still fails → end |
| SQL Guard rejection | Log and skip `execute_sql` tool result; ask LLM to reformulate |
| Tool not available for datasource | Return empty result; LLM proceeds without it |

## Related Files

| File | Purpose |
|------|---------|
| `app/services/agent_loop.py` | StateGraph definition, node implementations, `AgentLoopResult` |
| `app/infrastructure/llm/base.py` | `BaseLlmClient.complete_with_tools` abstract method |
| `app/infrastructure/security/sql_guard.py` | SQL Guard validation |
| `app/services/datasource_service.py` | `execute_readonly` + connection pool |
| `app/services/chat_service.py:_runL4AgentLoop` | L4 entry point call site |
| `Harness/wiki/nl2sql-engine.md` | 4-layer routing context |

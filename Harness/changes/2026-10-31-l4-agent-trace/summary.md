# L4 执行轨迹卡（Agent Trace Card）

- **日期**：2026-10-31
- **分支**：`feat/l4-agent-trace`（base `origin/main` = `df9b5b3a`）
- **范围**：L4 Agent Loop 的工具调用轨迹（list_tables / describe_table / sample_rows / list_joins / execute_sql）合成为一张「执行轨迹」卡，事后展示 + 持久化回放
- **不在范围**：执行过程中的实时进度（P1，见文末待办）

## 1. 背景与现状

L4 目前是**黑盒阻塞调用**：

- `run_agent_loop` 跑完全部迭代才返回；SSE 只发 `meta → sql → token → done`（`chat_stream_entry.py` preamble 分支）。
- `AgentLoopResult` 只保留 `tool_calls_made: list[str]`（工具名），参数、返回摘要、耗时、成败都没有数据结构承载；raw/sent 对比只写 stdout 日志。
- L4 落库只有 answer + final_sql，`query_plan` / `steps_snapshot` 两列恒 NULL。
- 前端 `MessageItem` 现有固定卡序：分析计划 → 查询计划 → SQL；L4 无任何过程卡。

## 2. 决策（用户确认）

1. **先做事后卡**；实时轨迹（SSE 增量进度）列为 P1 待办，不在本批。
2. **持久化用新列** `session_message.agent_trace` JSONB（迁移 0139），不复用 `steps_snapshot`（避免与「步骤=可续跑单元」语义纠缠）。
3. 卡片**默认收起**，标题一行摘要。
4. 只展示 `answered` / `max_iterations` 的轨迹；`error` / 降级到 L2 不展示。

## 3. 数据契约

`agent_trace` 快照（camelCase，与 `steps_snapshot` 同风格）：

```jsonc
{
  "kind": "l4_agent_trace",
  "status": "answered",          // answered | max_iterations
  "iterations": 3,
  "totalMs": 4210,
  "stepCount": 3,
  "steps": [
    { "index": 0, "iteration": 1, "tool": "list_tables",
      "status": "ok", "elapsedMs": 35,
      "args": {}, "resultSummary": { "tables": 12 } },
    { "index": 1, "iteration": 1, "tool": "describe_table",
      "status": "ok", "elapsedMs": 12,
      "args": { "table": "DWD_GOODS_RECEIPT_DTL" },
      "resultSummary": { "columns": 23 } },
    { "index": 2, "iteration": 2, "tool": "execute_sql",
      "status": "ok", "elapsedMs": 240,
      "args": { "sqlPreview": "SELECT ... FROM ..." },
      "resultSummary": { "rows": 286, "truncated": true } }
  ]
}
```

约束：

- **永不落** `rows`、完整 tool content（含 `<agent_tool_result>` 包装）、完整 SQL（完整 SQL 已有 `final_sql` 通道）。
- `sqlPreview` 归一空白后截断到 120 字符；错误文本截断到 200 字符。
- 快照最多 50 步，超出截断并在顶层写 `truncated: true`。
- 落库前过 `jsonSafe`。
- 所有字段由确定性模板拼装，**不额外调用 LLM**。

### 工具摘要规则

| 工具 | args 摘要 | resultSummary |
|---|---|---|
| list_tables | `{}` | `{tables: N}` |
| describe_table | `{table}` | `{columns: N}` |
| sample_rows | `{table}` | `{rows: N}` |
| list_joins | `{tables: [...]}`（最多 5 个） | `{joins: N}` |
| execute_sql | `{sqlPreview}` | `{rows: row_count, truncated?}` |

失败步骤：`status="error"`，`error` 为截断后的错误码/详情；不记录工具结果原文。

## 4. 实现落点

### 后端

- 新模块 `backend/app/services/l4_trace.py`：纯函数（参数摘要 / 结果摘要 / 步骤构造 / 快照构造 / 上限与截断）。
- `agent_runtime_service.py`：
  - `_dispatchSingleTool(...)` 增加可选 `trace_steps: list[dict] | None` 出口，在工具执行完成后追加一步（`time.monotonic()` 计时，`iteration` 由调用方传入）。
  - `_executePendingToolCalls` 持有 `state["trace_steps"]`（tuple，追加用拼接）。
  - `run_agent_loop` 记录总耗时；结束时 `buildTrace(...)` 写入 `AgentLoopResult.agent_trace`。
  - `AgentLoopResult` 新增 `agent_trace: dict | None = None`（尾部默认值，既有构造点不破坏）。
- `chat_l4.py`：`_buildL4ChatResponse` 把 trace 传入 `ChatResponse.agentTrace` 与 `_storeSessionMessages(agent_trace=...)`。
- `domain/schemas.py`：`ChatResponse.agent_trace`、`ChatMessageRead.agent_trace`。
- `chat_stream_output.py`：`PreambleResult.agent_trace`。
- `chat_stream_entry.py`：preamble 命中 L4 时，在 `token` 帧之前发 `agent_trace` 事件。
- `stream_events.py`：`EVENT_AGENT_TRACE = "agent_trace"`。
- `chat_context._storeSessionMessages`：新增 `agent_trace` 参数（落库前 `jsonSafe`）。
- `session_history_service.py`：SELECT + 映射 `agent_trace`。
- `domain/models.py` + 迁移 `0139_session_message_agent_trace.py`。
- `chat_helpers.py` 无需改（trace 不走 steps_snapshot）。

### 前端

- `types/chat.ts`：`AgentTrace` / `AgentTraceStep` 类型 + `ChatMessage.agentTrace`。
- `api/chat.ts`：`ChatMessageRead.agentTrace`、`StreamHandlers.onAgentTrace`、`case "agent_trace"`（严格收窄）。
- `utils/agentTrace.ts`：`asAgentTrace(unknown)` 白名单收窄（工具名、状态、步数上限、字段类型）。
- `stores/chatStore.ts`：`onAgentTrace` 增量 patch；非流式响应映射；`toChatMessage` 映射。
- 组件 `components/chat/AgentTraceCard.tsx`：默认收起，摘要一行；展开按轮分组展示步骤；未知工具名回退英文原名。
- `MessageItem.tsx`：卡序改为 **执行轨迹 → 分析计划 → 查询计划 → SQL**（L4 只有轨迹，不会与实际路径冲突；轨迹必须早于 SQL）。
- i18n（zh-CN / en-US）：`agentTrace.*` 键。

## 5. 测试计划（TDD）

后端单测（`backend/app/tests/unit/test_l4_agent_trace.py`）：

1. 五种工具的 args/result 摘要与错误状态；
2. `sqlPreview` 归一 + 截断；
3. 错误文本截断、不泄漏 `rows` / tool content；
4. 50 步上限 + `truncated`；
5. `_dispatchSingleTool` 追加轨迹（fake executor）；
6. `run_agent_loop` 的 `AgentLoopResult.agent_trace` 存在，`max_iterations` 也有。

后端集成（现有 `test_chat_stream_l1_l4.py` 扩展）：

7. L4 SSE 在 `token` 前有 `agent_trace` 帧，`stepCount >= 1`；
8. 历史回放 API 返回 `agentTrace`（落库后可读）。

前端单测：

9. `asAgentTrace` 收窄：合法快照通过，缺字段/未知工具/超步数被裁剪；
10. `AgentTraceCard`：摘要、展开、失败步骤、未知工具回退；
11. `MessageItem` 卡序：轨迹在 SQL 之前（`compareDocumentPosition`，沿用 PR #77 断言法）。

## 6. 风险

| 风险 | 对策 |
|---|---|
| 不可信内容进 UI | 快照只存摘要；前端纯文本渲染 |
| Decimal/复杂类型炸 JSONB | 落库前 `jsonSafe` |
| 同轮多工具无因果顺序 | 按 `iteration` 分组展示，不声明因果 |
| 快照膨胀 | 50 步上限 + 顶层 `truncated` |
| L4 降级 L2 后遗留孤儿卡 | 只在 `answered` / `max_iterations` 产出 trace；降级路径不发送 |
| 既有 SSE 断言被破坏 | 只新增帧与字段，不改既有事件 |

## 7. 实施与验证（2026-10-31 完成）

P0（事后卡）全部落地，文件清单与 §4 一致。验证证据：

| 范围 | 命令/套件 | 结果 |
|---|---|---|
| 后端新单测 | `app/tests/unit/test_l4_agent_trace.py` + `test_agent_runtime_service.py` + `test_l4_token_profile.py` + `test_agent_tools_nl2sql.py` | 115 passed |
| 后端流式帧 | `app/tests/integration/test_chat_stream_l1_l4.py` 新增 2 例（`agent_trace` 帧在 token 前 / 无轨迹不发帧） | passed |
| 后端回放 | `test_chat_history_api.py` 新增 2 例（回放带 `agentTrace` / 存量行 None） | 36 passed（含同行既有用例） |
| 前端收窄 | `src/utils/agentTrace.test.ts`（12 例） | passed |
| 前端卡片 | `src/tests/AgentTraceCard.test.tsx`（5 例） | passed |
| 前端链路 | `chatApi` / `chatStore`（流式帧、非流式、历史回放）/ `MessageItem`（卡序） | 176 passed |
| 类型 | `npx tsc --noEmit` | 0 error |
| 后端 lint | `ruff check` 对 11 个改动文件逐一比对 HEAD | 净新增 0 |

终止路径闸门：`run_agent_loop` 只在 `answered` / `max_iterations` 且确有工具步骤时构造快照
（`_TRACEABLE_TERMINATIONS`）；`error` / `cost_cap` 一律 `agent_trace=None`，由
`test_error_termination_produces_no_trace` 钉住。

### 已知红（均为存量，与本批无关）

取证方式：新建隔离测试库 `qa_metadata_test_base`，在**主仓 main（`df9b5b3a`）代码**上复跑同一批用例，得到完全相同的失败集合，随后已删除该库。

| 失败 | 根因 |
|---|---|
| `test_chat_stream_l1_l4.py` 2 errors、`test_stream_meta_single_frame.py` 2 errors | fixture 建 `business_object` 未填 `header_class_id` / `primary_domain_id`，迁移 0133 收紧 NOT NULL 后必然失败 |
| `test_chat_service_stream.py` 2 failed | 断言错误文案含「中断」，现被 `sanitizeErrorMessage` 归一为通用文案 |
| `test_check_schema_drift.py::test_healthy_db_has_no_column_or_index_drift` 1 failed | 测试库存在 ORM 未声明的历史列/表（`knowledge_claim.confidence_level` 等） |

## 8. 部署与回滚

- 迁移 `0139` 只 `add_column`，可空，无需回填；`downgrade` 直接删列。
- 发版后 `session_message` 多一列 NULL，旧版本后端读新库不受影响（列不被 SELECT）。
- 前端独立发布：老后端不认 `agent_trace` 帧时卡片自然不出现（收窄器对未知返回 null）。

## 9. P1 待办（实时轨迹）

- `run_agent_loop` 改造为可 emit（`asyncio.Queue` + task 或 async generator），在每步工具完成时发 `agent_trace_step`。
- 前端增量 patch，执行中显示进行中的步骤。
- 降级/断连时发 `agent_trace_discard`，避免孤儿卡。
- 触发条件：P0 上线后观测到 L4 平均步数/耗时确实需要过程反馈再立项。

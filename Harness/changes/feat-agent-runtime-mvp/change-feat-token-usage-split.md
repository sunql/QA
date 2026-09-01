# 变更：feat-token-usage-split

- **日期**：2026-09-01
- **阶段**：Phase 7 G2（Agent Runtime 遗留缺口）
- **类型**：功能补强（数据契约 / Token 审计）
- **提交**：`feat: token usage prompt/completion split`

---

## 1. 问题

`session_token_usage` 表已经存在 `prompt_tokens` / `completion_tokens` / `total_tokens` 三列，但运行时链路只透传了 `tokens_used`：

- `SupplierRiskService` 读取 LLM 响应后没有把 `promptTokens` / `completionTokens` 返回给调用方；
- `AgentRunRead` / `SupplierRiskRead` 缺少 prompt/completion 字段；
- `ToolResult` 只携带 `tokens_used`；
- `ChatService._recordDirectUsage` 只写 `tokens_used`，prompt/completion 默认落 0；
- 结果：audit 表里的 prompt/completion 拆分永远为 0，无法做输入/输出成本分析，也无法验证 MEDIUM#2 审计完整性。

`feat-agent-runtime-mvp` §10 明确列为 LOW#5 遗留缺口。

## 2. 设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 计量来源 | LLM 响应的 `promptTokens` / `completionTokens` | 与 `BaseLlmClient` / `LlmResponse` 契约一致 |
| 兼容层 | 同时兼容 camelCase 与 snake_case 属性 | 既有测试 fake 使用 snake_case，真实客户端使用 camelCase |
| DTO 扩展 | `AgentRunRead` / `SupplierRiskRead` 增加 `prompt_tokens` / `completion_tokens` | 保持 `tokens_used = prompt + completion`，不破坏前端 |
| 审计写入 | `_recordDirectUsage` 接收 prompt/completion 并透传给 `TokenUsageService.recordUsage` | 复用已有 `totalTokens = promptTokens + completionTokens` 逻辑 |
| 零值保护 | `tokens_used <= 0` 时直接 return | 保持现有闲聊/无 LLM 调用不写入审计的行为 |

## 3. 数据模型变更

无 DB 迁移、无 schema 变更。`SessionTokenUsage.prompt_tokens` / `completion_tokens` / `total_tokens` 列在 Phase 6.1 已建。

## 4. 关键代码

### 后端修改

- `app/domain/schemas.py`：
  - `AgentRunRead` 增加 `prompt_tokens: int = 0`、`completion_tokens: int = 0`；
  - `SupplierRiskRead` 增加 `prompt_tokens: int = 0`、`completion_tokens: int = 0`。
- `app/services/supplier_risk_service.py`：
  - `_generateRiskPoints` 返回 6 元组 `(risk_points, source, prompt_tokens, completion_tokens, cost, model_name)`；
  - 真实 LLM 响应读取 `promptTokens` / `completionTokens`（camelCase）并回退到 `prompt_tokens` / `completion_tokens`（snake_case）；
  - `assess` 计算 `tokens = prompt_tokens + completion_tokens` 并填充 `SupplierRiskRead` 全部计量字段。
- `app/services/agent_tools.py`：
  - `ToolResult` 增加 `prompt_tokens: int = 0`、`completion_tokens: int = 0`；
  - `_supplierRiskHandler` 把 `SupplierRiskRead` 的 prompt/completion 透传到 `ToolResult`。
- `app/services/agent_runtime_service.py`：
  - 构造 `AgentRunRead` 时从 `ToolResult` 透传 `prompt_tokens` / `completion_tokens`。
- `app/services/chat_service.py`：
  - `_recordDirectUsage` 签名扩展，新增 `prompt_tokens: int | None = None`、`completion_tokens: int | None = None`；
    - 未拆分来源（None）回退「全量计 prompt」，绝不让拆分量静默丢 0（code-reviewer MEDIUM 修复）；
  - `_handleSupplierRisk` 把 `SupplierRiskRead.prompt_tokens` / `completion_tokens` 传入 `_recordDirectUsage`；
  - `_handleAgentRun` 把 `AgentRunRead.prompt_tokens` / `completion_tokens` 传入 `_recordDirectUsage`。

### 前端修改

- `frontend/src/types/agentRuntime.ts`：`AgentRunRead` 增加 `promptTokens` / `completionTokens`。
- `frontend/src/types/supplierRisk.ts`：`SupplierRiskRead` 增加 `promptTokens` / `completionTokens`。
- `frontend/src/components/chat/AgentResponseCard.tsx`：底部 Token 标签显示拆分（`Tokens: N (P x / C y)`）。
- `frontend/src/components/chat/SupplierRiskCard.tsx`：同上。
- 测试 fixture 同步补齐 `promptTokens` / `completionTokens`（`AgentResponseCard` / `AgentRuntimePage` / `MessageItem`）。

### 测试修改

- `app/tests/integration/test_chat_agent_run.py`：
  - `test_chat_agent_run_stream_routes_agent_run_and_audits_tokens` 增加断言：
    - `rows[0].prompt_tokens == 1`
    - `rows[0].completion_tokens == 1`
    - `rows[0].total_tokens == 2`
- `app/tests/integration/test_chat_api.py`：
  - `test_full_pipeline_records_usage_rows` 增加断言：
    - `nl2sql` 用途 prompt=20 / completion=10 / total=30（计划 + SQL 两次调用）；
    - `answer` / `chart` 用途 prompt=10 / completion=5 / total=15。

## 5. 测试

| 层 | 文件 | 结果 |
|---|---|---|
| 集成 | `test_chat_agent_run.py::test_chat_agent_run_stream_routes_agent_run_and_audits_tokens` | passed |
| 集成 | `test_chat_api.py::TestChatApi::test_full_pipeline_records_usage_rows` | passed |
| 集成 | `test_chat_agent_run.py` + `test_chat_api.py` + `test_token_usage_service.py` | 33 passed |
| 全量后端 | `app/tests/ --cov=app` | 1861 passed, 1 skipped，覆盖率 93.22% |
| 前端 | `tsc --noEmit` | 0 errors |
| 前端 | vitest（`AgentResponseCard` / `AgentRuntimePage` / `MessageItem`） | 29 passed |

## 6. 安全审查

- 无新增用户输入处理；
- 无新增外部调用；
- 仅扩展既有 DTO 字段并透传 LLM 响应中已有的计量数据；
- 无密钥、无 SQL 拼接。

## 7. 验证

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest \
    app/tests/integration/test_chat_agent_run.py::test_chat_agent_run_stream_routes_agent_run_and_audits_tokens \
    app/tests/integration/test_chat_api.py::TestChatApi::test_full_pipeline_records_usage_rows -v
```

期望：2 passed。

## 8. 关键文件清单

- `backend/app/domain/schemas.py`
- `backend/app/services/supplier_risk_service.py`
- `backend/app/services/agent_tools.py`
- `backend/app/services/agent_runtime_service.py`
- `backend/app/services/chat_service.py`
- `backend/app/tests/integration/test_chat_agent_run.py`
- `backend/app/tests/integration/test_chat_api.py`
- `frontend/src/types/agentRuntime.ts`
- `frontend/src/types/supplierRisk.ts`
- `frontend/src/components/chat/AgentResponseCard.tsx`
- `frontend/src/components/chat/SupplierRiskCard.tsx`
- `frontend/src/tests/AgentResponseCard.test.tsx`
- `frontend/src/tests/AgentRuntimePage.test.tsx`
- `frontend/src/tests/MessageItem.test.tsx`

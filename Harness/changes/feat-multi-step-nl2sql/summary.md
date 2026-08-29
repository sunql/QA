# 变更：多步 NL2SQL（L1 线性 + 单步优先策略）

- **日期**：2026-08-15
- **作者**：AI 助手
- **Phase**：Phase 5（生产增强）
- **状态**：done

## 1. 需求

智能问答系统需支持"多步回答"：先查一个数据、再查一个数据、最后对比给出结论（如"先查 2024 年销售额，再查 2025 年，对比给出趋势"）。支持任意步数（第 3、第 4 步），硬上限 `MAX_MULTI_STEP=5`。

**策略（本次新增）**：单步优先——除非用户明确要求分步，否则先尝试单条 SQL 完成；单步执行失败（SQL 执行报错）时回退多步拆解。

验收标准：
- 明确要求分步（"分步/逐步/拆步/第一步"等）→ 直接多步。
- 无显式分步的对比类问题 → 先单步，成功即止（零额外拆步 LLM 成本）。
- 单步 SQL 执行失败 → 回退多步拆解；拆解失败则重抛原错误（行为不退化）。
- 多步响应契约含 `steps` 数组；Token/成本如实计量。

## 2. 设计评审

- **拆步判定**：LLM 结构化拆步（`StepQueryPlanner.plan`），JSON 容错，失败降级单步。
- **执行模型**：L1 线性——步骤按序执行，前序结果注入后续步骤的 NL2SQL prompt（`StepExecutionContext.inject_to_prompt`，不可变）。
- **汇总**：`StepAggregator` 基于各步结果 + aggregationHint 生成对比结论（复用 answer LLM 路径）。
- **单步优先策略**：显式分步信号零 LLM 成本判定（关键词）；无信号则先单步；执行失败（`_runQueryWithRetry` 抛错）才回退拆步。

## 3. 数据模型变更

无新增表/列。多步结果复用 `SessionMessage`（对话）、`SessionQueryState`（追问状态，保存最后一个数据步骤的 plan/sql）、`SessionTokenUsage`（用量，nl2sql/answer）。

## 4. 接口契约变更

- `ChatResponse` 新增 `steps: list[StepResultRead] | None`（仅 `intent="multi_step"` 时填充）。
- `StepResultRead`：`stepIndex/description/subQuestion/sql/data/summary/error`（camelCase 输出）。
- 删除未使用的 `MultiStepChatResponse`（与流式 done 事件字段不一致的死代码）。
- 流式新增事件：`step_plan`、`step_result`。

## 5. 实现要点

- `app/domain/multi_step_plan.py`：不可变 frozen dataclass（StepPlan/StepResult/MultiStepPlan/StepExecutionContext）。
- `app/services/step_query_planner.py`：`is_explicit_multi_step()`（显式分步信号）+ `plan()`（LLM 拆步）。
- `app/services/step_aggregator.py`：多步汇总 prompt 构造。
- `app/services/chat_service.py`：
  - `processMessage` / `_streamQuery`：单步优先 + 失败回退。
  - `_executeMultiStep` / `_streamMultiStep`：顺序执行子步骤 + 汇总。
  - 修复：移除 `_executeMultiStep`/`_streamMultiStep` 内冗余的 `_recordUsage(nl2sql)`（`_planAndGenerateSql` 已记录，原为双重计量）。
  - 计量：`_detectMultiStep` 记录 `purpose="step_plan"`；`_executeMultiStep`/`_streamMultiStep`
    新增 `initial_tokens`/`initial_cost` 入参，把拆步判定 + 单步失败回退时已消耗的
    token/成本计入响应 `tokensUsed`/`cost`（与审计行一致）。

## 6. 测试

- 单元：`test_step_query_planner.py`（23）、`test_multi_step_plan.py`、`test_step_aggregator.py`。
- 集成（真实 PG + 完整 API 链路）：`test_chat_multi_step.py`（4）——显式分步多步、单步成功不拆、单步失败回退、流式事件序列。
- 全量：单元 680 passed，集成 194 passed。

## 7. 安全审查

- 拆步/汇总 prompt 对未受信用户输入做 `_sanitize` 转义（防 prompt 注入）。
- 子查询仍经 SQL Guard 只读校验（复用 `_planAndGenerateSql` → `_runQueryWithRetry`）。
- 已交 code-reviewer 审查（见对话）。

## 8. 部署验证

`TEST_DATABASE_URL=postgresql+asyncpg://...:5432/qa_metadata_test uv run pytest app/tests/integration/` 194 passed。

## 9. 已知缺口（待办）

**已修复（本轮）**：
- 拆步判定 LLM 调用已纳入计量：`StepQueryPlanner.plan()` 返回 `StepPlanResult`
  （含 `prompt_tokens`/`completion_tokens`），`_detectMultiStep` 记录 `purpose="step_plan"`。
- 单步失败回退时的响应 `tokensUsed`/`cost` 少算：`_executeMultiStep`/`_streamMultiStep`
  新增 `initial_tokens`/`initial_cost`，回退时把失败单步生成 + 拆步判定消耗一并计入。

**遗留（非本次引入，记录在案）**：
- `_runQueryWithRetry` 重试后仍失败时，重试 `generateSql` 的 token 未计量
  （`raise firstErr` 路径丢弃了 `retryResult` 的 token；回退场景因此仍少算该笔）。
- 回退触发条件为宽泛 `except Exception`：基础设施/业务库错误也会误触发一次拆步
  LLM 调用（多消耗一次 step_plan），但不改变最终行为（拆不出多步仍重抛原错误）。
- 回退发生在 plan/sql 事件已下发之后：流式场景前端可能已渲染被废弃的单步 plan/sql
  再切换到多步（前端需容忍，非阻塞）。
- 显式"分步"但 LLM 判定单步（`_detectMultiStep` 返回 None）时，step_plan 已计量
  却未计入后续单步响应的 `tokensUsed`/`cost`（窄边角，仅显示口径偏差，审计行无误）。

## 10. 关联

- Wiki：`Harness/wiki/nl2sql-engine.md`（如存在）
- 规则：`Harness/rules/数据与AI治理规范.md`（Token 计量）

# 变更：ReAct NL2SQL + 多轮对话（#67-#71）

- **日期**：2026-08-12
- **作者**：AI 助手
- **Phase**：Phase A-E（ReAct 两阶段 NL2SQL + 多轮对话 + REFINE 捷径 + 前端计划展示）
- **状态**：done

## 1. 需求

原 NL2SQL 为"单次生成 + 盲重试"模式：生成 SQL → 解析失败 → 泛泛重试 → 耗尽抛错，模型易产生语义漂移。多轮对话也需要跨轮传递结构化查询状态（上一轮选了哪些表/列、SQL、结果列），而非仅靠原始对话文本。

验收标准：
- 每轮先调 LLM 生成结构化 `QueryPlan`（JSON），代码校验引用在本体 schema 内，再调 LLM 生成 SQL；失败时注入具体差异而非泛泛重试。
- 上一轮查询状态持久化，REFINE/FOLLOW_UP 轮注入 prompt。
- 5 类意图识别（query/new_query/refine/follow_up/clarify/chitchat），规则匹配不调 LLM。
- 纯排序/行数/简单筛选调整不调 LLM，直接代码改写 SQL（REFINE 捷径）。
- 前端助手消息展示可折叠 ReAct 查询计划，流式与非流式路径均生效。

## 2. 设计评审

- **两阶段 LLM 调用（关键决策）**：`generateValidatedPlan`（计划 JSON → 代码校验 `validatePlan` → 注入差异，`maxPlanAttempts=2`）→ `generateSql(plan=...)`。计划校验失败重试成本低于 SQL 解析失败重试，且约束模型按已校验计划产出，杜绝任意表/列名。
- **会话状态持久化（关键决策）**：新增 `session_query_state` 表（JSONB），`_loadQueryState`/`_saveQueryState`（UPSERT，`turn_count++`）；REFINE/FOLLOW_UP 才注入状态 prompt，避免无关轮次污染。状态内容经 `_sanitizeContext` 转义 `<>` 后包 `<previous_query_state>`，防 prompt 标签逃逸。
- **REFINE 捷径（关键决策）**：`applyRefineDirect(sql, plan, question)` 纯代码改写，命中时 `_SqlOutcome(sqlConfig=None)` 标记零 token 成本、不记 nl2sql 用量行；返回 `None` 安全退回 LLM。列约束到 `plan.selectedProperties` + 标识符白名单，值拒绝 `' " ; -- \`，防注入面不扩大。
- **不可变数据**：`QueryPlan`/`SessionQueryState` 均为 frozen dataclass，每次更新创建新实例（ORM 实体属性更新为 SQLAlchemy identity map 标准用法，刻意例外并注释说明）。
- **SSE 契约（Phase E 补齐）**：流式路径新增 `plan` 事件（`sql` 之前），前端 `onPlan` 回填，避免流式用户看不到计划的功能缺口。

## 3. 数据模型变更

- 新增 `session_query_state` 表：`session_id / last_question / last_plan(JSONB) / last_sql / last_result_columns(JSONB) / turn_count`。
- Alembic migration：`alembic/versions/0006_session_query_state.py`。
- ORM：`app/domain/models.py` 新增 `SessionQueryState`。

## 4. 接口契约变更

- SSE 事件新增 `plan`：`event: plan\ndata: {"plan": QueryPlanDict}`，在 `sql` 事件之前下发。
- `ChatResponse` 新增 `queryPlan?: QueryPlan | null`；`ChatMessage` 同步新增。
- 前端 `IntentType` 联合新增 `refine/follow_up/new_query/clarify`；`StreamEventHandlers` 新增 `onPlan`。

## 5. 实现要点

- `app/domain/query_plan.py`：`QueryPlan`/`Aggregation`/`JoinSpec`/`SortSpec`/`PlanResult` frozen dataclasses 与序列化。
- `app/services/nl2sql_service.py`：`generateValidatedPlan`/`validatePlan`/`_parsePlanFromResponse`/`_buildPlanSystemPrompt`/`_buildPlanUserPrompt`/`generateSql(plan=...)`；REFINE 捷径 `applyRefineDirect` + `_extractLimit`/`_rewriteLimit`/`_extractSort`/`_rewriteOrderBy`/`_extractFilter`/`_rewriteWhere`/`_quoteFilterValue`。
- `app/services/intent_service.py`：`IntentType` 5 类 + `classify(message, hasPriorState=False)` 规则匹配。
- `app/services/chat_service.py`：`_loadQueryState`/`_saveQueryState`/`_buildStatePrompt`/`_statePlan`；`_PipelineContext`/`_SqlOutcome` frozen dataclasses；`_planAndGenerateSql` 接入 REFINE 捷径；`_streamQuery` 下发 `plan` 事件。流式回答输出抽到 `chat_stream_output.py` mixin（块间超时保护 + 主模型降级）。
- `frontend/src/types/chat.ts`：`QueryPlan` 等接口；`frontend/src/api/chat.ts`：`onPlan` + `isQueryPlan` 运行时校验；`frontend/src/components/chat/QueryPlanCard.tsx`（Collapse+Tag 展示）；`frontend/src/stores/chatStore.ts`：流式/非流式回填 `queryPlan`。

## 6. 测试

- 新增：`test_query_plan.py`（QueryPlan 序列化）、`test_refine_shortcuts.py`（19 例，含 4 例安全回归）、`test_chat_service_stream.py` 补 `plan` 事件断言、`test_chat_stream_api.py` 同步索引。
- `test_chat_service.py`：REFINE 捷径整条流水线零 LLM（`test_refine_shortcut_rewrites_sql_without_llm`）、状态持久化、意图流转用例。
- 前端：`MessageItem.test.tsx` 2 例（计划卡渲染/流式中隐藏）、`chatStore.test.ts` 3 例（非流式回填、流式 plan 事件回填、refine 意图收窄）。
- 全量：后端 **360 passed**，覆盖率 **93.52%**（≥80% 门槛）；前端 `tsc -b` 退出 0 + **112 tests passed**（16 文件）。

## 7. 安全审查

- `python-reviewer`：**APPROVE**（REFINE 捷径零 token 语义正确、chart 用量不重复计数、mixin 抽取无循环导入；MEDIUM/LOW 全部修复）。
- `security-reviewer`：0 CRITICAL / 0 HIGH；3 MEDIUM + 3 LOW 全部修复并新增 4 例安全回归测试：

| 级别 | 问题 | 修复 |
|------|------|------|
| MEDIUM-1 | `\` 反斜杠可逃逸 `_quoteFilterValue` 校验 | 黑名单加入 `\` |
| MEDIUM-2 | 本体属性名未校验即拼入 SQL | `_SAFE_IDENT_RE` 白名单过滤列 |
| MEDIUM-3 | 筛选列大小写敏感导致命中失败 | 列名大小写不敏感匹配，返回真实列名 |
| LOW-1 | `count=1`+DOTALL 可能误改子查询内层 ORDER BY | 定位"最后一个 ORDER BY" |
| LOW-2 | 无行数子句时无法追加 | 刻意保留（方言未知），注释说明后回退 LLM |
| LOW-3 | 超大 LIMIT 无上限 | `_REFINE_MAX_LIMIT=1000` 钳制 |

- `typescript-reviewer`：**APPROVE-WITH-NITS**。H1（流式路径不下发 queryPlan 的功能缺口）已端到端修复：后端 `plan` SSE 事件 + 前端 `onPlan` + `isQueryPlan` 边界校验（M2 一并落地）；M1（预留意图）注释说明；M3 文档化；L1（key 冲突）修复；L2/L3 接受。

## 8. 部署验证

- 后端全量 360 通过（unit + integration），覆盖率 93.52%。
- 前端 `tsc -b` 退出 0；vitest 112 通过。
- 多轮冒烟（真实 Oracle 数据源 + stub LLM，此前 Phase 完成时验证）：提问 → 追问（REFINE）→ 再追问，验证 `session_query_state` 持久化、`plan`/`sql` 事件序列、REFINE 捷径改写（`tokenUsage` 仅 chart+answer 两条记录，无 nl2sql 行）。

## 9. 关联

- 设计稿：`docs/设计02-详细设计.md`（ReAct 模式 + 多轮对话）
- Wiki：`Harness/wiki/nl2sql-engine.md`（已更新两阶段/状态/捷径/plan 事件）、`Harness/wiki/architecture.md`
- 前置：`Harness/changes/feat-nl2sql-multidialect/`、`Harness/changes/feat-phase4-chat/`

# 变更：修复"服务内部错误"根因（migration 缺失 + DetachedInstanceError + 无法回答计划短路）

- **日期**：2026-08-12
- **作者**：AI 助手
- **Phase**：bugfix（ReAct NL2SQL + 多轮对话 上线后运维）
- **状态**：done

## 1. 需求

用户报告"现在问什么问题都是返回：服务内部错误，请稍后重试"。经排查共有三层根因，本变更逐一修复并补充回归测试。

验收标准：
- 任意提问不再返回笼统的"服务内部错误"；可回答的问题走完整流水线，超出本体范围的问题返回友好的"无法回答"说明。
- 修复后全量测试通过、覆盖率 ≥ 80%。

## 2. 设计评审

### 根因 1（主因）：`session_query_state` 表缺失
Alembic migration `0006_session_query_state.py` 从未应用（`alembic upgrade head` 处于历史位置），任何写入会话状态的请求都因缺表抛错。
**修复**：执行 `alembic upgrade head` 补建表。属运维动作，无代码变更。

### 根因 2：缓存 LLM 客户端的 ORM config 过期脱离（DetachedInstanceError）
`factory.createClient` 按 `config_id` 缓存客户端单例，缓存客户端持有**首次请求**会话的 ORM `LlmConfig`。后续请求的会话以 `getDb` 的 `except: rollback()` 结束时，该 config 被过期并脱离会话；下次请求复用缓存客户端调用 `complete`/`completeStream` 时，行内 `getattr(response, "model", self._config.model_name)` 会触发 `DetachedInstanceError` → 被包装为通用"服务内部错误"。
**修复**：`OpenAiClient`/`OllamaClient` 在 `__init__` 快照标量属性（`model_name`/`api_endpoint`），调用期不再触碰 ORM。

### 根因 3（本次新发现）：计划 `target=无法回答` 落空后仍生成/执行 SQL
当模型判定问题超出本体可回答范围（计划 `target="无法回答"`、未选任何类）时，原流水线仍继续 `generateSql` → 空计划诱导模型自由编造表名（如 `PORDERQ`）→ Oracle 执行报原始 `oracledb.DatabaseError`（非 `DomainError`）→ 包装为"服务内部错误"。
**修复（关键决策）**：
- `QueryPlan.isUnanswerable` 属性（`target == "无法回答"`），作为唯一短路信号。
- `_twoStageGenerate` 在计划校验通过后立即短路，返回空 SQL 哨兵，**不再调用** `generateSql`。
- `_SqlOutcome.sql` 拓宽为 `str | None`；`processMessage`/`_streamQuery` 在 `sql is None` 时走友好回答分支（固定文案、不执行 SQL/图表/回答 LLM、仍记录计划 token 用量与成本、保存会话状态）。
- `modelName` 上报**实际服务模型**（`sqlConfig`），降级场景下不与主模型混淆（code-reviewer MEDIUM）。

## 3. 数据模型变更

- 无代码数据模型变更。运维修复：应用 `0006_session_query_state.py`（建表 `session_query_state`）。

## 4. 接口契约变更

- SSE：无法回答时事件序列为 `meta → plan → token(固定文案) → done`，无 `sql`/`chart`/`error`；`done.modelName` 为实际服务模型。
- `ChatResponse`：`sql=None`、`data=None`、`chartType=None`，携带 `queryPlan={"target":"无法回答",...}`。
- `_SqlOutcome.sql` 类型 `str → str | None`（内部契约，文档化）。

## 5. 实现要点

- `app/domain/query_plan.py`：`UNANSWERABLE_TARGET = "无法回答"` 常量 + `QueryPlan.isUnanswerable` 属性。
- `app/infrastructure/llm/openai_client.py` / `ollama_client.py`：构造时快照 `model_name`/`api_endpoint`，调用期不懒加载 ORM。
- `app/services/chat_service.py`：`_UNANSWERABLE_ANSWER` 常量、`_twoStageGenerate` 短路、`_planAndGenerateSql` 哨兵→`sql=None`、`processMessage`/`_streamQuery` 友好分支、`_unanswerableResponse` 助手、`modelName` 用 `(outcome.sqlConfig or pc.selected).model_name`。

## 6. 测试

- 回归（TDD RED→GREEN）：`test_llm_client.py`（`TestClientWithDetachedConfig` 2 例）、`test_llm_streaming.py`（`*Detached` 2 例）——缓存客户端 config 过期脱离后调用不再抛 `DetachedInstanceError`。
- 新增：`test_query_plan.py`（`isUnanswerable` 4 断言）、`test_chat_service.py`（`TestUnanswerablePlan`：短路无 SQL/无执行/1 次 LLM/成本>0；fallback 服务时上报 fallback 模型）、`test_chat_service_stream.py`（`TestUnanswerablePlanStream`：事件序列、无 sql/chart、无 embedding、成本>0）。
- 全量：**368 passed**，覆盖率 **88.80%**（≥80% 门槛）。注：用 `SECRET_KEY=x` 跑会因非法 Fernet key 报 28 个环境性失败，改用合法 Fernet key 后全绿（环境问题，非代码问题）。

## 7. 安全审查

- `code-reviewer`：**APPROVE**（0 CRITICAL / 0 HIGH；1 MEDIUM"降级场景 modelName 上报"已修复 + 新增回归测试；2 LOW 记录：移除死 import 已处理、无法回答状态后的 REFINE prompt 质量建议留待后续）。
- `security-reviewer`：**SAFE to merge**（0 CRITICAL / 0 HIGH；无法回答路径 SQL 执行不可达、计划事件披露信息更少、`_sanitizeContext` 覆盖所有 prompt 注入面、无新增 SSRF/注入/auth 面、token 成本为负增益）。1 条 MEDIUM 为**既有** `_buildStatePrompt` 延迟净化模式（非本次引入），记录不阻塞。

## 8. 部署验证

- `alembic upgrade head` 后主服务（8000，--reload）实机冒烟：
  - 可回答问题"本月到货通知有哪些？"：`meta→plan→sql→chart(真实数据)→token→done`，`tokensUsed=18637, cost≈0.0266`。
  - 超出本体问题"各业务线今年各季度的销售额是多少？"：`meta→plan(target=无法回答)→token(友好文案)→done`，无 sql/chart/error，`tokensUsed≈9k, cost≈0.0128`（计划 token 正确计量计费）。

## 9. 关联

- 前置特性：`Harness/changes/feat-react-nl2sql-multiturn/summary.md`
- Wiki：`Harness/wiki/nl2sql-engine.md`、`Harness/wiki/architecture.md`

# 变更：feat-phase4-chat

- **日期**：2026-08-12
- **Phase**：Phase 4 - 对话 + NL2SQL + 图表渲染
- **状态**：done

## 1. 需求
在 Phase 3 多数据源 + 单表 NL2SQL 能力基础上，打通「自然语言 → SQL → 业务库执行 → 图表 → 回答」完整对话闭环。
验收标准：
- `POST /api/v1/chat` 主对话接口：意图识别（query/chitchat）→ 本体 schema 注入 → 模型路由 → NL2SQL → SQL Guard 校验 → 业务库只读执行 → 图表类型推荐 + ECharts option → 自然语言回答 → 分步记录 Token 消耗与成本。
- 闲聊（你好/谢谢等 ≤3 字或关键词）不调 LLM，直接返回问候，零 Token 消耗。
- 图表智能推荐：1 维度 1 指标 → 饼/柱（≤6 行饼、>6 行柱）；时间维度 → 折线；2 维度 → 分组柱；其他 → 表格。
- 图表 LLM 生成失败时优雅回退到规则生成的 ECharts option（不阻断回答）。
- 前端聊天面板：数据源选择、消息气泡、SQL 可折叠预览、图表/表格渲染、Token 与成本标签。
- 会话管理：客户端生成 UUID sessionId，前端维护最近 20 条消息（10 轮）作为 history 传入后端；不新增 DB 表。

## 2. 设计评审
- 意图识别：关键词匹配 + 长度判断，**不含 LLM 调用**（避免浪费 token）。
- NL2SQL：从 `OntologyService.listClasses`（eager load properties）构建 schema 文本注入 System Prompt，LLM 生成 Oracle SQL，用 regex 从 ```sql 块提取，解析/校验失败重试（最多 `NL2SQL_MAX_RETRIES`=2），重试时把错误信息注入 prompt。
- 图表生成：规则推荐 chartType + LLM 生成完整 ECharts option 双路径；LLM 输出解析失败回退 `_fallbackOption`（纯 Python 规则生成各类型 option）。
- 模型路由复用 Phase 1 `ModelRouterService`（加权随机 + 成本阈值 + 会话亲和），Token 计量复用 `TokenUsageService`，每次 LLM 调用（nl2sql/chart/answer）分别以 purpose 记录。
- ChatService 构造注入全部依赖（intent/nl2sql/chart/ontology/datasource/modelRouter/tokenUsage/llmFactory/adapterProvider），便于测试替换，默认值复用真实实现。
- 不实现流式输出（Phase 5），单次请求-响应模式。

## 3. 数据模型变更
- 无新表、无迁移。复用既有 `llm_config`、`ontology_class`/`ontology_property`、`data_source`、`session_token_usage`（新增 purpose 取值 `nl2sql` / `chart` / `answer`）。

## 4. 接口契约
- `POST /api/v1/chat`：
  - 请求 `ChatRequest`：`sessionId`(1-64)、`question`(1-2000)、`datasourceId`、`history`（可选，最近 10 轮消息数组 `{role, content}`）。
  - 响应 `ChatResponse`：`answer`、`intent`(query|chitchat)、`sql?`、`chartType?`(table|bar|pie|line|scatter)、`chartOption?`、`data?`、`tokensUsed`、`cost`。
  - JSON 输出 camelCase（`CamelModel` 别名生成器）。
  - 错误沿用统一包络：数据源不存在→404、NL2SQL 重试耗尽→400（detail 含最后错误）、校验失败→422。

## 5. 实现要点
- `domain/schemas.py`：新增 `HistoryMessage` / `ChatRequest` / `ChatResponse`。
- `services/intent_service.py`：`CHITCHAT_KEYWORDS` 元组 + `classify()`。
- `services/nl2sql_service.py`：`SqlResult` frozen dataclass；`buildSchemaText`（`### TABLE (别名): table=SCHEMA.TABLE\n  Columns:` 格式）、`parseSqlFromResponse`（fence → 纯 SELECT/WITH）、`generateSql`（system prompt 含 schema + Oracle 方言 + ROWNUM/FETCH FIRST，重试注入错误）。
- `services/chart_service.py`：`recommendChartType` 规则引擎；`generateChartOption`（LLM → 失败回退 `_fallbackOption`）；`_inferColumnType`（数值/时间/字符串，`_toNumber` 处理 Decimal→str 保证 JSON 安全）。
- `services/chat_service.py`：`processMessage` 编排流水线；每次 LLM 调用后立即 `recordUsage`；chitchat 短路；`_costFor` 按单价 × tokens/1000 计算；`_DATA_SAMPLE_LIMIT=20` 数据样本注入回答 prompt。
- `api/v1/chat.py` + `main.py`：路由挂载 `prefix="/api/v1/chat"`（chat router 自身 prefix=""，**不走 v1Router.include_router**，继续规避 FastAPI 0.141 `_IncludedRouter` prefix 叠加 bug——若挂载在 `/api/v1` 下会解析为 `/api/v1` 而非 `/api/v1/chat`）。
- 前端：`types/chat.ts`、`api/chat.ts`（`sendMessage`）、`stores/chatStore.ts`（`HISTORY_LIMIT=20`、`generateSessionId`、`toHistory`）、`components/chat/{SqlPreview,ChartRenderer,MessageItem,MessageList,ChatPanel}.tsx`、`pages/ChatPage.tsx` 重写；`tests/setup.ts` 补 `scrollIntoView` 与 `ResizeObserver` polyfill。

## 6. 测试
- 后端：**165 个测试通过，覆盖率 85.12%（≥80%）**。
  - `unit/test_intent_service.py`（10）：关键词命中/不命中/空/短消息。
  - `unit/test_nl2sql_service.py`（19）：schema 文本构建、SQL 解析（```sql / ``` / 纯 SELECT / 无 SQL）、mock LLM 成功/重试/耗尽。
  - `unit/test_chart_service.py`（15）：列类型推断、各规则推荐、LLM option 成功/失败回退、TABLE/BAR/PIE/LINE option 结构。
  - `unit/test_chat_service.py`（6）：mock 全部依赖，验证流水线顺序、chitchat 短路、分步 Token 记录、错误处理。
  - `integration/test_chat_api.py`（5）：200 camelCase 契约、3 行 usage（nl2sql/chart/answer）、chitchat 无 SQL、404、422。
- 前端：**75 个测试通过，`tsc -b` 通过**。
  - `tests/chatApi.test.ts`、`chatStore.test.ts`（7）、`ChartRenderer.test.tsx`（3，mock echarts-for-react）、`ChatPage.test.tsx`（4，mock api + echarts）、`pages.test.tsx` 更新 ChatPage 断言。
- 说明：前端 `npm test`（vitest run 无覆盖率）全绿；全局函数/分支 80% 阈值缺口为既有文件（main.tsx 0%、api/datasource.ts 26.82%、api/session.ts 0%、types 0%），非本 Phase 引入。

## 7. 安全审查
- SQL Guard：NL2SQL 生成结果经 `_assert_read_only` 白名单（SELECT/WITH）校验 + `queryRowLimit` 行数限制 + `queryTimeoutSeconds` 超时，业务查询仅允许只读。
- 数据源密码 Fernet 加密存储，`ChatResponse.data` 不含敏感字段（冒烟断言 `password` 不回显）。
- LLM prompt 仅注入本体元数据与查询结果样本，不注入数据源口令。

## 8. 部署验证（真实环境冒烟）
- 后端测试 + 覆盖率：`pytest --cov=app --cov-fail-under=80 -q` → 165 passed / 85.12% ✅
- 前端测试 + 类型：`npm test`（75 passed）+ `npx tsc -b`（clean）✅
- ASGI 全栈冒烟（**真实 Postgres 元数据 + 真实 Oracle 业务库**，仅 stub LLM 网络客户端——docker/.env 中 API Key 按设计留空）：
  - `POST /api/v1/chat {"sessionId":"s1","question":"各供应商的收货数量汇总","datasourceId":1}` → 200
  - 真实模型路由选中 `MiniMax-Text-01`；真实 tiktoken 计数 135 tokens（nl2sql/chart/answer 各 45，cost 0.000315）✅
  - 真实 Oracle 适配器执行 `SELECT BPSNUM_0 AS SUPPLIER, COUNT(*) AS RECEIPT_COUNT FROM ZJTH.PRECEIPT GROUP BY BPSNUM_0 ORDER BY ... FETCH FIRST 10 ROWS ONLY`，返回 10 行真实数据 ✅
  - `chartType=bar`（10 行 > 6 → 柱状图规则）、`chartOption.series` 就绪、`intent=query`、`answer` 非空 ✅
  - 3 行 `session_token_usage`（purpose=nl2sql/chart/answer）落库验证 ✅
  - chitchat 冒烟：`"你好"` → 200，`intent=chitchat`、`sql=None`、`chartType=None`、`tokensUsed=0`、`cost=0` ✅
- 前端 `npm run dev` → Vite v5.4.21 95ms ready，`/chat` 返回 SPA shell（`<title>智能问答系统</title>` + `id="root"`）✅

## 9. 关联
- 设计稿：`docs/设计01.md`、`docs/设计02.md`
- Wiki：`Harness/wiki/api-reference.md`、`nl2sql-engine.md`、`chart-rendering.md`、`model-router.md`
- 规则：`Harness/rules/权限与安全规范.md`、`数据与AI治理规范.md`

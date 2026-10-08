# AI Chat 对话服务能力清单

> 盘点日期：2026-09-28　·　范围：`backend/app/services/chat_service.py` 及 7 mixin + 1 helper
> 对应代码分支：`feat/business-object-registry`（HEAD = `fa389bc`，含 Lever E 全路径）
> 评估版同目录 `chat-service-assessment.md`（缺陷侧，本文是能力侧）

---

## 0. 一句话结论

`ChatService` 是以 **NL2SQL 为主干、本体/语义召回为入口、多轮状态为骨架、
模型路由与 L4 Agent 为兜底、Token 计量与流式断连兜底为合规硬底座** 的端到端
BI 智能问答系统——支持 **13 类意图分发**、**多步拆解**、**追问级联**、
**图表渲染**、**知识图谱多跳**、**Agent 运行时**、**供应商 360°/风险专项**，
全路径走真实 PostgreSQL + 完整 API 链路 + 80% 测试覆盖。

---

## 1. 入口与路由（API 层）

| 路由 | 能力 |
|---|---|
| `POST /api/v1/chat` | 主对话接口（同步）：意图识别 → NL2SQL → 图表 → 回答 |
| `POST /api/v1/chat/stream` | **SSE 流式**：meta → sql → chart → token×N → done；`background=` 钩子兜底断连补写 |
| `POST /api/v1/chat/suggest` | 历史查询向量检索相似问法（输入联想） |

实现：`backend/app/api/v1/chat.py`，路由 prefix 由 `main.py` 挂载到 `/api/v1` 下。

---

## 2. 13 类意图分发（`_classifyMessage` 入口）

| 意图 | 处理路径 | 关键能力 |
|---|---|---|
| `QUERY` / `NEW_QUERY` | NL2SQL 主流水线 | 全链路 ReAct 两阶段 + REFINE/FOLLOW_UP/多步/全局 filter |
| `REFINE` | NL2SQL 主流水线 | 上一轮状态 + 纯代码捷径（`_tryRefineDirect`：排序/行数/简单筛选改写 SQL，跳过 LLM） |
| `FOLLOW_UP` | NL2SQL 主流水线 | `_rewriteFollowUpQuestion` 重写 + 多步全局 filter |
| `CLARIFY` | `_handleClarify` | 概念解释，注入 schema 但**不执行 SQL、不更新查询状态** |
| `DEFINE` | `_handleDefineClass` | 斜杠命令：创建本体类 |
| `MAP` | `_handleMapProperty` | 源属性映射到目标类（设置 `ref_class_id`） |
| `METRIC` | `_handleDefineMetric` / `_handleShowMetric` | 创建 / 列举指标（含公式） |
| `CHITCHAT` | `_chitchatResponse` | 闲聊模板，**零 LLM、零 Token 计量** |
| `SUPPLIER_360` | `_handleSupplier360` → `Supplier360Service` | 供应商 360° 视图（Phase 5.3） |
| `SUPPLIER_RISK` | `_handleSupplierRisk` → `SupplierRiskService` | 供应商风险 Agent（Phase 5.4） |
| `GRAPH_REASONING` | `_handleGraphReasoning` → `GraphTraversalService` | 知识图谱多跳推理（Phase 6.3） |
| `AGENT_RUN` | `_handleAgentRun` / `_runL4AgentLoop` | Agent 运行时调度 Tool（Phase 6.4） |

意图分类规则 + 状态依赖（REFINE/FOLLOW_UP 仅在有上一轮状态时产出）见
`IntentType` 枚举的 docstring（13 类，全量接入流水线）。

---

## 3. NL2SQL 引擎能力（核心 5 类意图走这条）

### 3.1 本体召回（`RecallMixin`）

- `_selectRelevantClasses`：**向量 + 关键词**双通道召回相关本体类（topK/hit 阈值经 `system_config` 可调）
- `_rankByLayer`：分层加权（**ADS > DWS > DWD > DIM**，ODS_BUSINESS 默认排除）；维度问题降权
- `_fallbackRecall`：5 分支降级路径（向量失败 / 召回为空 / 降权失败等）
- `_expandByJoinNeighbors`：**1-hop JOIN 邻居扩边** —— 防召回把"成对表"拆散（schema 缺明细类 → LLM 编造）
- `_buildFewShot`：**语义相似历史查询**做 few-shot，复用成功经验
- `DIM_SUPPLIER 别名治理` + `JOIN 孤岛补边`：维度层召回补 7 条事实表边
- `ODS 召回入口过滤`：`_selectRelevantClasses` 加 `_isOdsBusinessTable` 过滤

### 3.2 两阶段 NL2SQL（`_planAndGenerateSql`）

- **ReAct 计划阶段**：`_twoStageGenerate` → 计划 → 校验 → SQL 生成
- **schema 优化（−29% token）**：`buildSchemaText` + `_buildCriticalColumnsDigest` 顶部摘要
- **REFINE 捷径**：`_tryRefineDirect` 纯代码改写（排序/行数/简单筛选），命中零 LLM
- **计划降级**：`isEmptyPlan` 移 `rowLimit/perGroupLimit/target` + `isUnanswerable` 短路
- **公式 SQL 关键字集合**：含 ASC/DESC/OVER/排名（`qa-system-asc-desc-keyword-drift`）
- **多步 + 全局 filter**：`MultiStepMixin` 拆步；`_resolveGlobalFilters` 一次性 LLM 抽取全局范围类约束（注入每步）
- **追问重写 + 级联**：A/B/C 三修收敛入场点 4→1
- **失败隔离**：`_executeDataStep` 共享 helper + `sql is None` 唯一失败标记（C3/C4 修）
- **同模型瞬态重试**（M4）：`llm_retry_policy` + `allowRetry=attempt==0`，异常必须带走已累加用量
- **断连落库（H4）**：`StreamingResponse(background=...)` + `StreamPersistState` 单发标志 + 0085 migration
- **失败路径用量**（H1/H2/H8/H9）：`LlmClientError.tokens` + `consumedTokens` 第二档；不伪造 0
- **Prompt Cache 账单**（Lever E）：`_costFor` 读 `cached_tokens` + `LLM_CACHE_HIT_MULTIPLIER=0.25`（DeepSeek 1/4 价），SQL / 图表 / 回答 / 流式全路径透传

### 3.3 图表（`ChartService` 门面 + 决策引擎，2026-09-30 重写）

> 完整规则表与契约见 [chart-rendering.md](./chart-rendering.md)。

- **决策引擎规则优先**：`decideChartKind` 按数据语义选型（R00–R14，首个命中者胜），
  **LLM 不再是决策者**，只在规则歧义时给一个语义标签（`TREND|SHARE|RANK|COMPARE|
  RELATION|DETAIL|KPI` 白名单），**绝不产出图表代码**
- **标准化 spec 驱动渲染**：`chart_spec_builder` 从 `QueryPlan` 派生出 ChartSpec
  （标签取 `aggregations[].alias` / `target`），`chart_renderer` 逐 kind 转成
  **不含颜色**的 ECharts option；11 类（折线/柱状/横柱/饼/环形/散点/热力图/KPI/表格/
  柱线组合/瀑布）
- **服务端只发结构**（决策 6）：颜色/轴色/文字色全部由前端 `applyChartTheme` 按主题补
- **降级不产空图**：规格校验失败、渲染异常、空数据一律降级 TABLE + warning
- **多步每步出图**（决策 3）：`StepResultRead` 与 `EVENT_STEP_RESULT` 各带
  `chartType`/`chartOption`；分类器每轮最多调 1 次（跨步预算）
- **最终回答也出图**（0105，用户反馈「图表应该在最终报告里，不是只在分析计划里」）：
  多步顶层 `chartType`/`chartOption`/`data` = **最后一个成功数据步骤**那张，
  流式在回答 token 之后、`done` 之前发**一次** `EVENT_CHART`；全步骤失败 → `None` 不发。
  计划卡里的每步图保留
- **导出 PDF 是真图**（0105）：前端用 ECharts 把图导成 PNG 随导出请求回传，后端
  三档降级排版（原生 table/kpi > PNG > 占位框）；`session_message` 落 `chart_type`/
  `chart_option` 两列，故**历史回放也能看到图**。导出端点为此由 GET 改 **POST**
- **导出取图的窗口必须对齐**：导出本身只覆盖**最近 500 轮**，而 `/messages` 默认按
  `id.asc()` 返回**最早**那批 —— 长会话下两个窗口不相交，`attachChartImages` 一张也匹配
  不上，导出的 PDF **全是占位框且不报任何错**。故 `/messages` 增 `tail=true`
  （先倒序取最新 N 条再翻回正序，响应契约恒为正序）；`before_id` 与 `tail` 同给时
  **`before_id` 优先**
- **落库截行要双向披露**：表格 `rows` 是全量（`QUERY_ROW_LIMIT` 默认 0 = 不限行），
  落库截到 200 行并打 `truncated: true`。**PDF 与前端回放都必须如实标注** —— 只有一侧
  标注时，另一侧的读者会以为那是完整结果
- **导出请求必须自带 Bearer**：该调用刻意绕开 `httpClient`（二进制响应），于是也绕开了
  请求拦截器的 Bearer 注入，而 `sessions` router 是 router 级鉴权 ⇒ 少了它一律 403。
  头走 SSOT `api/authHeaders.ts`；nginx 会剥掉 `X-User-*`，别再手搓那族头
- **L1 KPI 直答补指标卡**（决策 7）：`chartType=kpi` + `{"kpi": {label, value, unit, delta}}`；
  值不能转成数字时不发卡
- **阈值治理**：`CHART_PIE_MAX_ROWS` / `CHART_HBAR_MIN_ROWS` / `CHART_HEATMAP_MIN_COVERAGE`
  / `CHART_TOP_N_MAX` 走 `system_config`
- 失败 / 非 JSON / 无 series 全部优雅回退；token 计量口径（4-tuple + `cachedTokens`）不变

### 3.4 回答与可读性

- `_callWithFallback`：**模型 fallback 链**（多 LLM 配置降级）
- `_buildAnswerText` / `_buildAnswerPrompt`：基于 SQL + 数据 + 历史生成自然语言回答
- `_unanswerableResponse` + `_buildUnanswerableSuggestion`：不可回答时给"缺表/缺术语"建议
- `_buildDataQualityBadges`：查询结果附数据质量徽章
- `_buildAffinityStatus`：亲和度（连续轮次识别用户偏好）
- **Feature 规则引擎**：`feature_rule_config` DB-backed + 3-tier RISK_SCORE + RISK-priority bypass wrapper

### 3.5 模型路由（`_resolveChatLlmClient` / `_usableModelConfigs`）

- `dto.modelId` 优先 → router 选 → keyless 503 / 显式 modelId 404（`qa-system-chat-llm-router-keyless-500`）
- L4 agent loop / 风险点 / Agent 运行时走相同解析口径

### 3.6 Wiki 业务规则注入（`WikiInjector` / `WikiLinkService` / `WikiChunkLoader`）

NL2SQL 在生成 SQL 前，把与**本次召回的 ontology** 关联的 wiki chunk 注入
system prompt，让 LLM 拿到业务口径而非仅靠 schema 推。

- **`WikiLinkService.getLinksByOntology`**：按 `(ontology_type, ontology_id)` 批量反查
  `wiki_ontology_link`（索引 `ix_wol_ontology` 命中；PG 元组 IN 语法）
- **`WikiLinkService.listConfiguredOntologyTypes`**：`SELECT DISTINCT ontology_type ... WHERE revoked_time IS NULL`，
  供注入侧判断是否值得为某类型做语义召回（某类型一条链接都没有 ⇒ 跳过该类型召回，零额外成本）
- **按需召回（`chat_service._collectWikiBlock`）**：class 分数取自 `_selectRelevantClasses` 附加的
  `_recall_score`；property / metric 仅当 `listConfiguredOntologyTypes` 显示该类型确实配了链接时才
  `searchByKeyword(..., typeFilter=type)` 召回（`_recallExtraOntologyScores`），单类型失败只跳过该类型不阻断注入
- **`WikiChunkLoader.loadChunks`**：段落级走 Milvus `wiki_page_embeddings`（`(page_id, chunk_id)` 双键），
  整页级走 PG `wiki_page.content`（Markdown 全文 + 4000 字符截断）
- **`WikiInjector.collectAndScore`**：纯函数算法（spec §6.1）：
  - 评分 `score = Σ(weight × recall_score)`，同 chunk 多 ontology 自动合并
  - 剪枝：未召回 ontology 的链接不进注入
  - 预算：按 `WIKI_INJECTION_MAX_CHARS=2000` / `WIKI_INJECTION_MAX_CHUNKS=5` 截断（末位可截不丢已有块）
  - 配置异常 → `_DEFAULTS` dict 兜底不抛出（`backend/app/services/wiki_injector.py:62`）
- **`WikiInjector.renderPromptBlock`**：返回完整 markdown 块（含 `### 业务规则补充` header +
  适用 ontology 标注 + `[规则来源]` footer）；空 scored → 不渲染（与改前等价）
- **总闸**：`WIKI_INJECTION_ENABLED=false` → 整条链路短路，注入 0 chunk
- **失败模式**：4 步任一异常 → catch + log warning + 退化为空块（graceful degradation）
- **审计**：每次成功注入都写 `nl2sql_wiki_trace`（90 天保留）；admin CRUD 进 `audit_log`
- **可观测性**：`wiki_injector_inject_total` / `wiki_injector_injected_chars` histogram /
  `wiki_injector_truncated_chunks_total` 三个 Prometheus 指标 + `[wiki_injector]` 结构化日志
- **接入点**：`chat_service._planAndGenerateSql` 在两阶段 prompt 拼装前调用，
  `_buildTwoStagePrompt` 增加 `wikiRulesBlock` 参数（与 contextBlock 同层）

完整运营指南见 [wiki-ontology-link.md](wiki-ontology-link.md)，对应 spec
`docs/superpowers/specs/2026-09-28-wiki-ontology-link-design.md` §6 算法 / §10 Observability。

---

## 4. 多轮状态与会话上下文（`ContextMixin`）

- `_loadRecentRounds`：**服务端持久化消息**（user + assistant + sql），按时间正序取最近 N 轮
- `_roundsFromClientHistory`：服务端无记录时**降级**到客户端 `history` 字段
- `_buildContextPrompt`：上下文注入 NL2SQL System Prompt
- `_saveQueryState`：每轮成功查询 UPSERT 到 `session_query_state`（SQL + plan + 数据快照）
- `_buildStatePrompt`：REFINE/FOLLOW_UP 注入上一轮查询状态
- **预算控制**：`contextPromptCharBudget` / `stateHistoryFieldLimit` / `contentSegmentLimit` / `sqlSegmentLimit` 全可调
- **可调旋钮均进 `system_config`**：`getContext*Limit` / `getStateHistoryFieldLimit` 模式

### 4.1 会话归属与客户端恢复（2026-09-30）

两个必须一起理解的事实（写与回放拆开上线是**回退**，见 `changes/2026-09-30-chat-session-restore`）：

- **上下文的两半来源不同**：发给 LLM 的对话历史是**前端**带的 `history`（屏幕上可见的那批），
  而追问锚点（`last_plan` / `last_sql`）由**服务端**按 `session_id` 存在 `session_query_state`
  （`chat_recall.py` 只按 session_id 读，从不校验这轮在屏幕上是否可见）。
  ⇒ 所以「只让前端记住 sessionId 却不回放」会让用户对着空屏提问、却拿到**关于一场看不见的对话**的回答。
- **客户端恢复**：`qa:chat:lastSessionId:<channel>`（每渠道一个恢复目标）+ `qa:chat:lastChannel`。
  面板挂载声明渠道（`enterChannel`）→ 有指针则以 `tail: true` 回放**最新**那批 → 失败（404/403/422）
  **清指针并换新会话**（否则每次刷新重放同一个失败，而失败只在历史面板展开时可见 ＝ 表现为「刷新永远空白」）。
  发送路径必须写指针 —— 原缺陷正是「只有点历史项/换渠道才写，且写的多是刚生成的空会话 id」。

### 4.2 会话归属守卫（`api/v1/session_guard.py`）

`assertSessionOwnership(session, sessionId, user)`，语义：**admin 放行** → 有归属标记且不含当前用户
→ **403**（detail 不回显归属者，防侧信道枚举）→ 无归属标记 → **fail-open**。

- 挂载点（**7 处**）：`GET /sessions/{id}/messages`、`DELETE /sessions/{id}`、`POST /sessions/{id}/export.pdf`、
  `GET /sessions/{id}/usage`、`GET /sessions/{id}/usage/list`、
  `POST /chat`、`POST /chat/stream`、`GET /chat/sessions/{id}/hypotheses`。
  写端点也要守卫：**不加就能继承别人会话的追问锚点**；usage 两处属「按 sessionId 暴露单会话内容」的对称端点
  （拿到 id 就能读别人的 token / 成本 / 模型明细）。
- **事实源按渠道取并集**（`getSessionOwnerUserIds(..., channel=None)`）：被守卫的端点服务
  chat / doc_qa / wiki_qa **三个**渠道，只查 `channel='chat'` 会让另外两个渠道恒返空集 ⇒ 守卫**静默失效**（已修，4 条用例钉住）。
- **403 文案中性**：`MSG_SESSION_NOT_OWNED` = 「会话不存在或不属于当前用户」。刻意不区分
  「不存在」与「不属于」—— 区分即泄露「这个 id 存在」。（原 `MSG_HYPOTHESIS_SESSION_NOT_OWNED`
  「无权访问该会话的分析假设」已删除：守卫搬到共享模块后服务 7 个端点，该文案在一半端点上描述错对象。）
- **fail-open 覆盖面有限**：`user_id` 2026-09-30 才开始写（chat 渠道 1032 行里 **920 行 NULL**）⇒
  存量未打标会话仍对任何登录用户开放（读 / 删 / 导出）。回填属 prod 数据变更，**未做**。
- 仍未纳入归属：`GET /chat-history`（会话枚举，仅按 channel 过滤，**明确不做** —— 严格过滤会让
  存量 NULL 占多数时所有人的面板变空，须先定回填口径）。

---

## 5. L4 Agent Loop（`L4Mixin`）

- `_handleNl2SqlAgent`：**L4 入口**（NL2SQL 失败兜底时启动 Agent）
- `_runL4AgentLoop`：调 `AgentRuntimeService.run_agent_loop`，异常降级返回 None
- `_buildL4ChatResponse`：把 `AgentLoopResult` 包成 `ChatResponse` + 持久化 + audit
- **预算控制**（`qa-system-l4-iteration-budget`）：`_L4_SYSTEM_PROMPT` 加探索≤1 轮 + `max_iterations` 3→5

---

## 6. Token 计量（`UsageMixin`，CLAUDE.md 硬性约束）

| 字段 | 含义 |
|---|---|
| `session_token_usage.prompt_tokens` | **原始累计**（含 cache 命中）= LLM 真实处理上界 |
| `session_token_usage.completion_tokens` | 输出 token 累计 |
| `cost` | **实付**（用 `cachedTokens` + `cacheHitMultiplier` 折扣后） |
| `cachedTokens` | DeepSeek 服务端命中 cache 的 token 数（LLMResponse 字段） |

> **prompt_tokens 与 cost 独立**：审计完整性（看 LLM 真实在处理多少）vs 真实账单（看花了多少钱）分得清。

每次 LLM 调用（nl2sql / chart / answer / wasted / 重试 / 流式）都进 `_recordUsage`，
通过 `_summarizeUsage` 聚合。`chartCached` / `answerCached` / `multi-step aggregate`
全路径透传到 `_costFor`（`fa389bc`）。

---

## 7. 流式（`StreamMixin` + `ChatStreamOutputMixin`）

- `processMessageStream` / `persistInterruptedStream`：SSE 生成器 + 断连兜底
- `StreamPersistState.pending`：单发标志 = "已下发但未落库"
- `background=BackgroundTask(persistIfInterrupted)`：唯一可靠捕获断连的钩子
- `_chartStep` / `_recordChartUsage` 在流式路径同样带 `cachedTokens` + `cacheHitMultiplier`
- 多步流式：`_streamMultiStep` + `_streamQuery` 入口一次性读 multiplier，全 `_costFor`/`_costForSql` 透传

---

## 8. 安全与边界

- **SQL 只读**：`BusinessDbAdapter` + `_assert_read_only`，PG `SET TRANSACTION READ ONLY`，MySQL `SET SESSION READ ONLY`，Oracle best-effort（3 层纵深）
- **SQL Guard 侧信道防护**（`qa-system-sql-guard-side-channel`）：三集合分口径 + 引号族绕过 + 拒绝原因只回注原因
- **API 限流**：`@limiter.limit(rateLimitValue)`
- **审计日志**：每次 LLM 调用 + Agent 运行都写 audit
- **ACL**：X-User-* 防伪造 + `getOwnerActor` actor 派生（`qa-system-owner-acl-gate-not-binding`）
  > 例外：容器内 `nginx.conf` 会剥掉客户端 `X-User-*` 且 `AUTH_MODE=real`，现网身份由 JWT→DB 派生，
  > 不可伪造（stub 头仅测试环境可用）。
- **会话归属**：`api/v1/session_guard.py` 挂 6 个端点，403 不回显归属者；**fail-open 有存量缺口**，见 §4.2

---

## 9. 文件结构（7 mixin + 1 helper）

```
backend/app/services/
├── chat_service.py          # ChatService 编排（319 行 processMessage + 路由）
├── chat_usage.py            # UsageMixin（_costFor / _recordUsage / _summarizeUsage）
├── chat_context.py          # ContextMixin（会话上下文 + 查询状态）
├── chat_recall.py           # RecallMixin（本体召回 + 分层 + 扩边 + few-shot）
├── chat_multistep.py        # MultiStepMixin（拆步 + 追问 + 全局 filter）
├── chat_l4.py               # L4Mixin（Agent Loop 兜底）
├── chat_domain.py           # DomainCommandMixin（8+ 拦截：CLARIFY/SUPPLIER/Agent/METRIC...）
├── chat_stream.py           # StreamMixin（流式编排 + 断连兜底）
├── chat_stream_output.py    # ChatStreamOutputMixin（SSE 事件格式化）
└── chat_helpers.py          # _PipelineContext / _SqlOutcome / _RetryGenUsage / 工具
```

主服务类签名（`chat_service.py:257`）：

```python
class ChatService(
    RecallMixin,
    MultiStepMixin,
    StreamMixin,
    ContextMixin,
    UsageMixin,
    DomainCommandMixin,
    L4Mixin,
    ChatStreamOutputMixin,
):
```

---

## 10. 关键能力亮点（一句话总结）

| 维度 | 能力 |
|---|---|
| 意图覆盖 | 13 类全量接入（NL2SQL 主 5 + 概念 1 + 本体 3 + 领域 4） |
| 召回 | 向量 + 关键词 + 分层加权 + JOIN 扩边 + 相似历史 few-shot |
| NL2SQL | ReAct 两阶段 + REFINE 捷径 + 多步 + 全局 filter + 追问重写 + 失败隔离 |
| 兜底 | L4 Agent Loop（NL2SQL 失败时自动调度 Tool） |
| 图表 | 决策引擎 11 类（规则优先，LLM 只做歧义标签）+ 标准化 spec + 无颜色 option + 前端主题注入 |
| 多轮 | 服务端持久化 + 客户端降级 + 状态注入 + 预算控制 |
| 计量 | prompt_tokens / cost 双轨 + cache 命中折扣 + 全 LLM 调用覆盖 |
| 流式 | SSE + 单发标志 + `background=` 断连兜底 |
| 安全 | SQL Guard + 只读 SET + ACL + 限流 + 审计 |

---

## 11. 相关 memory

- [[qa-system-chat-service-file-split]] — 7 mixin + 1 helper 拆分原因
- [[qa-system-chat-service-assessment]] — 同目录评估版（缺陷侧）
- [[qa-system-chat-disconnect-persistence]] — H4 断连落库
- [[qa-system-chat-llm-router-keyless-500]] — 模型路由 503/404
- [[qa-system-multistep-global-filter]] — 多步全局 filter
- [[qa-system-follow-up-cascade]] — 追问级联 A/B/C
- [[qa-system-multistep-failure-isolation]] — C3/C4 多步失败隔离
- [[qa-system-llm-metering-blindspots]] — H1/H2/H8/H9 计量盲区
- [[qa-system-llm-failure-tokens]] — 失败路径用量
- [[qa-system-llm-prompt-cache-cost]] — Lever E prompt cache 账单
- [[qa-system-l4-iteration-budget]] — L4 预算
- [[qa-system-magic-number-governance]] — 魔数治理
- [[qa-system-feature-rule-config]] — Feature 规则引擎
- [[qa-system-sql-guard-side-channel]] — SQL Guard 侧信道
- [[qa-system-wiki-knowledge-layer-plans]] — Wiki 知识层三份计划（spec + P0/P1/P3 迁移链）
- [[qa-system-chat-session-restore]] — 刷新恢复上次会话 + 会话归属守卫铺开（§4.1 / §4.2）
- [[qa-system-chat-session-restore-broken]] — 该缺陷的原始诊断（指针恒为 null）
- [[wiki-ontology-link]] — wiki ↔ ontology 链接 + NL2SQL 业务规则注入（§3.6）

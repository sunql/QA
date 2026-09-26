# AI Chat 对话服务能力评估与改进建议

> 评估日期：2026-09-25　·　评估范围：`backend/app/services/chat_service.py` 及上下游对话服务
> 评估方法：对意图、检索、NL2SQL、多轮、多步、路由、计量、流式 8 个维度逐一读码审计，
> 全部结论附 `file:line` 证据，不依赖既有文档（文档已与代码漂移，见 §3.5）。

---

## 0. 一句话结论

对话服务已经是一条**能力齐全、安全护栏到位、计量意识强**的 L4 AI-Ready 流水线
（4 层路由 + 两阶段 NL2SQL + 13 类意图 + 多步拆解 + 全局过滤继承），但存在
**1 个已确认的运行时 TypeError 缺陷**、**9 处计量盲区（H1–H9）**、**多处向量/异常静默降级**
和**显著的文档-代码漂移**。当前短板集中在「流式路径与分步能力未对齐」「失败隔离
不彻底」「Token 计量有漏账」三块，而非能力缺失。

> **修复进度**：§2.1 的 C1（流式 TypeError）已于 2026-09-25 修复（见 §6）；
> LLM 可用性两个缺陷 D1/D2 与多步降级收尾 D3 已于 2026-09-26 修复并部署（见 §7）；
> §2.2 的 H1 / H2 / H8 / H9（四处「直调 LLM 绕过计量收口」）已于 2026-09-26 修复（见 §8）；
> §2.1 的 **C3 / C4**（多步硬失败隔离 + 重试上下文）已于 2026-09-26 修复（见 §9）；
> §3 的 **P0 第 5 项**（C 兜底分支的 global_filters 对称缺口）已于 2026-09-26 修复（见 §10）。
> **§2.1（CRITICAL）已全部清零；§3 的 P0 路线图亦已清空**。
> **仍待处理**：§2.2 的 H3–H7、§2.3 的 **M7**（重试错误只剩第一次，与本主题最相关的下一项）、
> 以及 §2.4 / §2.5 全部。

---

## 1. 能力全景（已具备）

### 1.1 意图理解（`intent_service.py`）

| 能力 | 实现 | 成本 |
|---|---|---|
| 13 类意图分类 | `QUERY / NEW_QUERY / REFINE / FOLLOW_UP / CLARIFY / DEFINE / MAP / METRIC / CHITCHAT / SUPPLIER_360 / SUPPLIER_RISK / GRAPH_REASONING / AGENT_RUN` | 纯关键词+正则，**零 LLM** |
| 省略式追问 | `isEllipsisFollowUp`（「4月份呢？」），疑问词前缀排除守 N6 | 零 LLM |
| 斜杠指令 | `/metric` `/define` `/map`，优先级最高 | 零 LLM |
| 显式多步守卫 | `rule_based_split` 命中即 NEW_QUERY，防 REFINE/FOLLOW_UP 误吞 | 零 LLM |
| Agent 语义路由 | `AgentRoutingService` 关键词评分：≥0.7 自动调度、0.4–0.7 建议卡片 | 零 LLM |
| 实体抽取 | 供应商编码（5-9 位）、维度/指标/图表类型、跳数、agent_code | 零 LLM |

### 1.2 语义检索匹配（类召回 + RAG）

| 能力 | 实现 |
|---|---|
| 类召回 | Milvus 向量 `topK=15` → 1-hop JOIN 扩边 → `max=30` 截断 → 层级排序 |
| 层级优先级 | `_LAYER_RANK`：ADS(0)>DWS(1)>DWD(2)>DIM(3)>ODS_DICT(4)>ODS_BUSINESS(5)>UNKNOWN(6) |
| ODS 治理 | 业务表默认排除、显式 `ODS_*` 表名解锁、维度词触发 DIM 全量 |
| ADS 加权 | 命中 `ADS_` 前缀类 `score × 1.5`（system_config 可调） |
| L1 KPI 匹配 | Jaccard 相似度（每 KPI 独立 `match_threshold` 0.75）+ 精确别名 1.0，缓存预热 |
| RAG 双通道 | doc_qa（`document_embeddings`）+ wiki_qa（`wiki_page_embeddings`），score=`1/(1+d)` |
| 辅助召回 | 词库 TermDictionary、特征目录 FeatureCatalog、值域采样 valueSampler、few-shot（top3） |
| 供应商名解析 | `supplier_name_resolver`（裸公司名规范化） |

### 1.3 NL2SQL 转换（`nl2sql_service.py`）

| 能力 | 实现 |
|---|---|
| 两阶段生成 | 计划 → `validatePlan` 纯代码校验（`maxPlanAttempts=2`）→ SQL |
| 4 层路由 | L1 KPI → L2 单 SQL → L3 CTE 链 → L4 Agent Loop（5 工具迭代） |
| SQL Guard | `business_db_pool._assert_read_only` + `_assertNoHiddenWrites`，仅 SELECT/WITH |
| 派生指标 | formula 必填校验（占比/比率/ratio/percent…） |
| 范围感知行数 | `_applyScopeRowLimit`：有范围不截断、无范围兜底 100 |
| REFINE 捷径 | `applyRefineDirect` 纯代码改写排序/筛选/行数，**零 LLM** |
| 准确性增强 | 跨类属性归属 hint、属性归一化（property ref normalize）、JOIN 等式归一化、schema digest、多方言（Oracle/MySQL/PG） |

### 1.4 多轮对话（状态 + 追问）

| 能力 | 实现 |
|---|---|
| 会话状态 | `session_query_state`（JSONB）存 last_plan/SQL/columns/turn_count |
| 状态注入 | REFINE/FOLLOW_UP 轮 `<previous_query_state>` 注入两阶段 prompt |
| 追问级联 A/B/C | A 省略式正则（零 LLM）/ B 改写+多步重跑 / C 短句不可回答兜底重试 |
| 历史上下文 | `_loadRecentRounds` 最近 10 条（5 轮）注入 NL2SQL + answer prompt |

### 1.5 分步执行拆解（多步 NL2SQL）

| 能力 | 实现 |
|---|---|
| 拆步 | 规则拆步（`第X步`/序数副词，零 LLM）+ LLM `plan()`（最多 4 子步） |
| 复合问题 | L1.5 启发式 `_looks_like_compound_question`（多动词并列 / 头+列表） |
| 跨步注入 | `[entity_list]`（≤50 行字符串列，2000 char）/ `[aggregate]`（600 char） |
| 全局过滤继承 | feat-multistep-global-filter A+B：措辞强化 + `[global_constraints]` 预抽取 |
| 汇总 | `StepAggregator` 聚合各步结果生成最终回答 |
| 不可变模式 | `StepExecutionContext/StepResult/GlobalFilters` 全 frozen dataclass + `with_step` |

### 1.6 模型路由 + Token 计量

| 能力 | 实现 |
|---|---|
| 路由 | 加权随机 + 成本阈值熔断 + 会话亲和（3 轮）+ 预算降级 + 最便宜 fallback |
| 可用性过滤 | `_usableModelConfigs`：自动路由候选池剔除构造不出客户端的配置（无 API key），判据复用 `createClient`（SSOT） |
| 降级重试 | `_callWithFallback`：`LlmClientError` → 记 zero-token → 降级重试一次 |
| 无可用 LLM | `client is None` → `LLMUnavailableError` → **503**（与 `doc_qa`/`wiki_qa` 同口径；2026-09-26 补齐，此前为裸 `AttributeError` 500） |
| 计量 | `TokenUsageService` + tiktoken（OpenAI/代理）/ 启发式（Ollama CJK） |
| 多 provider | OpenAI / Azure / 兼容代理 / Ollama，Fernet 加密凭据 |

> 关键边界：可用性过滤**不**覆盖「key 合法但 endpoint 不可达」（如本地 ollama 未起）——
> 那种情况 `createClient` 正常返回客户端，失败发生在调用期，交 `_callWithFallback` 处理。

### 1.7 流式 + 图表 + 数据摘要

| 能力 | 实现 |
|---|---|
| SSE 事件 | `meta/plan/sql/chart/token/done/error/qa_*/step_plan/step_result/class_recall` |
| 图表 | `ChartService` 规则推荐（PIE 行≤6 等）+ LLM 生成 ECharts option，异常回退规则 |
| 数据摘要 | `summarize_data`：≤100 行全量，>100 行 head/tail 5 行 + truncated |
| 不可回答 | 固定前缀 + `unanswerable_suggestion`（缺失术语 vs 本体词表，≤6 类） |

---

## 2. 存在的不足（按严重度分级，全部附证据）

### 2.1 CRITICAL —— 运行时正确性缺陷

| #   | 缺陷                                                                                                                                                                                                                                             | 状态                                             | 证据                                      |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- | --------------------------------------- |
| C1  | **流式 FOLLOW_UP 多步传参错误 → TypeError**。`_streamQuery` 在 FOLLOW_UP-上一轮多步分支调用 `_streamMultiStep(..., global_filters=gf2)`，但 `_streamMultiStep` 签名**无 `global_filters` 参数**。该路径（流式追问多步）必抛 `TypeError`，被外层 `except Exception` 吞成通用 internal error 事件。 | ✅ **已修复**（2026-09-25，见 §6）                     | `chat_service.py:3088` vs `3358-3370`   |
| C2  | **流式多步全局过滤完全缺失**。显式/复合多步的流式分支（`_streamQuery`）不调 `_resolveGlobalFilters`，`_streamMultiStep` 构建 `StepExecutionContext` 不含 `global_filters`。feat-multistep-global-filter 的 A/B 层在**整个流式多步路径全部失效**（非流式正常）。                                         | ✅ **已修复**（2026-09-25，见 §6）                     | `chat_service.py:3050-3076`、`3386-3391` |
| C3  | **多步失败隔离不覆盖硬异常**。仅「unanswerable plan」软失败被隔离；`_planAndGenerateSql` / `_runQueryWithRetry` 抛出的硬异常会中止整条多步序列，丢弃已完成步骤，**无部分结果恢复**。                                                                                                                  | ✅ **已修复**（2026-09-26，见 §9）                      | `chat_service.py:1783`、`1878-1887`、`3801-3808` |
| C4  | **多步执行错误重试用错问题**。`_runQueryWithRetry` 重试时用 `dto.question`（原始复合问题）而非 `step_plan.sub_question`，且 `priorState=None`，同时丢失子问题范围与跨步注入文本。                                                                                                             | ✅ **已修复**（2026-09-26，见 §9）                      | `chat_service.py:3790-3795`                    |

> C1/C2 是 feat-multistep-global-filter 的「半接线」遗留：只接通了非流式
> `_executeMultiStep`，流式 `_streamMultiStep` 的签名未同步、流式入场点未接
> `_resolveGlobalFilters`。属当前工作区未提交代码中的真实缺陷。

### 2.2 HIGH —— 计量盲区与静默降级

| # | 缺陷 | 状态 | 证据 |
|---|---|---|---|
| H1 | **全局过滤抽取 LLM 调用不计量**。`extract_global_filters` 直调 `client.complete` 但只返回 `GlobalFilters`（无 token 数）；`_resolveGlobalFilters` 写 0-token 假 marker，注释「token 已计到 plan/split 路径」**与实际不符**。 | ✅ **已修复**（2026-09-26，见 §8） | `step_query_planner.py:201-214`、`chat_service.py:1879` |
| H2 | **L4 Agent Loop 不写 session_token_usage**。`run_agent_loop` 每次迭代 `complete_with_tools` 不计量，只用**硬编码 gpt-4o-mini 价格**估算 USD，token 数丢失。 | ✅ **已修复**（2026-09-26，见 §8） | `agent_runtime_service.py:327-336`、`chat_service.py:758` |
| H3 | **历史注入无 token/字符预算**。`_buildContextPrompt` 全文拼接 `session_message.content` + `[SQL:...]`，无截断；一条超长 SQL/答案会无限膨胀后续每次 NL2SQL + answer prompt。 | ⬜ 待处理 | `chat_service.py:4124-4144` |
| H4 | **客户端断连无处理**。`CancelledError`（`BaseException`）不被任何 `except` 捕获；`_storeSessionMessages` 只在流尾执行，断连即丢整轮（无部分答案、无历史落库）。 | ⬜ 待处理 | `chat.py:59-67`、`chat_service.py:2904-2922`、`3303` |
| H5 | **向量失败静默降级到全 schema**。类召回异常回退 `return list(allClasses)`，**不过滤 ODS、不截断 max**——正是召回剪枝要解决的老问题在 Milvus/embedding 挂掉时原样回来。 | ⬜ 待处理 | `chat_service.py:1269-1280`、`1352-1359` |
| H6 | **3 处 score 公式不一致（DRY 违反）**。`embedding_service`/`ontology_service` 有 `round(,4)` + `max(0)`；`wiki_vector_service` 无 round；`rag_service` 无 `max(0)` 无 round——负距离得 score>1，缺 `distance` 键直接 `KeyError`。 | ⬜ 待处理 | `embedding_service.py:29-31`、`ontology_service.py:1198`、`wiki_vector_service.py:264`、`rag_service.py:299` |
| H7 | **provider_type 死元数据**。`embedding_provider_factory` 从不读 `provider_type`，全部当 OpenAI 兼容；维度守卫只对 DB-provider 路径生效，env 回退路径不校验。 | ⬜ 待处理 | `embedding_provider_factory.py:48-61` |
| H8 | **doc_qa 不写 token ledger**（wiki_qa 写），违反「每次 LLM 调用必须计量」约束；且无命中时 doc_qa 存 `citations=[]` 而 wiki_qa 存真实 citations，行为不一致。 | ✅ **已修复**（2026-09-26，见 §8） | `wiki_qa_service.py:215-224` vs `rag_qa_service.py:161-162` |
| H9 | **成本单位不一致**。`supplier_risk_service._generateRiskPoints` 用硬编码 **CNY** 0.001/0.002，系统其余用 config 的 **USD**。 | ✅ **已修复**（2026-09-26，见 §8） | `supplier_risk_service.py:275-280` |

> H1/H2/H8/H9 同一根因：**直调 `client.complete(...)` / `complete_with_tools(...)`
> 绕过计量收口**，各自违反核心约束 #3。四处一次修完，SSOT 见
> [`Harness/changes/fix-llm-metering-blindspots/`](../../changes/fix-llm-metering-blindspots/summary.md)。
>
> 修 H9 带来一处**必须知会的行为变更**：供应商风险的 LLM 路径此前在生产上从未跑过
> （`createClient(None)` 恒返回 `None` → AttributeError 被吞 → 永远是模板文案），
> 修复后只要解析得出配置就真调 LLM ⇒ 该路径的**延迟与成本从 0 变为非 0**，文案也会变。
> 受影响：`supplier_risk` 意图、`supplier_risk_agent` 工具。

### 2.3 MEDIUM —— 健壮性与可维护性

| # | 缺陷 | 证据 |
|---|---|---|
| M1 | **SQL Guard 侧信道函数未覆盖**：缺 `pg_sleep`/`pg_advisory_lock`/`dblink`/`SLEEP`/`BENCHMARK`/`LOAD_FILE`/`UTL_HTTP`；`INTO` 过度拦截（含良性 `SELECT…INTO`）；sqlparse 跳过 `Literal/Comment` 的隐患。 | `business_db_pool.py:60-72`、`148-161` |
| M2 | **SQL Guard 拒绝反馈不具体**：LLM 只见「未通过安全校验」，无法自愈守卫违规。 | `nl2sql_service.py:2076-2082` |
| M3 | **`QueryPlan.from_dict` 吞所有解析错误**：损坏输入静默变空 tuple，掩盖根因（文档明言「绝不抛错」）。 | `query_plan.py:203-206` |
| M4 | **无同模型瞬态重试**：`generateQueryPlan`/`generateSql` 内部不捕获瞬态 LLM 异常，只靠模型 fallback（换模型≠同模型重试）。 | `nl2sql_service.py:1608`、`2039` |
| M5 | **L3 CTE 引擎是死代码**：`_executeChainedSteps`/`_executeSingleChainedStep` 仅测试引用，无生产接线，且含未计量 LLM 调用。 | `chat_service.py:1935-2023` |
| M6 | **重复定义**：`_buildOptionPrompt` 两次（`chart_service.py:121/226`，同名同签名同注释，后者静默覆盖前者——前一份是死代码）。<br>⚠️ **2026-09-26 复核修正**：本行原写「`_consumedTokens` 两次（`chat_service.py:4048/4052`）」**不成立**——当前只剩一个定义（`chat_service.py:4249`），行号也对不上；评估当日应是笔误或事后已清理。 | ⬜ 待处理 | `chart_service.py:121`、`226` |
| M7 | **`_runQueryWithRetry` 丢弃第二次错误详情**：重试失败 re-raise `firstErr`，实际最终错误被丢。⚠️ **2026-09-26 部分修复**（C3/C4 批，见 §9）：重试的**生成 token** 已随异常交回并落账、重试的**新错误**已 `logger.warning` 留痕（此前是裸 `except` 静默吞掉）；**用户可见文案仍只报第一次错误**（有意：那是本步骤首次 SQL 的病因），单步路径仍未取 token 记账。 | `chat_service.py` `_runQueryWithRetry` |
| M8 | **`prior_cte` 契约不一致**：docstring 说可无 `WITH`，但 `_assert_read_only` 会拒绝无 `WITH` 形式（当前仅 `render_prior_cte` 输出可过，潜伏）。 | `nl2sql_service.py:414-416` vs `business_db_pool.py` |
| M9 | **Milvus 16384 上限**：`listAllEmbeddings` 截 16384，超量后对账 diff 会算错（无 guard）。 | `milvus_client.py:245-265` |
| M10 | **KPI 缓存线性扫描**：`findByAnyKeyword` O(keywords×catalog) 无倒排索引，随目录增长退化。 | `kpi_match_cache.py:79-87` |

### 2.4 LOW —— 优化项

- 供应商名 `ilike %name%` 无 trigram 索引（3500 行 seq scan，`supplier_name_resolver.py:151`）。
- `_callWithRetryBackoff` 末尾 `raise RuntimeError("unreachable")`（死分支）。
- 大量编译期魔数（topK=15、max=30、2000/600 char、阈值 0.3、5 轮等），仅部分已 `system_config` 化。

### 2.5 文档-代码漂移（独立成节，因影响后续开发）

| 漂移点 | 现状 |
|---|---|
| 文档引用 `sql_guard.py` | 实际无此文件，SQL Guard 在 `infrastructure/business_db_pool.py`（`architecture.md`/`agent-loop.md`/`nl2sql-engine.md` 均引用） |
| `nl2sql-engine.md` 写「5 类活跃意图」 | 实际 `IntentType` 已 13 类（新增 supplier_360/risk/graph_reasoning/agent_run 等 4 条领域拦截路径） |
| `IntentType` docstring 写「DEFINE/MAP/METRIC 暂未接入流水线」 | 实际已接入 `_handleDefineMetric/_handleDefineClass/_handleShowMetric/_handleMapProperty` |
| `config.py` Settings 字段重复定义 | `bcryptRounds`(12 vs 10)、`jwtSecret`、`jwtTtlSeconds`(3600 vs 86400)、`dbPoolSize`、`authMinDelayMs` 均定义两次且默认值不同，后者覆盖前者 |
| `nl2sql-engine.md` 4 层路由「L3 有触发条件」 | 实际 L3 `_executeChainedSteps` 无生产调用者，是死代码 |

---

## 3. 提升建议（按优先级路线图）

### P0 —— 正确性修复（建议立即，先于任何新功能）

1. ~~**修 C1/C2（流式全局过滤）**~~ ✅ **已完成（2026-09-25）**，见 §6。
2. ~~**修 C3（多步硬失败隔离）**~~ ✅ **已完成（2026-09-26）**，见 §9。
3. ~~**修 C4（重试子问题）**~~ ✅ **已完成（2026-09-26）**，见 §9。
4. ~~**补齐 H1/H2 计量**（`extract_global_filters` 返回 token 数并落 `session_token_usage`；
   `run_agent_loop` 返回 token 数、按实际模型 config 计价；统一 H9 成本单位为 USD）~~
   ✅ **已完成（2026-09-26）**，连同 H8 一次修完（四处同一根因），见 §8。
5. ~~**补 C 兜底分支的 global_filters 对称缺口**（P0 唯一剩余项）：流式/非流式
   `_isFollowUpRetryCandidate` 命中后经 `_prepareFollowUpMultiStep` 走多步的路径均未传
   `global_filters`（两侧现状一致，属对称缺口）。抽一次 `_resolveGlobalFilters(dto2)` 即补齐。~~
   ✅ **已完成（2026-09-26）**，见 §10。修法比本行原建议更彻底：抽取收敛进共享前置
   `_prepareFollowUpMultiStep`（4 个入场点改为纯透传）—— 根因是「同一份多步前置知识被复制到
   4 个入场点、靠人记得传参」，而它已经漏过两次（C1/C2 流式那次 + 本次 C 兜底两侧）。
   ⚠️ 与本批同源的 **M7**（重试错误只剩第一次；C3 已让它变成用户可见文案）**仍待处理**，见 §9 遗留 1。

### P1 —— 健壮性（本季度）

5. **H5 向量失败降级**：类召回异常回退时仍应用 ODS 过滤 + `CLASS_FILTER_MAX_CLASSES`
   截断，并记 warning（当前是全量裸返回）。
6. **H3 历史预算**：`_buildContextPrompt` 增加 token/字符截断（如 `_clip_text` 复用），
   防止长轮次膨胀 prompt。
7. **H4 断连处理**：捕获 `CancelledError`，持久化已产出的部分答案与消息，落库后再退出。
8. **H6 score 公式统一**：抽一个 `distanceToSimilarity(d)` 单源函数，四处复用；
   缺 score 键时显式按 0 处理并记日志（消除 NaN%/静默 not-found）。
9. **M1/M2/M3 SQL 安全加固**：补侧信道函数黑名单；守卫拒绝把具体原因注入重试反馈；
   `QueryPlan.from_dict` 改为严格解析（损坏即抛，让上层重试而非静默丢弃）。

### P2 —— 可维护性（清理欠账）

10. **文档-代码对齐**（§2.5 全部）：更新 `nl2sql-engine.md`/`architecture.md`/`agent-loop.md`
    的 `sql_guard.py` 引用与 13 类意图；修正 `IntentType` docstring；收敛 `config.py`
    重复字段定义。
11. **M5 死代码清理**：L3 CTE 引擎要么接线（承接 `requiresCte` 场景）要么删除，
    避免带未计量 LLM 调用的死代码长期驻留。
12. **M6 重复定义清理**：只剩 `_buildOptionPrompt` 一处（`_consumedTokens` 经 2026-09-26 复核
    已无重复，见 §2.3 M6 修正）。
13. **拆大文件/大函数**：`chat_service.py`(4353)、`nl2sql_service.py`(2431) 远超 800 行；
    `_streamQuery`(335)、`_handleAgentRun`(216)、`validatePlan`(143) 等按职责拆。
14. **魔数治理**：把 topK/max classes/char 上限/阈值等编译期常量下沉 `system_config`，
    与已治理的 `CLASS_FILTER_MAX_CLASSES`、`ADS_RECALL_WEIGHT` 保持一致。

### P3 —— 规模化（类库增长后，出现真实案例再做）

15. **类召回升级**（`nl2sql-engine.md` 已记录，勿提前）：调大 topK / 多路召回 /
    两阶段检索（宽召回 50 → LLM/cross-encoder 精排 30）。
16. **M9 Milvus 16384 上限**：分页对账或加 guard 断言。
17. **M10 KPI 倒排索引**：catalog 增长后 `findByAnyKeyword` 换倒排。
18. **H7 provider_type 生效**：真正支持非 OpenAI 兼容 embedding provider。

---

## 4. 建议的开发守则（给后续迭代）

1. **所有新 LLM 调用点必须走 `_callWithFallback` + `_recordUsage`**。本次审计发现的
   计量盲区（H1/H2/L3）全是「直调 `client.complete` 绕过计量」这一反复出现的模式。
2. **流式与非流式两套路径改动必须成对做**。C1/C2 的根因是只改了非流式
   `_executeMultiStep`、漏了流式 `_streamMultiStep`——新增多步能力时先问「两条路径都接了吗」。
3. **提示注入复用既有渲染器**（`_renderStatePart`/`inject_to_prompt`/`_sanitizeContext`），
   不手写新注入片段，避免方括号/尖括号转义这类已踩过的坑。
4. **不可变模式沿用**：frozen dataclass + `replace`/`with_step`，禁止原地改。

---

## 5. 关联文档

- `Harness/wiki/nl2sql-engine.md` — NL2SQL 引擎设计（含 4 层路由、召回窗口升级路径）
- `Harness/wiki/model-router.md` — 模型路由与 Token 计量
- `Harness/wiki/agent-loop.md` — L4 Agent Loop
- `Harness/wiki/architecture.md` — 系统架构
- `Harness/changes/feat-multistep-global-filter/summary.md` — 全局过滤继承 SSOT（C1/C2 的根因来源）
- `Harness/changes/feat-follow-up-cascade/` — 追问级联 A/B/C

---

## 6. 修复记录：C1/C2（2026-09-25）

### 根因

`feat-multistep-global-filter` 只接通了**非流式** `_executeMultiStep`，流式 `_streamMultiStep`
的接线缺三处（缺一即断）：

| 缺失项 | 现象 |
|---|---|
| 签名缺 `global_filters` 形参 | FOLLOW_UP 分支传参 → `TypeError` |
| `StepExecutionContext` 构造未赋值 | 即便传参也注入不到 plan prompt |
| 流式显式/复合分支未调 `_resolveGlobalFilters` | 从不抽取全局约束 |

注意 `_streamMultiStep` 内部**已**写 `global_filters=ctx.global_filters`——即上一轮只写了
「消费端」，没写「生产端」，故该表达式恒为 `None`，且形参缺失导致传参崩溃。

### 修复（`app/services/chat_service.py`，4 处）

1. `_streamMultiStep` 签名补 `global_filters: GlobalFilters | None = None`
2. `_streamMultiStep` 的 `StepExecutionContext(...)` 补 `global_filters=global_filters`
3. `_streamQuery` 显式多步分支补 `_resolveGlobalFilters` + 透传
4. `_streamQuery` 复合多步分支补 `_resolveGlobalFilters` + 透传

设计取向上**直接复用非流式同一 helper 与同形签名**，不新写一套流式专用逻辑，避免再次漂移。

### 验证（TDD：先 RED 后 GREEN）

新增 `app/tests/integration/test_multistep_global_filter.py::TestMultistepGlobalFilterStreaming`
3 用例：

| 用例 | 守约 |
|---|---|
| `test_stream_explicit_multi_step_injects_global_constraints` | 流式 3 步 plan prompt 均含 `[global_constraints]` |
| `test_stream_explicit_multi_step_calls_extractor_once` | 流式路径真正调抽取器且仅一次 |
| `test_stream_follow_up_multistep_no_type_error` | 流式追问多步无 error 事件（C1 回归锁） |

RED 阶段实测复现原始 `TypeError`：
`ChatService._streamMultiStep() got an unexpected keyword argument 'global_filters'`。

回归结果：
- `test_multistep_global_filter.py` 7 passed（4 旧 + 3 新）
- 针对性集成（multi_step / follow_up_cascade / stream_api / streaming_detached / service_state）**56 passed**
- 相关单测（multi_step_plan / step_query_planner / nl2sql_service / chat_service_stream / intent_service）**287 passed**

### 遗留

- 全量集成套件因耗时长（>17 分钟未跑完，含 Milvus 慢路径）被中止，**未取得全绿证据**；
  已用针对性套件覆盖本次改动路径。
- ~~新增代码尚未部署~~ → **已于 2026-09-26 随 §7 一并部署**（容器 `qa-backend` md5 与源码全等）。

---

## 7. 修复记录：D1/D2（2026-09-26）

排查追问级联回归时顺带发现的**两个独立缺陷**（详细 SSOT 见
[changes/fix-chat-llm-keyless-and-degrade](../../changes/fix-chat-llm-keyless-and-degrade/summary.md)）。

### 根因

| 编号 | 缺陷 | 触发条件 | 修复前形态 |
|---|---|---|---|
| D1 | 路由分支 7「超预算 → `_cheapest(active)`」选中**无 API key** 的配置 | `sessionCost >= SESSION_BUDGET` 且最便宜配置无 key | 裸 `AttributeError` → **500** |
| D2 | chat 流水线缺 `client is None` 兜底（`doc_qa`/`wiki_qa` 都有） | 候选全部构造不出客户端 | 通用 `except` → **internal 错误** |
| D3 | `_streamMultiStep` 降级分支不落库、不存查询状态 | 所有步骤都非 `aggregation_only`（**当前生产不可达**） | 悬空 user turn + `last_question` 留旧值 |
| D4 | 可用池过滤遍历每个配置调 `createClient`，未隔离 `ConfigError` | DB 有**单条**密文损坏配置（跨环境 restore / 手工改库） | **400**「API Key 密文无法解密」——所有自动路由请求 |

> D4 是 D1/D2 修复**引入的回归**，由 code-reviewer 复审抓出：改动前自动路由只解密「被选中」的那一个
> 配置，改动后遍历全部 ⇒ 失败面从 1 放大到 N。修法：逐配置 `try/except ConfigError` 按「不可用」筛掉。
> 通用教训——**把「1 次调用」扩成「N 次遍历」时，每一点都要能独立失败而不拖垮整条路径**。

本地触发土壤（`llm_config`）：

```
 id |    model_name    | is_active | has_key | cost_per_1k_input
  1 | deepseek-chat    | t         | t       |          0.001400
  3 | Qwen3.8-27B-4bit | t         | f       |          0.000000   ← 最便宜且无 key
```

**曾误判**：「前端始终下发 `modelId`，故聊天路径不受影响」。实际 `chatStore.ts:145`
的 `selectedModelId` 初值 `null` 且**不持久化**、`ChatPanel` 无自动选中 → **刷新页面后
前端自己就不带 `modelId`**，只要该会话成本已过预算线即 500。

### 修复（`app/services/chat_service.py`）

1. 新增 `_usableModelConfigs`：自动路由候选池剔除 `self._llmFactory(c)` 构造不出的配置
   （含**逐配置 `try/except ConfigError`** 隔离，见 D4），剔除时打日志；全部不可用时回退
   原列表（让兜底给 503 而非 `NoAvailableModelError`）。显式 `modelId` **仍按全量配置
   查找**，保 404 语义不被污染。`_PipelineContext.configs` 同步收窄 → fallback 也选不中它。
2. `_buildPipelineContext` 补 `if client is None: raise LLMUnavailableError(MSG_LLM_UNAVAILABLE)`。
3. 抽取 `_finalizeMultiStepDegrade` 供流式/非流式**共用**（落库 + `_saveQueryState` +
   如实报告已完成数据步骤数），消除「只改一条路径」的偏差。
4. `_resolveL4LlmClient` 的工厂调用包 `try/except ConfigError → None`，兑现其
   「可降级、不报错」的 docstring 契约。
5. `docker/.env` `SESSION_BUDGET` 0.1 → 1.0（对齐代码默认值）。
6. 降级收尾的两条文案移入 `messages_zh.py`（`MSG_MULTI_STEP_DEGRADE_PARTIAL/FAILED`）。

### 验证（TDD：先 RED 后 GREEN）

- 新增 `test_chat_model_routing_fallback.py` **5 用例**：RED 阶段 3 例以**生产的同一错误**
  （`AttributeError: 'NoneType' object has no attribute 'complete'`）失败；
  GREEN 断言含 `errorType == domain` 与具体文案，**不**只看「最后是 error 事件」——
  后者会被通用 `except`（`errorType=internal`）蒙过去。
  第 5 例（D4）RED 实测复现 `400 {"error":"API Key 密文无法解密，可能密钥已变更"}`，
  假工厂走**真实 `decryptApiKey`**、只 stub 网络客户端，故异常由生产代码真实抛出。
- 新增 `test_chat_multi_step.py::TestNoAggregationStepDegrade` 2 用例（流式 + 非流式）。
- 集成 74 passed；单元 376 passed / 1 failed（预存 `TestSearchByKeywordAdsWeighting`，
  stash 本次改动后复跑同样失败，已确认无关）。

### code-reviewer 复审

CRITICAL 0 / **HIGH 1（D4，已修）** / MEDIUM 0 / LOW 2（文案集中已修；「可用池 = 可构造
而非可连通」无需改，已在 docstring 声明）。审查并确认三项设计成立：`usable or configs`
回退链路闭合、显式 `modelId` 的 404 语义未被 503 掩盖、两条降级路径参数口径一致且无重复落库。

### 真机（`docker compose build backend && up -d backend`）

| 场景 | 结果 |
|---|---|
| 不带 `modelId` 调 `/chat` | **200**, `modelName=deepseek-chat`，返回 THBI Oracle 真实数据（385 家供应商 / 合计 17.84 亿） |
| 日志证据 | `自动路由剔除 1 个无可用 key 的模型配置（候选 3 → 2）` |
| 显式 `modelId=3`（无 key）非流式 | **503** `未配置可用的 LLM，无法回答该问题` |
| 同场景流式 | `event: error` / `errorType: "domain"` |

### 教训

- **同一契约在多条流水线里的实现要对齐**：`doc_qa`/`wiki_qa` 已有的 `None → 503`
  兜底，chat 漏了；「参照实现也要逐条比」比「看起来都在」可靠。
- **「部署了」≠「生效」**：本次既改代码又改 `.env`，只 `docker cp` 改不了环境变量，
  必须 build + up；验证用容器内 `printenv` + `md5sum` 双向核对。
- 记录缺陷时**先验证可达性再定级**：D3 写进评估时按「潜在」记录（生产不可达），
  避免把未验证的推断当活 bug 汇报。

---

## 8. 修复记录：H1 / H2 / H8 / H9（2026-09-26）

四处同一根因：**直调 `client.complete(...)` / `complete_with_tools(...)` 绕过计量收口**，
各自违反核心约束 #3「每次 LLM 调用必须记录 Token 消耗与成本」。SSOT 见
[`Harness/changes/fix-llm-metering-blindspots/`](../../changes/fix-llm-metering-blindspots/summary.md)。

### 根因

跨模块调用时 token 数**没有出口**：`extract_global_filters` 只返回 `GlobalFilters`；
`AgentLoopResult` 只有一个 USD 数字、无 token 字段（且该数字是 `_estimate_cost` 按
gpt-4o-mini 硬编码估的）；`rag_qa_service` 未接 `_recordUsage`；`supplier_risk_service`
把单价硬编码在自己内部（还是 CNY）。只有 `chat_service` 有落库收口。

后果不止「漏账」：同一张 `session_token_usage` 台账里混着**三种成本口径**
（config USD / gpt-4o-mini 估算 USD / 硬编码 CNY），而会话成本报表与
`ModelRouterService` 的预算降级判断都在读这张表。

### 修复

1. **token 数随返回值上行，成本在收口处算**：`AgentLoopResult` /
   `_AgentLoopStepResult` 增 `prompt_tokens` / `completion_tokens`；`_usage_tokens`
   取数、`_cost_for_usage` 复刻 `chat_service._costFor` 的 USD 公式（`Decimal` 全程）；
   三个 return 分支（含 `cost_cap`——那一轮的钱已经花了）都带 token。
2. **工厂与配置成对**：`SupplierRiskService.assess(..., llm_config=None)`；
   降级判据扩为「工厂或配置缺失 / `factory(cfg)` 返回 None」三者皆走**显式**降级
   （`logger.warning` + `fallback_template` + 归零），不再靠 AttributeError 被吞。
   `AgentToolContext` 相应增 `llm_config`。
3. **成本可降级契约**：`_resolveChatLlmClient`（原 `_resolveL4LlmClient`）对 L4 /
   供应商风险 / agent_run 三个调用方统一「解析不出就返回 `None` 由调用方降级」。

### 验证（TDD：先 RED 后 GREEN）

| 项 | RED 证据 | GREEN |
|---|---|---|
| H1 | 台账无 `multistep_global_filter` 行 | 61 passed |
| H2 | 台账 0 行；成本 `1.2e-05`（硬编码）vs 期望 `6e-05`（config 单价） | 2 passed（新增 `test_l4_agent_loop_metering.py`，走完整 `POST /api/v1/chat`） |
| H8 | 台账零行 + 低分场景 `citations=[]` | 13 passed |
| H9 | **诚实假工厂**（`cfg is None → None`，与 `createClient` 同语义）下 `riskPointsSource == "fallback_template"` | 2 passed + 回归 18 passed + 单元 49 passed |

集成回归（9 个文件，60 passed）：`test_chat_multi_step` / `test_chat_follow_up_cascade` /
`test_chat_model_routing_fallback` / `test_multistep_global_filter` / `test_doc_qa_api` /
`test_supplier_risk_api` / `test_supplier_risk_llm_metering` / `test_chat_agent_run` /
`test_l4_agent_loop_metering`。

全量单元套件 `2 failed, 2239 passed`，两个失败**已证明预存**（在 HEAD `4e84753` 的临时
worktree 中单独复跑同样失败）：ADS 加权重排被 `_rankByLayer` 覆盖（见 M6 邻近说明，
加权逻辑实际已是死代码）、`test_dependencies.py` 的 `Header.lower()` 版本漂移。

### 本批被回归测试抓出的问题（值得单独记）

H1 把一个**真实存在的第 5 次 LLM 调用**（B 层全局过滤抽取）纳入计量后，
`test_chat_multi_step.py` 里三处 `assert sorted(r.purpose for r in usages) == [...]`
**精确列举**了 purpose 集合，于是全红。这**不是**测试该改——LLM 确实调用了、token
确实花了，台账就该有这一行；错的是断言停留在旧调用序。修法是补上新 purpose，
并顺手加断言「该行 token > 0」，把「真花了钱」而不是「有个字符串」钉住。

> 教训：**精确列举型断言在「新增一次调用」时必然失败**，这本身是好事（它逼你看清
> 新增的调用有没有被计量）；但前提是每次改完一处计量，**必须重跑隔壁的集成套件**，
> 而不是只跑新写的那一个文件——本次 H1 的绿灯只覆盖了新建的
> `test_multistep_global_filter.py`。

### code-reviewer 复审

审出 3 条（0 CRITICAL / 1 HIGH / 1 MEDIUM / 1 LOW），全部当场处理：

| 级别 | 问题 | 处理 |
|---|---|---|
| HIGH | **L4 中途失败时已花掉的 token 凭空消失**：`run_agent_loop` 的 while 体无异常包容，任一抛错就穿透到 `_runL4AgentLoop` 的 `except`（`return None` 在 `_recordUsage` **之前**），前面若干轮的累计值随之丢失。征兆很典型：`terminated_reason="error"` 被文档写着、被 `_maybeRunL4AgentLoop` 检查着，却**从未被真正产生过**——异常旁路取代了它。 | while 体包 try/except，异常收敛为 `terminated_reason="error"` 后正常返回累计值；行为不变（照旧降级 L2/L3），只是账不再丢。新增 `test_l4_mid_loop_failure_still_meters_spent_tokens`（RED：`实际 0 行`） |
| MEDIUM | doc_qa 低分短路的 citations 落库**无测试**（H8 把 `[]` 改成真实 citations，但只有 SSE 事件被断言过） | 补 `test_answer_stream_low_score_persists_real_citations`，并在 HEAD worktree 上确认它**失败**（旧代码 `[] or None` → None），是真闸；行为保留（与 wiki_qa 对齐）。⚠️ 留一条产品待裁：模板说「未找到相关依据」却挂着低分 citations，`wiki_qa` 同样如此，属既有设计 |
| LOW | doc_qa 台账写 0/0 假行（退化流不报 usage 时），与其余三处计量点的零用量守卫不一致 | `if total_pt or total_ct:` 包住落库；新增 `test_answer_stream_zero_usage_writes_no_ledger_row`（RED：`total=0 cost=0.000` 行存在） |

### 遗留

1. 供应商风险台账行的 `model_config_id` 仍为 `NULL`（走 `_recordDirectUsage`，该助手
   按设计写 `None`）。改走 `_recordUsage` 会更一致，但 `_recordUsage` **不跳过 0 token**，
   降级路径会写出 0/0 假行——正是 H9 第二条用例明确禁止的。需先给它加零用量守卫。
2. **H9 是一处必须知会的行为变更**：供应商风险的 LLM 路径此前在生产上从未跑过
   （`createClient(None)` 恒返回 `None`），修复后只要解析得出配置就真调 LLM ⇒
   该路径**延迟与成本从 0 变为非 0**，文案也会变。受影响：`supplier_risk` 意图、
   `supplier_risk_agent` 工具。
3. 产品待裁（复审 MEDIUM 带出）：doc_qa / wiki_qa 在「无相关依据」模板下仍持久化低分
   citations，文案与依据自相矛盾。两个 service 要改一起改。

## 9. 修复记录：C3 / C4（2026-09-26）

§2.1（CRITICAL）最后两项，同一处结构问题的两个面：**多步序列里没有单一的执行出口**。
SSOT 见 [`Harness/changes/fix-multistep-failure-isolation/`](../../changes/fix-multistep-failure-isolation/summary.md)。

### 根因

`_executeMultiStep`（非流式）与 `_streamMultiStep`（流式）各自复制了一份「生成 → 执行」循环，
且**只隔离了软失败**：

```python
if outcome.sql is None or outcome.plan is None or outcome.plan.isUnanswerable:
    completed.append(StepResult(..., sql=None, error="无法回答（LLM 判定无有效查询计划）"))
    continue
data, final_sql, retry_tokens = await self._runQueryWithRetry(...)   # ← 抛错就没人接
```

- **C3**：`_planAndGenerateSql` 抛错、或 `_runQueryWithRetry` 执行 + 回灌重试均失败时，异常穿透整条
  序列到 API 层（非流式 500 / 流式 internal 错误事件），**已完成步骤的数据与已消耗 token 全部作废**。
- **C4**：重试路径绕过 `_planAndGenerateSql`，自己直接 `generateSql(dto.question, ..., priorState=None)`
  ⇒ 丢掉**子问题范围**与**跨步注入文本**（如「第二步要引用的前三个供应商」），与本步首次生成口径不一致，
  重试出的 SQL 可能重新对齐回整个复合问题。

### 修复

1. **抽共享 helper `_executeDataStep`**（流式/非流式同源）：内部完成生成 → 执行 → 回灌重试、
   token/cost 累计、`_spawnEmbedding`、`_recordUsage`，并把**所有**步骤级硬失败收敛为
   `_StepRun(result=StepResult(error=..., sql=None), tokens=..., cost=..., modelName=..., plan=...)`。
   两条循环只剩「记账 + 更新锚点 + （流式）发事件」，结构上无法再漂移（本项目第三次踩这个模式）。
2. **`sql is None` 确认为唯一失败标记**：与既有 `_finalizeMultiStepDegrade` 的
   `succeeded = [r for r in completed if r.sql is not None]` 同口径，于是降级文案的「完成 N/M 步」
   自动正确；新增 `_hasDataStepResult` 复用同一判据。
3. **全部数据步骤失败则跳过汇总 LLM**（`continue` 而非 `break`，不依赖「汇总步在末尾」的隐含契约），
   落到既有降级收尾 `MSG_MULTI_STEP_DEGRADE_FAILED` —— 汇总 LLM 拿到的只有错误行时只会编造结论。
4. **失败步骤不再污染下游 prompt**：`inject_to_prompt` 与 `StepAggregator._build_prompt` 对
   `sql is None` 的步骤改渲染「无数据 / 不得为其推测」。**空数据块与「查询结果为 0」在下游 LLM
   眼里无法区分**——照常渲染 `[aggregate] []` 会得到「2025 年销售额为 0」这种看似合理、实则编造的结论，
   那样「失败隔离」只是把 500 换成了更隐蔽的错答案。
5. **C4 用显式 kw-only 参数**：`_runQueryWithRetry(..., *, question=None, prior_state=None)`，
   多步调用方传子问题 + 注入文本；`question is not None` 同时作为「多步语境」判据决定是否把主问题
   透传为 `scopeQuestion`。单步调用方不传 ⇒ 逐字节不变。

一处**刻意的行为变更**：步骤错误文案进入用户可见响应（`steps[i].error`），
`该步骤查询生成失败：…` / `该步骤执行失败：…` + 截断 200 字符。选择暴露而非隐藏，是因为
「哪一步失败了、为什么」是多步场景下用户唯一能自我纠正的信息；截断 + `_summarizeExecutionError`
避免把整段堆栈或连接串写进响应。

### 验证（TDD：先 RED 后 GREEN）

| 用例 | RED 证据 | GREEN |
|---|---|---|
| 非流式步骤失败隔离 | `RuntimeError: ORA-00942` 从 API 层穿出（500） | 200 + 步骤 2 `error` 非空 / `sql`·`data` 空 + 步骤 1 完整 + 台账 5 行 + `state.last_sql == steps[0].sql` |
| 流式步骤失败隔离 | 流出 `error` 帧（断言 `'error' == 'done'` 失败） | 无 `error` 事件 + 末帧 `done` + 2 条 `step_result`（0 成功 / 1 失败） |
| 全失败跳过汇总（流式 / 非流式） | —（新增能力） | 汇总 LLM **未**被调用 + `answer == MSG_MULTI_STEP_DEGRADE_FAILED` |
| 重试上下文（C4） | 断言 `'请分步查询 2024 和 2025 年的销售额并对比' == '2025年的销售额是多少'` 失败 | 重试调用 `question == 子问题 != 复合问题`、`priorState` 含「前序步骤结果」、`scopeQuestion == 主问题` |
| 失败步骤注入渲染（单元） | 渲染出 `[aggregate] []` | 「该步骤执行失败」可见 + 无 `[/aggregate]` / `[/entity_list]` |
| 失败步骤汇总 prompt（单元） | 出现「共 0 行」数据块 | 「不得为其推测」可见 |

`test_chat_multi_step.py` 全文件 **17 passed**。

### 变异验证（比「跑在 HEAD 上」更强的证据）

HEAD 工作树不含本轮之前若干未提交批次（`MSG_MULTI_STEP_DEGRADE_FAILED` 等），且首版用例的失败
构造方式与最终版不同，故改用**定向变异**逐条证明用例是真闸（在 `/tmp` 的 worktree 副本里改）：

| 变异 | 结果 |
|---|---|
| `_executeDataStep` 的 `except` 改回 `raise`（还原 C3） | 4 个隔离用例全红，C4 用例仍绿 ⇒ 两缺陷各自独立成闸 |
| 不再向 `_runQueryWithRetry` 传 `question`/`prior_state`（还原 C4） | 仅 C4 用例红，4 个隔离用例仍绿 |
| 还原 `inject_to_prompt` / `_build_prompt` 的失败步骤分支 | 对应单元用例各红一条 |

> 教训：**测试的失败构造方式要与被测缺陷解耦**。第一版 C3 用例靠「第二步 SQL 含专属标记」构造失败，
> 而重试会**重新生成** SQL（内容随 C4 是否修复而变）⇒ C3 用例实际被 C4 绑住。改用「第 N 个数据
> 查询起失败」的序号判定（`_DataQueryFailAdapter`）后两缺陷才真正独立。变异验证会立刻暴露这种耦合。

### 回归

相邻集成套件（9 文件：多步 / 追问级联 / 全局过滤 / chat / 流式 / 模型降级 / L1 路由 / doc_qa）**86 passed**；
全量单元套件 **2243 passed, 2 failed**，两个失败均为既有预存（ADS 加权重排死代码、Starlette
`Header.lower` 版本漂移），本批未触碰。

### code-reviewer 复审

结论 **APPROVE**（0 CRITICAL / 0 HIGH / 2 MEDIUM / 2 LOW），两条 MEDIUM 当场修完：

| 级别 | 问题 | 处理 |
|---|---|---|
| MEDIUM | **步骤错误原文进用户可见响应**：`str(exc)` 经 SQLAlchemy 包装后含 `[SQL: ...]`（内部表/列名）与 `[parameters: ...]`（查询字面量，可能含业务数据），`StepResult.error` 会原样进非流式响应与流式 `step_result` 事件由前端渲染 —— 违反「UI 层友好提示 / 不泄漏敏感数据」。RED 证据里响应确实带出了 `[SQL: SELECT SECRET_COL …]` 与 `{'n': 'ACME-机密客户'}`。 | 新增 `_userFacingErrorText`：剥掉两段细节、剥空时兜底；细节留给服务端日志与**回灌 LLM 的重试反馈**（并加了「反馈里仍含细节」的正向对照断言，防止退化成一刀切截断） |
| MEDIUM | **重试生成的 token 在「重试执行也失败」时丢账**（原 §2.3 M7 的一部分）：那次生成花了钱却不进台账；原用例的 `purpose` 精确列举断言把它固化成了期望（只期望 2 个 `nl2sql` 行）。 | `_attachRetryGenTokens` 把 token 挂到上抛的原始异常私有属性上（**不改类型/消息**，API 层按类型映射 HTTP 状态），`_executeDataStep` 取出落账 |
| LOW ×2 | `_hasDataStepResult` 语义边界（汇总步不在末尾的畸形计划）；C4 用「是否传 `question`」隐式推断多步 | 前者 docstring 写明（非 bug、生产不可达）；后者改为显式 kw-only `scope_question` |

### 遗留

1. ~~**「重试执行也失败」时重试生成的 token 仍会丢账**~~ ✅ **复审中已修**（见上）。仍未覆盖的边角：
   **单步**路径的两处 `_runQueryWithRetry` 调用拿到异常后直接抛给 API 层，未取 `_retryGenTokens`
   记账（单步失败即整轮失败，影响小于多步）；**重试生成本身抛错**时 `Nl2SqlError.tokens` 仍未在
   该路径记账（且要小心与 `_callWithFallback` 已写的 `fallback_sql` 行重复计数）。
   另一个相关事实：C3 只报**第一次**执行错误，重试后的新错误看不到（失败细节仍在服务端日志）。
2. `_callWithFallback` 全模型失败时的浪费 token 同属预存缺口。
3. ~~**C 路径（`_isFollowUpRetryCandidate` 兜底）的 `global_filters` 对称缺口**仍未补
   （`chat_service.py` 非流式与流式两处，前一批遗留）。~~ ✅ **已完成（2026-09-26）**，见 §10。
4. **单步流水线的硬失败仍直接抛给 API 层 —— 这是有意的**：单步失败会先回退多步拆解，两条都失败
   才对外报错；C3 只承诺「多步序列内部的失败隔离」。注意 `except Exception` **不捕获**
   `asyncio.CancelledError`（BaseException），故本批隔离不覆盖客户端断连（H4 仍待处理）。

---

## 10. 修复记录：C 兜底分支的 global_filters 对称缺口（2026-09-26）

**SSOT**：`Harness/changes/fix-c-fallback-global-filters/summary.md`（§3 P0 第 5 项，修复前的 P0 唯一剩余）。

### 根因

C 兜底分支（`_isFollowUpRetryCandidate` 命中 → `_prepareFollowUpMultiStep` → 多步重跑）不传
`global_filters`，而同级 B 分支（FOLLOW_UP 且上一轮多步）传 —— 非流式与流式**两侧同样缺失**。
用户可见后果：上一轮是带跨步口径约束的多步问题时，一句省略式追问若先进 C 兜底，
改写后重跑的每一步 SQL 都丢掉 `[global_constraints]`（外购/内外贸/站点等），
口径与上一轮不一致且无报错。

真正的根因不是「漏写一行」，而是**同一份多步前置知识被复制到 4 个入场点、靠人记得传参** ——
这个模式已漏过两次（C1/C2 流式那次 + 本次 C 兜底两侧）。

### 修复（`app/services/chat_service.py`）

抽取收敛进共享前置 `_prepareFollowUpMultiStep`（SSOT）：返回元组 4 → **5**，末位
`GlobalFilters | None`；4 个调用点（非流式 B/C、流式 B/C）改为纯透传。抽取点 **4 → 1**，
结构上不可能再漏。不变式：抽取仍只在「改写成功且已拆出多步计划」之后发生；抽取 token 仍由
`_resolveGlobalFilters` 内部落 `multistep_global_filter` 台账、不进 `initial_tokens`（避免重复计价）。

附带：`GlobalFilters` 在 5 处用作注解却从未 import（`from __future__ import annotations` 掩盖了
运行时错误），一并补上。

### 验证（TDD：先 RED 后 GREEN）

3 个新用例（cascade 非流式 C / global-filter 文件非流式 C + 流式 C）。RED 时三条都停在
`[global_constraints]` 缺失（且「3 次计划调用」已成立 ⇒ 链路走到了，缺的确实是约束注入）。

有意为之的两条断言设计：① 只查 `planCalls[1:]` —— 第 1 次是 C 首轮**单步**，本就不该有块；
reviewer 建议的负向对照（`not in planCalls[0]`）也已补上，防「过度注入」回归；
② 断言提取器输入含**改写后的问题** —— 这是「按 dto2 抽取」与「按 dto 抽取」的分野。

### 回归

- 两套件 **20 passed**；
- 相邻套件（multi_step / service_state / l1_routing / stream_api / model_routing_fallback）**54 passed**；
- 全部 chat 集成套件（`test_chat_*.py`，12 文件）**130 passed**；
- chat 相关单元套件 **254 passed, 1 failed**（失败为预存 ADS 加权重排死代码用例，与本批无关）。

### code-reviewer 复审

**APPROVE**：0 CRITICAL / 0 HIGH / 0 MEDIUM / 2 LOW。

| 级别 | 问题 | 处理 |
|---|---|---|
| LOW | 用例缺负向对照（未断言 C 首轮单步**无**约束块） | 当场补 3 处断言 |
| LOW | 窄路径下 L1.5（`:1051`，结果被丢弃）与 C 兜底（`:1956`）可能各抽一次 | 预存行为、非本次引入；两次是不同问题且各自独立计量（无重复计一笔、无丢账）⇒ 记 backlog |

### 遗留

1. **M7「重试错误只剩第一次」仍未处理**（§2.3；§9 遗留 1 已记录其部分修复）。当前用户可见文案
   只报首次执行错误（有意：那是病因），重试的新错误仅在服务端日志。**这是 P0 列表清空后
   与本主题最相关的下一项**（属健壮性/可观测性，非正确性）。
2. L1.5 的「先抽取后判能否拆步」（`:1051`）在拆步失败时白花一次 LLM 调用 —— 预存，
   与本次同源（抽取点位置不当），可考虑一并收敛到「拆步成功后再抽」。
3. 同类注解未 import 的预存隐患 6 处（`StepResultRead` / `AgentLoopResult` / `KpiCatalog`）。

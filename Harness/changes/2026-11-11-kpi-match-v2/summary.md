# KPI 匹配器 v2（包含式匹配 + 歧义澄清 + 50 条回归集）

> 分支：`feat/kpi-match-v2`（base = `origin/main` `33d292f8`）
> 设计稿：`docs/superpowers/specs/2026-11-11-kpi-definition-consumption-design.md`
> 计划：本目录 `plan.md`（D1–D6，按天拆分）
> 状态：**已完成，未部署**。生产仍是旧代码 + 匹配器 v1；本批**零 DDL、零生产写**。

## 1. 需求与背景

P0 止血（`2026-11-11-kpi-definition-consumption`）把治理列规范化了、也给出了诊断接口，
但**匹配器本身还是 v1 的 Jaccard**：问句与 `semantic_keywords` 分词后算集合相似度。

三个实测缺陷：

| # | 缺陷 | 后果 |
|---|---|---|
| 1 | Jaccard 对中文短问句极其敏感：关键词一个词没切中，分数直接掉到阈值以下 | 「上个月供应商的准时交付率是多少」命中不了「准时交付率」 |
| 2 | 短英文短语按子串参与，`OTD` 会命中 `notdone` 这类无关词 | 误命中 |
| 3 | 多条 KPI 同分时取 top-1，**静默替用户选了一个指标** | 答非所问且用户无从察觉 |

v2 的做法：**精确 code → 归一化包含 → 最长短语胜出 → 等长并列必反问**。
不猜，是这一批的核心约束。

## 2. 匹配规则（D-1，与代码一致）

```
1. 精确 code（KPI_SUPPLIER_OTD 这种大写下划线 token）→ confidence 1.0，单命中
2. 归一化包含：短语源 = kpi_name + semantic_keywords + ontology_metric.metric_alias
   - 归一化：NFKC → lower → 非字母数字转空格 → 折叠空白 → strip
   - 含 CJK 的短语：子串即命中
   - 纯 ASCII 短语：必须满足词边界 (?<![a-z0-9])phrase(?![a-z0-9])
   - 短语门槛：CJK ≥ 2 字，或 ASCII 字母数字 ≥ 3
3. 每个 KPI 取自己命中的**最长**短语；全局比较，**最长者胜出**
4. 最长长度并列且分属不同 KPI ⇒ ambiguous（不取 top1），候选按 code 升序
5. 无命中 ⇒ none（回落 LLM 流水线）
```

对外三种结果：

| `kind` | 含义 | 诊断接口 | 契约注入（B3 / chat Tier B） | chat |
|---|---|---|---|---|
| `hit` | 唯一命中（可能多条 KPI，但长度严格领先） | `ambiguous=false`，块按匹配结果构建 | 按命中 KPI 注入 | 正常 L1 / L4 流程 |
| `ambiguous` | 等长并列 | `ambiguous=true`，`matches` 全量返回但**块必为空** | **不注入** | `L1C` 澄清，0 LLM、0 token |
| `none` | 无命中 | 空 matches | 不注入 | 回落 LLM 流水线 |

`matchAll`（v1 Jaccard）**保留**，继续供 `/search` 端点、研究线、B4 分母示警使用（见 §7）。

## 3. 开关语义（D-3，与 `KPI_CONTRACT_INJECTION_ENABLED` 同构）

| 层 | 默认 | 作用 |
|---|---|---|
| env `KPI_MATCH_CONTAINMENT_ENABLED` | `true` | 进程级紧急杀闸：置 false ⇒ 一律走 v1 |
| `system_config` 行 `KPI_MATCH_CONTAINMENT_ENABLED` | 行缺失 ⇒ **false** | 灰度旋钮：显式 `true` 才启用 v2 |

**合入后生产照旧走 v1**（没有 `system_config` 行 ⇒ fail-closed）。开灰度是独立的一次数据操作，
需要事先批准，本批不做。回退只需把该行置 `false` 或删除，无需发版。

## 4. 变更清单

| commit | 内容 |
|---|---|
| `ba2bbb20` | 实施计划（D1–D6，按天拆分） |
| `4269f02e` | **D1**：`matchRoute` 包含式路由（归一化 / 短语门槛 / 最长胜出 / 等长歧义） + 单测 257 行 |
| `dce88c40` | **D2**：缓存短语源补 `metric_alias`（outer join） + 灰度开关 + 开关联动用例 |
| `2a95b0da` | **D3**：chat `L1C` 澄清短路 + `metaIntentOf` 收敛 wire 映射 |
| `e01f4dad` | **D4**：诊断接口 / B3 注入 / chat 三处共用同一 `matchRoute` 决策 |
| `61b4b7f5` | **D5**：50 条真实问句回归集（admin 诊断接口真实 HTTP 链路） |
| `58080a2a` | **D6**：变更记录 + Wiki 三处更新 |
| `d048980e` | **D6**：评审修复（MEDIUM-1/2/3 + L-1/L-2/L-3，见 §8） |
| `75d79407` | **D6**：评审发现与处置写入变更记录 |
| `5a84b99d` | **D6**：Milvus 隔离库改为文件自建，解耦 CI 分片归属（见 §6.5） |

## 5. 文件清单

| 文件 | 动作 |
|---|---|
| `backend/app/services/kpi_semantic_match_service.py` | 新增 `normalizeKpiText` / `isEligiblePhrase` / `phraseMatchesQuestion` / `_phrasesOf` / `buildKpiClarifyAnswer` / `matchRoute`；`matchAll` 不动 |
| `backend/app/services/kpi_match_cache.py` | 短语源增加 `OntologyMetric.metric_alias`（**outer** join，inner join 会丢 `metric_id IS NULL` 的 12 条 seed KPI） |
| `backend/app/services/kpi_metric_switches.py` | 新增 `isKpiMatchContainmentEnabled(session)` |
| `backend/app/config.py` | 新增 `Settings.kpiMatchContainmentEnabled`（alias、默认 True） |
| `backend/app/api/v1/kpi_diagnose.py` | 响应新增 `ambiguous`；歧义时 `matches` 全返回但 `codes=[]` |
| `backend/app/services/kpi_contract_builder.py` | `_matchKpiCodes` 按开关切 v2，歧义 ⇒ `[]` |
| `backend/app/services/kpi_contract_reconcile.py` | B3 `buildKpiContractBlock` 按开关切 v2，`kind != "hit"` ⇒ `None` |
| `backend/app/services/chat_constants.py` | `ROUTING_LAYER_KPI_CLARIFY="L1C"`、`META_INTENT_CLARIFY="clarify"`、`metaIntentOf` |
| `backend/app/services/chat_stream_output.py` | `_routeKpi` / `_buildKpiClarifyPreamble`；`PreambleResult.routing_layer: Literal["L1","L4","L1C"]` |
| `backend/app/services/chat_stream_entry.py` | meta `intent` 由 `metaIntentOf(routing_layer)` 派生；L1/L1C 零成本 |
| `backend/app/services/chat_service.py` | clarify 分支接线 |
| `backend/app/domain/schemas.py` | 诊断/匹配 DTO 增加 `match_method`；`semantic_keywords` 边界校验（评审 MEDIUM-3） |
| `backend/app/services/routing_metrics_service.py` | `_ROUTING_LAYERS` 补 `L1C`（评审 MEDIUM-2） |
| `backend/app/services/chat_context.py` | docstring 层枚举更正为 `L1 / L1C / L2 / L4`（评审 L-3） |
| `frontend/src/types/routingMetrics.ts` | 联合类型补 `L1C`（评审 MEDIUM-2） |
| `frontend/src/pages/RoutingMetricsPage.tsx` | `LAYER_COLORS` 补 `L1C`（评审 MEDIUM-2） |
| `frontend/src/i18n/zh-CN.ts`、`en-US.ts` | 补 `L1C` 层名与副标题（评审 MEDIUM-2） |
| `backend/app/tests/unit/test_kpi_match_route.py` | 新建（v2 规则单测） |
| `backend/app/tests/unit/test_kpi_match_cache.py` | 新建（alias 短语源 + outer join 反证） |
| `backend/app/tests/unit/test_chat_preamble_kpi_route.py` | 新建（L1C 短路） |
| `backend/app/tests/unit/test_chat_stream_meta_constants.py` | 收敛到 `metaIntentOf`，锁三态映射 |
| `backend/app/tests/integration/test_kpi_diagnose_api.py` | 新增 3 例（歧义 / 包含式命中 / 开关关闭回退） |
| `backend/app/tests/integration/test_kpi_contract_injection.py` | 新增 v2 注入面 3 例（歧义 ⇒ 空 / 歧义 ⇒ 不建块 / 包含式命中 ⇒ 建块） |
| `backend/app/tests/integration/test_kpi_switch_unification.py` | 新增开关同构用例 |
| `backend/app/tests/integration/test_kpi_match_corpus.py` | 新建（50 条语料验收） |
| `backend/app/tests/unit/test_kpi_catalog_schemas.py` | 新增 `TestSemanticKeywordBounds`（评审 MEDIUM-3） |
| `backend/app/tests/unit/test_routing_metrics_service.py` | L1C 计数用例 + 5 层断言（评审 MEDIUM-2） |
| `backend/app/tests/unit/test_kpi_match_cache.py` | 全量失效断言补强（评审 L-2，变异 M8 取证） |
| `backend/app/tests/integration/test_kpi_governance_contract_injection.py` | 新增冷缓存自预热用例（评审 MEDIUM-1） |

**零 DDL**：本批不新增 Alembic 迁移，不碰表结构；唯一的数据依赖是 `system_config` 里可以**不存在**的那一行。

## 6. 验收实测

### 6.1 50 条真实问句语料（`test_kpi_match_corpus.py`，真实 PG + 完整 HTTP）

语料构成：正向 36 条（唯一命中）/ 歧义 4 条（等长并列）/ 负向 10 条（一个都不许命中）。
每条都走 `GET /api/v1/admin/kpi/diagnose`，即运行时同一个 `matchRoute` 决策。

| 指标 | 要求 | 实测 |
|---|---|---|
| 正向命中 | ≥ 33/36（90%） | **36/36** |
| 误命中（负向被命中 / 命中错 KPI / 歧义判错） | ≤ 2 | **0** |
| 歧义候选集合精确等于期望 | 4/4 | **4/4** |

**反向取证（证明这条验收不是空转）**：把 `KPI_MATCH_CONTAINMENT_ENABLED` 置 `false`
（强制回落 v1 Jaccard）后同一份语料跑出 **正向命中 1/36、误命中 4** ⇒ 断言必然失败。
即：这组数字是 v2 真实贡献的，不是「期望值调出来的」。

### 6.2 定向回归（净新增失败 0）

对 `33d292f8`（main）与 `feat/kpi-match-v2` 跑同一集合
（`test_kpi_match_route` / `test_kpi_semantic_match_service` / `test_kpi_match_cache` /
`test_kpi_switch_unification` / `test_chat_stream_meta_constants` / `test_chat_preamble_kpi_route` /
`test_chat_service` / `test_kpi_diagnose_api` / `test_kpi_match_corpus` / `test_chat_l1_routing`）：

| | 基线 `33d292f8` | 分支 | 差 |
|---|---|---|---|
| failed | 8 | 8 | **0（同一组 8 个 node id）** |
| passed | 213 | 274 | +61（本批新增用例） |

8 条基线红全部是既有问题，与本批无关：
`test_chat_service.py` 5 条（`TestChatService::test_class_filter_logs_successful_pruning`、
`TestBuildAnswerPrompt::{test_empty_data_injects_hint, test_summary_includes_total_and_columns,
test_summary_includes_numeric_stats, test_summary_27_rows_not_truncated_v2}`）与
`test_chat_l1_routing.py::TestL1KpiCard` 3 条（`test_l1_answer_carries_a_kpi_card`、
`test_l1_answer_without_a_value_carries_no_card`、`test_l1_answer_carries_rationale_and_persists_it`）。

### 6.3 变异取证（7 条，全部证明「变红」）

每条变异施加后只跑对应的一个用例，跑完 `finally` 还原；每次还原后 `git status` 干净。

| # | 变异 | 判别用例 | 结果 |
|---|---|---|---|
| M1 | ASCII 词边界 `(?<![a-z0-9])…(?![a-z0-9])` → 纯 `in` | `test_kpi_match_route.py::test_ascii_phrase_requires_token_boundary` | 红 |
| M2 | 等长并列 → 永远取 `top[0]` | `…::test_equal_length_cross_kpi_is_ambiguous_sorted_by_code` | 红 |
| M3 | 最长短语胜出 → 最短胜出 | `…::test_longest_phrase_wins` | 红 |
| M4 | CJK 门槛 2 → 1 | `…::test_single_cjk_char_phrase_is_ineligible` | 红 |
| M5 | 短语源丢掉 `metric_alias` | `…::test_metric_alias_is_a_phrase_source` | 红 |
| M6 | 诊断 `ambiguous` 恒 `False` | `test_kpi_diagnose_api.py::test_diagnose_reports_ambiguous_and_builds_no_block` | 红 |
| M7 | 缓存 `outerjoin` → `join` | `test_kpi_match_cache.py::TestMetricAlias::test_metricAliasOf_is_none_for_unlinked_kpi` | 红 |

M6 的判别点：诊断用例同时断言 `ambiguous=true` **且** `injectionBlockEmpty=true`，所以
「只是把字段写死」和「歧义时仍然建块」都会红。M7 的判别点：seed 的 12 条 KPI `metric_id` 全为 NULL，
inner join 会让它们整体消失。

### 6.4 M1 三条验收对照

设计 §13 M1（「匹配器 v2 上线可验收」）三条：

| # | 验收项 | 证据 |
|---|---|---|
| 1 | 50 条语料正向命中 ≥ 90%、误命中 ≤ 2 | §6.1：36/36、0 |
| 2 | 4 条歧义必须 `ambiguous` 且不执行 SQL | §6.1 候选集合 4/4；`test_kpi_diagnose_api` 断言块为空；`test_chat_preamble_kpi_route` 断言 L1C 零 token |
| 3 | 问句直接给 KPI code ⇒ 契约块非空 | `test_kpi_match_corpus.py::test_kpi_code_question_yields_non_empty_contract_block` |

第 3 条需要真实挂靠的 metric（seed 的 12 条 `metric_id` 均为 NULL，块必空），用例里单独构造了一条
`RATIO / WEIGHTED_RATIO / ESCAPE_HATCH` 的 pass 型 KPI，断言 `matchMethod=="exact_code"` 且块非空。

### 6.5 闸门首轮：12 条「净新增」是分片重排假回归（已修用例隔离）

PR #119 首轮 `merge-gate / failure-set-diff` 判 **退出码 1 / 净新增 12**，全部落在
Milvus / RAG 集成用例（`test_milvus_document_fields.py` 4 条、`test_rag_clearance.py` 8 条），
与本批改动的 KPI 匹配零交集。

**取证（不是推测）**：

- CI 集成分片按「`find | sort` 后**下标 mod 4**」切分（`.github/workflows/ci.yml`）。
- 本批新增 `test_kpi_match_corpus.py` ⇒ 排序位置 188 之后的文件**整体后移 1 位**。
- 实测两个红文件的归属：`test_milvus_document_fields.py` 211→212、`test_rag_clearance.py`
  243→244 ⇒ 均从 **shard 2 挪到 shard 3**。
- 错误原文：`MilvusException: (code=800, message=database not found[database=qa_ci])`。
  Milvus 隔离库 `qa_ci` 是**惰性创建**的（只有用到本体清理夹具时才建），shard 3 恰好
  没有任何建库文件 ⇒ 这两个文件直接撞库不存在。PR #116 是同一类问题，当时只给
  `test_rag_api.py` 补了自建库夹具，其余文件仍是碰运气。

**修复**（`5a84b99d`，纯测试侧）：

- `app/tests/milvus_isolation.py` 新增 `ensureIsolatedDatabaseForTest()`（按当前
  `MILVUS_DB_NAME` 幂等建库）；
- `test_milvus_document_fields.py` / `test_rag_clearance.py` 补 module 级 autouse 夹具；
  `test_rag_api.py` 改为复用该函数（三处同一实现）；
- 单测 `test_milvus_db_isolation.py::TestEnsureIsolatedDatabaseForTest` 2 条
  （缺库则建、有库 no-op），RED（`AttributeError`）→ GREEN 取证。

修复后这两个文件**不再依赖分片归属**；判据仍以新一轮 CI 的失败集合对差为准。

## 7. 明确未做的事

| 项 | 状态 |
|---|---|
| 部署 | **未部署**。生产仍跑旧代码 + 匹配器 v1 |
| 灰度行 | **未开启**。`system_config` 里没有 `KPI_MATCH_CONTAINMENT_ENABLED` 行 ⇒ fail-closed，生产不切 v2 |
| `/search` 端点 | 仍 v1 `matchAll`（发现/诊断类界面，不是执行面） |
| 研究线 ESL 指标臂 | 仍 v1 |
| B4 分母示警 `buildKpiDenominatorWarning` | 仍 v1（计划只改 B3） |
| Jaccard | **不删**。`matchAll` 继续供上述面使用 |
| 表结构 / 迁移 | 零 DDL，无新迁移 |
| KPI 写 ACL | **未改**（仍是登录即可建 KPI）。这是 §8.1 HIGH-1 的根因，需产品决策；在决策落地前灰度开关必须保持关闭 |

## 8. 评审发现与处置

`code-reviewer` + `security-reviewer` 对本批全量 diff 各跑一轮，逐条处置如下。
**唯一未修的是 HIGH-1**（需要产品决策，超出本批边界），其余全部在本批内修掉。

### 8.1 HIGH-1（未修，需产品决策）— 匹配器不再看 `match_threshold`，KPI 写入口仍只要求登录

**现象**：v1 的 `matchAll` 按 `kpi_catalog.match_threshold` 过滤候选；v2 的 `matchRoute` 用固定的
`CONTAINMENT_CONFIDENCE = 0.9` 判定包含式命中，**完全不读 `match_threshold`**。同时
`kpi_catalog.createKpi` 只用 `Depends(getCurrentUser)`（「CREATE 不做 ACL」，见
`api/v1/kpi_catalog.py`），任何登录用户都能写 KPI。

**组合后果**：任意登录用户可以新建一条名为「供应商」的 KPI，把自己的问句劫持到该 KPI 的契约块
（进而影响 SQL 生成的方向），**且 v2 下不再有阈值这道闸门**。

**为什么本批不修**：

- 根因是**既有的写 ACL 缺口**（已在 `qa-system-governance-write-gate` 记录为待产品决策事项：
  类/属性/导入 REST 仍只要求登录），不是 v2 引入的。v1 同样可被利用——攻击者新建 KPI 时
  把 `match_threshold` 直接设成 `0.0` 即可，D5 语料的负向用例正是这样构造的。
- v2 只是**放大了**影响面（去掉阈值门槛），没有制造新的权限缺口。
- 修 ACL 会改变谁可以建 KPI 这个产品语义，属于另一个立项。

**本批的处置**：

1. 记录为 HIGH 已知问题，不在本批扩大范围去改 ACL。
2. **灰度开关必须保持关闭**，直到 ACL 决策落地：
   - `system_config` 里**不要**插入 `KPI_MATCH_CONTAINMENT_ENABLED` 行（缺行 ⇒ fail-closed，生产走 v1）；
   - 环境变量 `KPI_MATCH_CONTAINMENT_ENABLED` 保持默认即可（默认 True 只在灰度行存在时才生效——
     两个开关是**且**关系：env 是紧急熔断，`system_config` 行是灰度闸门，缺行仍然 False）。
3. 合并前请用户确认：**本批合入但不开启灰度**（§7 已列）。

### 8.2 MEDIUM-1（已修）— B3 在冷缓存下静默丢失契约块

`buildKpiContractBlock` 依赖 `get_kpi_match_cache()`；冷/脏缓存下 `matchRoute` 返回 `none`、
`matchAll` 返回空列表 ⇒ **契约块静默消失、零日志**。chat 链有预热，研究线等非 chat 调用方直接进 B3，
不受保护。

**修复**：匹配前在 savepoint 内 `await get_kpi_match_cache().ensureFresh(session)`；
失败时 `logger.warning(...)` 并返回 `None`（不阻断 B3 之外的主流程）。
savepoint 保证刷新失败不会污染调用方事务。

**测试**：`test_kpi_governance_contract_injection.py::test_cold_cache_is_warmed_by_b3_itself`
（只 `onKpiChanged()` 不作预热，断言块非 `None`）。修前 RED、修后 GREEN。

### 8.3 MEDIUM-2（已修）— `L1C` 在路由指标里不可见

D3 新增了 `L1C` 路由层，但 `routing_metrics_service._ROUTING_LAYERS` 只有 `L1/L2/L3/L4` ⇒
`layer_distribution` 静默丢掉 L1C 的命中，且 `total_queries` 不计入。灰度后监控面会系统性少报。

**修复**：后端白名单补 `L1C`；前端 `routingMetrics.ts` 联合类型 + `RoutingMetricsPage` 的
`LAYER_COLORS` 补 `L1C: "#faad14"`；`zh-CN` / `en-US` 补层名与副标题。

**测试**：`test_routing_metrics_service.py::test_clarify_layer_is_counted`（断言 L1C 桶命中 1 且
`total_queries == 1`）；原 4 层用例改名并更新为 5 层有序断言。

### 8.4 MEDIUM-3（已修）— `semantic_keywords` 无界（提示注入放大 + DoS）

`KpiCatalogCreate/Update.semantic_keywords` 是 `list[str]`，**无长度上限**：单条可写任意长字符串
（放大注入面：短语会进入匹配与 LLM 上下文），列表可写任意多条（缓存与匹配 O(n·m) 放大）。

**修复**：`app/domain/schemas.py` 新增模块级常量 `KPI_SEMANTIC_KEYWORDS_MAX_ITEMS = 50`、
`KPI_SEMANTIC_KEYWORD_MAX_CHARS = 100`，`KpiCatalogCreate` / `KpiCatalogUpdate` 各挂
`field_validator("semantic_keywords")`。

**测试**：`test_kpi_catalog_schemas.py::TestSemanticKeywordBounds` 5 条
（101 字拒 / 51 条拒 / 50×100 边界过 / Update 拒超长 / Update 收 None）。

### 8.5 LOW（已修）

| # | 问题 | 处置 |
|---|---|---|
| L-1 | `buildKpiDenominatorWarning` 的 docstring 没说清 B4 不跟 v2 | docstring 明确：v2-only 命中 ⇒ **无**分母示警（D-5 的已知语义差） |
| L-2 | `test_onKpiChanged_full_invalidation_drops_aliases` 断言不到东西（`metricAliasOf` 冷时恒 None） | 增加 `cache._metricAliasByCode == {}` 并注释原因；已用变异 M8（删掉 `self._metricAliasByCode = {}`）取证该断言会红 |
| L-3 | `chat_context.py` docstring 仍写「枚举值 L1~L4」 | 更正为 `L1 / L1C / L2 / L4` 并注明 L1C 语义 |

### 8.6 LOW（不修，记录）

| # | 问题 | 理由 |
|---|---|---|
| L-4 | 澄清话术渲染 `kpi_name`（用户可控字符串） | 前端 `react-markdown` 默认转义，无 XSS 路径；且读的是**用户自己建的** KPI 名 |
| L-5 | `buildLineOption` 趋势图仍是 mock 数据 | 既有 TODO，与本批无关 |
| L-6 | `ruff` 在改动文件上仍有 37 条既有问题 | 逐文件 now-vs-HEAD 对比确认**净新增 0**；`kpi_contract_reconcile.py` 反而从 1 条降到 0 条 |

### 8.7 修后回归

修复后重跑 15 个相关文件：**8 failed, 338 passed** —— 与 §6.2 的比赛前基线**同一组 8 个 node id**，
净新增失败仍为 **0**（新增 64 条用例全绿）。

## 9. 回滚

`git revert` 本批提交（`4269f02e`…`d048980e`，加上文档提交）即可。不涉及 schema、不涉及数据迁移；
回滚后生产行为与 `33d292f8` 完全一致（本批未部署、灰度未开，回滚只影响代码仓库）。
若已开灰度（本批未开），把 `system_config` 行置 `false` 或删除即可退回 v1，无需发版。

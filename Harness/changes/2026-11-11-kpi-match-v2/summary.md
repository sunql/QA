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
| `backend/app/domain/schemas.py` | 诊断/匹配 DTO 增加 `match_method` |
| `backend/app/tests/unit/test_kpi_match_route.py` | 新建（v2 规则单测） |
| `backend/app/tests/unit/test_kpi_match_cache.py` | 新建（alias 短语源 + outer join 反证） |
| `backend/app/tests/unit/test_chat_preamble_kpi_route.py` | 新建（L1C 短路） |
| `backend/app/tests/unit/test_chat_stream_meta_constants.py` | 收敛到 `metaIntentOf`，锁三态映射 |
| `backend/app/tests/integration/test_kpi_diagnose_api.py` | 新增 3 例（歧义 / 包含式命中 / 开关关闭回退） |
| `backend/app/tests/integration/test_kpi_contract_injection.py` | 新增 v2 注入面 3 例（歧义 ⇒ 空 / 歧义 ⇒ 不建块 / 包含式命中 ⇒ 建块） |
| `backend/app/tests/integration/test_kpi_switch_unification.py` | 新增开关同构用例 |
| `backend/app/tests/integration/test_kpi_match_corpus.py` | 新建（50 条语料验收） |

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

## 8. 已知遗留（MEDIUM 及以下）

<!-- REVIEW_FINDINGS -->

## 9. 回滚

`git revert` 本批 5 个提交即可。不涉及 schema、不涉及数据迁移；回滚后生产行为与 `33d292f8` 完全一致。
若已开灰度（本批未开），把 `system_config` 行置 `false` 或删除即可退回 v1，无需发版。

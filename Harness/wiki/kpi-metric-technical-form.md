# KPI 指标技术形态治理

> **状态**：设计已批准（2026-10-28），**未实施**。
> **Spec**：[`docs/superpowers/specs/2026-10-28-kpi-metric-technical-form-design.md`](../../docs/superpowers/specs/2026-10-28-kpi-metric-technical-form-design.md)（608 行）
> **变更 SSOT**：[`Harness/changes/2026-10-28-kpi-metric-technical-form/summary.md`](../changes/2026-10-28-kpi-metric-technical-form/summary.md)

本文件是**后续开发的架构参考**（不是实施记录）。回答"指标口径放哪、谁说了算、NL2SQL 怎么消费、为什么 L1 直通是死的"四类问题。

---

## 1. 一句话

把散落在 6 个地方的业务口径，收敛成一条**可对账、可机器读、可确定性执行**的链：
`kpi_catalog`（治理签字）→ `ontology_metric`（技术形态）→ `ontology_metric_source`（多源类），
并让 NL2SQL **动态消费实际配置的口径**（Tier B 契约注入）与**确定性直通**（Tier A）。

---

## 2. 权威关系（方案 A）

```
kpi_catalog              治理 SSOT      业务语义 + 口径声明(中文) + 粒度 + 业务负责人
   │ metric_id FK (nullable, 已有)
   ▼
ontology_metric          技术形态 SSOT  结构化 measure + 聚合阶 + 粒度键 + 过滤谓词 + 逃生舱
   │ 1:N
   ▼
ontology_metric_source   多源类 M:N     role = NUMERATOR | DENOMINATOR | FILTER | BASE

feature_definition      执行封装(降级)   calculation_logic + 窗口 + 刷新 + feature_value
```

**为什么不是别的**（完整对比见变更 SSOT §2.1）：

| 方案 | 否决理由 |
|---|---|
| `feature_definition` 中心化 | 语义错位（**特征 ≠ 指标**）；只有 6 行；`entity_type` 单值表达不了复合粒度；`feature_value` 覆盖 1/6 且 stale |
| 新建 `metric_definition` 中枢表 | 迁移面最大；`ontology_metric` 已经是这个角色，再开等于承认上一张选错 |

**`feature_definition` 为什么"降级"而不是"删除"**：它是**唯一存着完整可执行 Oracle SQL** 的地方（`calculation_logic`，6 条），也是 `feature_value` 预计算管线所在。它不是"口径的权威"，是"口径的执行载体"。两者语义不同级，不合并。

---

## 3. 关键概念

| 术语 | 含义 |
|---|---|
| **治理口径** | 业务方签字的口径内容（中文五件套：numerator / denominator / grain / unit / owner） |
| **技术形态** | 机器可读的口径表达（measure JSON + 聚合阶 + 粒度键 + 过滤谓词） |
| **measure** | 基础度量定义：`{agg, expr, filter, class_id, class_alias}` |
| **聚合阶 `aggregation_order`** | `WEIGHTED_RATIO`（`SUM(num)/SUM(den)`）vs `AVG_OF_RATIOS`（`AVG(num/den)`）—— **辛普森悖论开关，两者绝不可混用** |
| **逃生舱 `formula`** | `ontology_metric.formula`：人写的可执行 SQL 兜底。**不自动嗅探它是不是 SQL**，用显式 `execution_source` 枚举 |
| **对账 verdict** | `pass` / `mismatch` / `unknown` 三档 |
| **Tier A / B / C** | A=确定性直通(0 LLM) / B=NL2SQL+契约注入 / C=降级(现状) |
| **SSOT** | Single Source of Truth，单点事实 |

---

## 4. 双轨制（结构化 + 逃生舱）

| 轨 | 载体 | 地位 | 适用 |
|---|---|---|---|
| **机器轨** | `numerator_measure` / `denominator_measure` / `aggregation_order` / `grain_keys` / `filter_expression` | **真相** | 可对账、可编译、可确定性执行 |
| **人轨** | `formula`（SQL 逃生舱）、中文 `numerator`/`denominator`/`grain` | 兜底 / 人话说明 | 窗口函数、同环比、分位数、跨表 pivot |

**裁决规则（一句话）**：

> **口径的内容以治理签字为准；口径的技术表达以结构化为准。**

两句不矛盾 —— 前者管"治理 ⇄ 技术"，后者管"技术内部"。

**`execution_source` 枚举**（不用自动嗅探 —— 不可测、不可复现、会把人写的说明误当可执行）：

| 值 | 含义 | Tier A |
|---|---|---|
| `COMPILED` | 由结构化编译出可执行 SQL | ✅ |
| `ESCAPE_HATCH` | `formula` 是手写可执行 SQL | ⚠️ 可执行但不经编译器 |
| `NONE` | 无可执行路径 | ❌ 闸门拦 |

---

## 5. 分层数据模型（因 5 种口径形状）

dev 库 12 条 KPI 实为 5 种形状（加总 / 平均 / 带过滤平均 / 条件计数比 / 差值比），**9/12 跨 2–3 张表、3 条单表**（`KPI_PAYMENT_ON_TIME` / `KPI_SUPPLIER_DEFECT_RATE` / `KPI_SUPPLIER_FPY`）⇒ `target_class_id` 单值 FK 表达不了其中 9 条真实口径。

**measure 层**（一份基础度量的定义）：
```json
{
  "agg": "SUM | COUNT | AVG | COUNT_DISTINCT",
  "expr": "ACTUAL_RECEIPT_DATE - PROMISED_DATE",
  "filter": "IQCRESULT = 'FAIL'",
  "class_id": 12,
  "class_alias": "DWD_INCOMING_INSPECTION"
}
```

**metric 层**（一个指标 = 分子 + 分母 + 聚合阶 + 粒度 + 过滤）：
| 字段 | 值域 |
|---|---|
| `metric_type` | `MEASURE` / `RATIO` / `DIFFERENCE` / `DERIVED` |
| `numerator_measure` / `denominator_measure` | measure JSON |
| `aggregation_order` | `WEIGHTED_RATIO` / `AVG_OF_RATIOS` / `NONE` |
| `grain_keys` | `["supplier_code","plant_code","year_month"]` |
| `filter_expression` | 全局过滤谓词（Text） |
| `status` | `DRAFT` / `PUBLISHED` |
| `owner` | **技术口径负责人**（≠ `kpi_catalog.owner` 的业务负责人） |

> `target_class_id` 保留不改，语义 = `role=BASE` 的类。`dimension_defaults` **零消费者**，另立批次清理。

---

## 6. 对账模型（三层三档）

| # | 对账对 | 基准 | mismatch 行为 |
|---|---|---|---|
| 1 | **技术内部**：结构化 measure ⇄ `formula` SQL 列引用 | **结构化** | 标 degraded，Tier A 不可用 |
| 2 | **治理 ⇄ 技术**：治理 `*_columns` / `grain_keys` ⇄ 技术 measure 列集合 | **治理** | **阻断**：不注入 + audit + 告警 |
| 3 | **可执行 ⇄ 结构化**：`execution_source` SQL 列引用 ⇄ 结构化 measure 列 | **结构化** | Tier A 不可用，降 Tier B |

**三档判定**：

| verdict | 条件 | 行为 |
|---|---|---|
| `pass` | 治理列 ⊆ 技术列 ∧ grain 一致 ∧（RATIO 时）`aggregation_order` 非空 | 正常 |
| `unknown` | 治理侧结构化字段为空 —— **存量主流**（中文 numerator 抽不出列名） | 注入但标记 |
| `mismatch` | 技术跑偏 / grain 不一致 / RATIO 缺 `aggregation_order` | **不注入该 KPI** |

**挂点**：**注入时实时对账** + **发布时对账** ⇒ **不依赖任何编译入口**（`compileObject` 零生产调用方 ⇒ 编译期对账在生产永不触发）。

**审计**：`audit_log`，`entity_type='kpi_metric_reconcile'`，**仅在有 finding 时落**（`pass` 不落，避免噪声）。

---

## 7. 运行时（Tier A / B / C）

```
问句
 ├─ KPI 语义匹配命中？（读配置里的 semantic_keywords）
 │   ├─ 否 ─────────────────────────────────▶ Tier C：NL2SQL 无契约（现状）
 │   └─ 是 → kpi.metric_id → ontology_metric(+sources)
 │        ├─ metric.status=PUBLISHED ∧ execution_source≠NONE ∧ 对账=pass
 │        │  ∧ 问句无范围/分组/筛选约束        ⇒ **Tier A**（0 LLM 直通）
 │        ├─ metric 有结构化口径，对账 pass|unknown ⇒ **Tier B**（NL2SQL + 契约注入）
 │        └─ metric 缺失 / 对账 mismatch            ⇒ **Tier C**（+ audit 记 drift）
```

### Tier A（确定性直通）

**这条路早已存在，但 100% 失效** —— 见 §9。接通后：0 LLM、0 幻觉、直接返回结果、落 `METRIC_RESULT` 证据、任何异常自动回退 NL2SQL。

**三级绑定**（P1 激活 1+3，level 2 的 `feature_definition.metric_id` FK 属 P3）：

| 级别 | 绑定方式 |
|---|---|
| 1 | `kpi.metric_id` → `ontology_metric` |
| 2 | `feature_definition.metric_id` → `ontology_metric`（P3） |
| 3 | `kpi.formula` → `feature_definition.calculation_logic`（**逃生舱路径**） |

**问句约束检测**（4 类，任一命中 ⇒ 降 Tier B）：时间范围 / 分组维度 / 排名筛选 / 实体过滤。方向保守（宁可多降级，不可答非所问）。

### Tier B（契约注入）

**注入点**：`nl2sql_prompts.py:256` `_buildPlanSystemPrompt` —— 已有 3 个同款 block（`featureCatalogText` / `dictionaryText` / `wikiRulesBlock`），照抄模式。

- 新增第 12 个 `UntrustedLabel.KPI_CONTRACT`（现 11 个，`app/security/prompt_sanitizer.py:51`；`wrapUntrusted` 在 `:118`）⇒ **红队 `ALL_LABELS` 断言须同步**
- 渲染：治理五件套 + 聚合阶 + 粒度键 + 源类(role) + **允许的 SQL 形态**
- **比率型文案（防倒置）**：

| `aggregation_order` | 允许且仅允许 | 禁止 |
|---|---|---|
| `WEIGHTED_RATIO` | `SUM(分子)/SUM(分母)` | `AVG(分子/分母)` |
| `AVG_OF_RATIOS` | `AVG(分子/分母)` | `SUM(分子)/SUM(分母)` |

> ⚠️ **反面教材**：未合并分支 `feat/kpi-l1-metric-id` 的 `nl2sql_prompts.py:311`「禁止先聚合后相除以外的写法」字面 = **强制先聚合后相除** = 辛普森悖论错误写法。该分支 B3 **弃用**，按本表重写。提案 `2026-10-27-kpi-governance-into-nl2sql-proposal.md:156` 同处亦错。

---

## 8. 发布闸门

```
KPI: DRAFT → PUBLISHED  ⟸ 必须同时满足：
  ① metric_id IS NOT NULL
  ② metric.status = PUBLISHED
  ③ metric.execution_source ≠ NONE
  ④ 对账 = pass（unknown 不允许发布）
  ⑤ datasource_id IS NOT NULL        ← dev 现状 12/12 全 NULL
不满足 → 409 + 明确缺哪一项
```

`ontology_metric.status: DRAFT → PUBLISHED` 走同一套闸门。闸门在 **service 层**强制（不只在 API 层）。

---

## 9. 现状陷阱（动手前必读）

| # | 陷阱 | 证据 |
|---|---|---|
| **T1** | **`ontology_metric` 在 NL2SQL 里零消费者** —— 补 12 行也不改变任何一条 SQL | `nl2sql_service.py:34-35` 不 import 它；`nl2sql_prompts.py` 0 个 "etric"；`nl2sql_schema.py:251` `buildSchemaText` 只渲染类；`chat_recall.py` 用 `typeFilter="class"`；`query_plan.py` 注释明写"formula 没有确定性渲染器" |
| **T2** | **L1 确定性直通早已存在但 100% 失效** | `chat_stream_output.py:207/222` → `chat_service.py:1456/1480/1499/1520`；断点 `_resolveCalculationFeature` 用 `FeatureDefinition.feature_definition == kpi.formula` 等值匹配（**散文描述列 vs 技术公式列**）；dev 库 12×6 命中 **0** |
| **T3** | **口径 6 个家只活 1 个** | `kpi_catalog` 五件套(12,中文) / `kpi_catalog.data_source`(22 表引用/13 命中=59%) / `ontology_metric`(0 行,死) / `feature_definition.calculation_logic`(6 条完整可执行 Oracle SQL,**不进 prompt**) / `feature_definition.feature_definition`(散文,进 prompt) / `ontology_class.property`(54 类) |
| **T4** | **语义匹配第一道门是断的** | dev 库 `semantic_keywords` **12/12 全空**；`KpiCatalogUpdate`/`KpiCatalogRead` **都没有**这两个字段；前端无控件 |
| **T5** | **缓存三洞** | `kpi_match_cache.py:149` 改名残留 / `:166-167` 冷缓存 no-op / `:201-212` ORM listener 只 log（裸 SQL seed 绕过） |
| **T6** | **本体缺 DWS 层** | dev `ontology_class` 54 类 = ODS 21 + DWD 30 + 空 3，**DWS=0 / ADS=0**；feature 引用的 3 张 DWS 表全不在本体；8 张 KPI 源表不在本体（`DWD_INCOMING_INSPECTION` 影响合格率/FPY/NCR 三条质量 KPI） |
| **T7** | **B2 对账挂在零调用方的 `compileObject` 上** | 唯一 `KnowledgeCompilerService()` 实例在 `knowledge_reconcile_worker.py:60`，只调 `reconcile()` ⇒ 编译期对账生产永不触发 |

### 缓存三洞修法

| 洞 | 位置 | 修法 |
|---|---|---|
| C1 改名残留 | `kpi_match_cache.py:149` `old = self._by_code.pop(kpi_code, None)` | 改**按 `id` 失效**（id 跨改名稳定） |
| C2 冷缓存 no-op | `:166-167` `if not self._loaded: return` | 置 dirty 标记 → 下次查询 lazy rebuild |
| C3 raw SQL 绕过 | `:201-212` listener 只 log | seed 脚本改走 service；或脚本末尾显式 `onKpiChanged(None)` + `warmUp` |

---

## 10. 分期

| 期 | 内容 | 状态 |
|---|---|---|
| **P1** | 迁移 `0136`/`0137` + `ontology_metric` 扩列 + `ontology_metric_source` 新表 + `kpi_catalog` 扩列 + DTO 补 `semantic_keywords`/`match_threshold` + 对账模块 + Tier B 契约注入 + 缓存三洞修复 | 待实施 |
| **P1-数据** | 12 条 `ontology_metric` 反向合成（治理口径 + `docs/data-knowledge/采购域.md` 血缘）→ **产出需业务方审核后落库**；`semantic_keywords` 补全；`datasource_id` 绑定 | 待实施 |
| **P2** | Tier A 确定性直通 + 问句约束检测 + 发布闸门 | 待实施（独立立项） |
| **P3** | 结构化 → SQL 编译器（`COMPILED` 路径）+ `feature_definition.metric_id` FK | 可选 |

**实施计划范围 = P1 + P1-数据**。

### 灰度开关

| 开关 | 默认 | 作用 |
|---|---|---|
| `KPI_CONTRACT_INJECTION_ENABLED` | true | Tier B 契约注入 |
| `KPI_METRIC_TIER_A_ENABLED` | **false** | Tier A 直通（先只读对比两路答案一致性） |
| `KPI_METRIC_GATE_ENABLED` | **false** | 发布闸门（先观察再阻断） |

---

## 11. 未合并分支 `feat/kpi-l1-metric-id` 的处置（已决 = 选项 3）

分支 4 commit（`66e2542f` B1+B5 / `1b29e139` B2 / `aaba0a43` B3 / `fd84d092` B4），基点 `cc4b8986`，**main 领先 60**。

| 件 | 处置 |
|---|---|
| B1（`_resolveCalculationFeature` 两级解析） | **重写**（按本设计三级绑定） |
| B2（编译期对账） | **弃用** —— 挂在零调用方的 `compileObject` 上 |
| B3（契约注入） | **弃用** —— 机制对但**比率型文案写反** |
| B4（分母契约守卫 + `extractSqlIdentifiers`） | **✅ 只取这一件** |
| B5（`/metric` 自动 DRAFT KPI） | 保留（可后置合入） |

**不背 60-commit rebase**；新分支只取 B4 的两块。

---

## 12. 相关文件

| 文件 | 角色 |
|---|---|
| `backend/app/domain/models.py:337` | `OntologyMetric`（现 4 列 → 扩 9 列） |
| `backend/app/domain/models.py:362` | `KpiCatalog`（含 `metric_id` FK、`semantic_keywords`、`match_threshold`） |
| `backend/app/domain/schemas.py:624/634/686` | `OntologyMetricCreate` / `Update` / `Read` |
| `backend/app/domain/schemas.py:699/733/756` | `KpiCatalogCreate` / `Update` / `Read`（后两者**缺** `semantic_keywords`） |
| `backend/app/services/chat_service.py:1499` | `_resolveCalculationFeature` —— **T2 断点所在** |
| `backend/app/services/chat_stream_output.py:207/222` | `_runL1AndL4Preamble` —— L1 入口 |
| `backend/app/services/kpi_match_cache.py` | 缓存三洞（T5） |
| `backend/app/services/kpi_semantic_match_service.py:137/212/226` | `match()` / `_matchExactAlias` / `_matchByKeywords` |
| `backend/app/services/nl2sql_prompts.py:256` | `_buildPlanSystemPrompt` —— Tier B 注入点 |
| `backend/app/security/prompt_sanitizer.py:51/118` | `UntrustedLabel`（11 个）/ `wrapUntrusted` |
| `backend/app/services/chat_domain.py` | `_handleDefineMetric` —— **B5 顺修了这里的 `actor` 漏传 500** |

---

## 13. 相关条目

- [nl2sql-engine](nl2sql-engine.md) —— 4 层路由；L1 段的 `sql_template` 说法已按事实更正（见该文件 `:110-114`）
- [metric-pipeline](metric-pipeline.md) —— 冷指标晋升 / 路由层聚合；本文的 §9 T3 与其口径家谱对齐
- [data-model](data-model.md) —— `ontology_metric` / `kpi_catalog` 列清单
- [数据与AI治理规范](../rules/数据与AI治理规范.md) —— 本体版本 / 指标审查 / 路由策略
- [变更 SSOT](../changes/2026-10-28-kpi-metric-technical-form/summary.md) —— 本设计的决策记录与实施计划入口

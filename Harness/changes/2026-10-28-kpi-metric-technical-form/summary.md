# 变更：2026-10-28-kpi-metric-technical-form

- **日期**：2026-10-28（设计）/ 2026-10-29（实施完成）
- **作者**：Claude（与用户逐节确认）
- **Phase**：Phase 6.7 指标技术形态治理
- **状态**：**已实施，待部署验证**（P1 + P1-数据工具链已落地；tier 分布 **A4 / B5 / C3**；prod 迁移/回填未执行，见 §11）
- **分支**：`feat/kpi-metric-technical-form`
- **Spec 路径**：[`docs/superpowers/specs/2026-10-28-kpi-metric-technical-form-design.md`](../../docs/superpowers/specs/2026-10-28-kpi-metric-technical-form-design.md)（608 行）
- **Spec commit**：`4a4f736f`（分支 `docs/kpi-metric-technical-form-spec`，base `main` @ `3ecd780c`）
- **关联变更**：
  - predecessor：[feat-kpi-catalog-governance](../feat-kpi-catalog-governance/summary.md)（kpi_catalog 表与治理元数据）
  - predecessor：[feat-complex-metric-pipeline](../feat-complex-metric-pipeline/summary.md)（辛普森悖论口径基线，`aggregation_order` 语义来源）
  - predecessor：[2026-10-18-kpi-datasource-write-flow-spec](../2026-10-18-kpi-datasource-write-flow-spec/summary.md)（`datasource_id` 写入流；本项闸门条件⑤依赖它）
  - **未合并分支**：[kpi-l1-metric-id](../../../../Harness/changes/kpi-l1-metric-id/summary.md)（`feat/kpi-l1-metric-id` @ `fd84d092`；B1–B5 —— **本项不复用其 B2/B3**，处置见 §2.4）
- **迁移版本**：`0136_metric_structured`（已落地）、`0137_metric_source_unique`（已落地）；分支 head = `0137`，**prod 仍为 `0135`**
- **实跑证据**：[`realdata-run.txt`](realdata-run.txt)（prod 只读 + 隔离测试库回填幂等的完整 stdout）
- **MEMORY**：[`qa-system-kpi-metric-technical-form-design.md`](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-kpi-metric-technical-form-design.md)

---

## 1. 需求

### 1.1 业务背景

业务方提出的原始诉求：

> 「保证业务和治理产生的信息，可以提供本体管理的指标技术形态方面，结合的更好，提高指标的准确性」

拆成可验收的三条：

| # | 需求 | 验收标准（用户视角） |
|---|---|---|
| R1 | 业务/治理侧的口径能进入本体侧的「指标技术形态」 | `ontology_metric` 能承载分子/分母/过滤/粒度/聚合阶，**机器可读**（不再只能写中文） |
| R2 | NL2SQL 用的是**实际配置的**口径，不是硬编码的 | 问句命中某 KPI 时，其口径**动态**进入 plan prompt；改配置即改行为 |
| R3 | 指标结果准确 | 口径一致时走**确定性执行**（0 LLM，答案=治理定义）；口径不一致时**明确示警**而非静默漂移 |

### 1.2 为什么现在做（触发证据）

| 发现 | 证据 | 影响 |
|---|---|---|
| **`ontology_metric` 在 NL2SQL 链路零消费者** | `nl2sql_service.py:34-35` 不 import 它；`nl2sql_prompts.py` 全文 0 个 "etric"；`nl2sql_schema.py:251` `buildSchemaText` 只渲染类；`chat_recall.py` 用 `typeFilter="class"`；`query_plan.py` 注释「formula 没有确定性渲染器」 | **补 12 行 metric 也不会改变任何一条 SQL** —— 问题不是"缺数据"，是"缺消费者" |
| **L1 确定性直通路径早已存在却 100% 失效** | `chat_stream_output.py:207/222` → `chat_service.py:1456/1499/1520`；断点：`_resolveCalculationFeature` 用 `FeatureDefinition.feature_definition == kpi.formula` 等值匹配（**散文描述列 vs 技术公式列**）；dev 库 12×6 等值命中 **0** | 0 LLM / 0 幻觉的直通路径全部流量落回 NL2SQL |
| **口径分散 6 个家，只有 1 个是活的** | `kpi_catalog` 五件套(12,中文) / `kpi_catalog.data_source`(表血缘) / `ontology_metric`(0 行,死) / `feature_definition.calculation_logic`(6 条完整可执行 Oracle SQL，**不进 prompt**) / `feature_definition.feature_definition`(散文，进 prompt) / `ontology_class.property`(54 类) | 正确口径被关在一张与本体无关的表里 |
| **语义匹配第一道门是断的** | dev 库 `semantic_keywords` **12/12 全空**；`KpiCatalogUpdate` / `KpiCatalogRead` **都没有**这两个字段；前端 `KpiCatalogPage.tsx` 无该控件 | Jaccard 恒不命中；只剩 exact-alias（问句须含 KPI code） |
| **缓存三洞** | `kpi_match_cache.py:149` 改名残留 / `:166-167` 冷缓存 no-op / `:201-212` ORM listener 只 log（`seed_kpi_semantic_index.py` 裸 SQL 绕过） | 改了配置运行时行为不变 |
| **本体缺 DWS 层** | dev `ontology_class` 54 类 = ODS 21 + DWD 30 + 空 3，**DWS=0 / ADS=0**；feature 引用的 3 张 DWS 表全不在本体 | 语义层指标（`on_time_rate` 等）在本体里不存在 |

---

## 2. 设计评审

### 2.1 候选方案对比（权威源选型）

| 方案 | 主张 | 优点 | 缺点 | 判定 |
|---|---|---|---|---|
| **A. ontology_metric 中心化** | `kpi_catalog`(治理 SSOT) → `ontology_metric`(技术形态 SSOT) → `ontology_metric_source`(多源类 M:N)；`feature_definition` 降级为执行封装 | 唯一被"本体"承认的技术形态容器（`MetricTab` 有 UI、Neo4j/Milvus 同步链已通、`POST /ontology/metrics` 活的）；扩成真容器成本最低 | 需结构化扩列 + 数据重整 | **✅ 选定** |
| B. feature_definition 中心化 | `calculation_logic` 升为权威，`ontology_metric` 降为投影 | 唯一"跑得通"的口径来源，预计算管线现成 | 语义错位（**特征 ≠ 指标**）；只有 6 行；`entity_type` 单值表达不了复合粒度；`feature_value` 覆盖 1/6 且 stale @2026-09-19 | ❌ |
| C. 新建 `metric_definition` 中枢表 | 一张表统管，三处引用 | 最干净，天生无漂移 | 迁移面最大（3 表读写路径 + DTO 全改）；`ontology_metric` 已是这个角色，再开等于承认上一张选错 | ❌ |

### 2.2 数据模型双轨（第二层选型）

| 方案 | 判定 |
|---|---|
| 纯结构化（只有 measure/filter/grain/aggregation_order） | ❌ 表达不了窗口函数、同环比、分位数、跨表 pivot ⇒ "不限定范围"名不副实 |
| **结构化 + SQL 逃生舱（双轨）** | **✅ 选定**：结构化是**机器轨真相**，`formula` 是**人轨逃生舱**；两者不等 ⇒ 标 `degraded`，Tier A 不可用 |
| 只留 SQL（放弃结构化） | ❌ 无法机器对账（B2 已验证：中文 numerator 抽不出列名 ⇒ 对账恒 `unknown`） |

**裁决规则**：**口径的内容以治理签字为准；口径的技术表达以结构化为准。** 两句不矛盾 —— 前者管"治理 ⇄ 技术"，后者管"技术内部"。

### 2.3 运行时形态（第三层选型）

| 方案 | 判定 |
|---|---|
| 只做 NL2SQL 契约注入 | ❌ 治标：LLM 仍要重新推导口径，漂移风险仍在 |
| 只做确定性执行 | ❌ 治标：问句含范围/分组约束时答非所问 |
| **分级直通 Tier A / B / C** | **✅ 选定**：Tier A=确定性执行(0 LLM) / Tier B=NL2SQL+契约注入 / Tier C=降级现状 |

### 2.4 未合并分支 `feat/kpi-l1-metric-id` 的处置（已决）

分支 4 commit（`66e2542f` B1+B5 / `1b29e139` B2 / `aaba0a43` B3 / `fd84d092` B4），25 文件 +2391/−22，基点 `cc4b8986`；**main 领先 60**。

| 分支件 | 核实结论 | 处置 |
|---|---|---|
| B1 | `_resolveCalculationFeature` 两级解析 —— 正是本设计 Tier A 三级绑定的前两级 | **新分支重写**（按新绑定链） |
| B2 | 挂在 `compileObject`，而该方法**零生产调用方**（唯一 `KnowledgeCompilerService()` 实例在 `knowledge_reconcile_worker.py:60`，只调 `reconcile()`）⇒ **编译期对账在生产永不触发**，而提案把 B2→B3 列为硬依赖 | **弃用**，改为注入时 + 发布时实时对账（§5.3） |
| B3 | 机制正确（唯一 metric→prompt 通道），但**比率型文案写反**：`nl2sql_prompts.py:311`「禁止先聚合后相除以外的写法」字面 = **强制先聚合后相除** = 辛普森悖论错误写法 | **弃用**，按 §5.4 重写 |
| B4 | 分母契约守卫 + `extractSqlIdentifiers`（低层纯函数，本设计三层对账复用） | **✅ 只取这一件** |
| B5 | `/metric` 自动 DRAFT KPI，与本设计无冲突 | 保留（可后置合入） |

**决策：选项 3** —— 只取 B4 的守卫 + `extractSqlIdentifiers` 到新分支；B1/B3 在新分支按本设计重写；**不背 60-commit rebase**。

> ⚠️ **提案原文同一处也写错**：`Harness/changes/2026-10-27-kpi-governance-into-nl2sql-proposal.md:156` 同款倒置表述 —— 该文件在另一分支（`docs/kpi-governance-nl2sql-proposal`），需一并修正。

---

## 3. 数据模型变更

### 3.1 `ontology_metric` 扩展

| 列 | 类型 | 说明 |
|---|---|---|
| `metric_type` | varchar(20) NN default `'MEASURE'` | `MEASURE` / `RATIO` / `DIFFERENCE` / `DERIVED` |
| `numerator_measure` | JSON NULL | 基础度量定义 |
| `denominator_measure` | JSON NULL | 同上；`metric_type=RATIO` 时必填 |
| `aggregation_order` | varchar(24) NULL | `WEIGHTED_RATIO` / `AVG_OF_RATIOS` / `NONE` —— **辛普森开关** |
| `grain_keys` | JSON NULL | `["supplier_code","plant_code","year_month"]` |
| `filter_expression` | Text NULL | 全局过滤谓词 |
| `status` | varchar(20) NN default `'DRAFT'` | `DRAFT` / `PUBLISHED` —— 技术形态自身成熟度 |
| `owner` | varchar(100) NULL | **技术口径负责人**（≠ `kpi_catalog.owner` 的业务负责人） |
| `execution_source` | varchar(16) NULL | `COMPILED` / `ESCAPE_HATCH` / `NONE` —— 本指标是否有可直接执行的只读 SQL |

**measure JSON 形状**（`numerator_measure` / `denominator_measure` 共用）：

```json
{
  "agg": "SUM | COUNT | AVG | COUNT_DISTINCT",
  "expr": "ACTUAL_RECEIPT_DATE - PROMISED_DATE",
  "filter": "IQCRESULT = 'FAIL'",
  "class_id": 12,
  "class_alias": "DWD_INCOMING_INSPECTION"
}
```

**保留不改**：`formula`（Text NN，逃生舱）、`agg_function`（varchar(20)）、`target_class_id`（FK，单值，语义 = `role=BASE` 的类）、`dimension_defaults`（JSON，**零消费者**，另立批次清理）。

> **不加** `version` / `revision_count` —— 变更审计走 `audit_log`（YAGNI）。
> `execution_source` **不做自动嗅探**（不可测、不可复现、会把人写的说明误当可执行）⇒ 显式枚举。

### 3.2 `ontology_metric_source`（新表，多源类 M:N）

```
id         PK
metric_id  FK → ontology_metric.id  ON DELETE CASCADE
class_id   FK → ontology_class.id    ON DELETE RESTRICT
role       varchar(20) NOT NULL   -- NUMERATOR | DENOMINATOR | FILTER | BASE
UNIQUE(metric_id, class_id, role)
INDEX(class_id)
```

> **必要性**：**9/12** KPI 的 `data_source` 含 `+`（多表；3 条单表：`KPI_PAYMENT_ON_TIME` / `KPI_SUPPLIER_DEFECT_RATE` / `KPI_SUPPLIER_FPY`）⇒ `target_class_id` 单值 FK **表达不了那 9 条真实口径**。

### 3.3 `kpi_catalog` 扩展

| 列 | 类型 | 说明 |
|---|---|---|
| `grain_keys` | JSON NULL | 结构化粒度键（业务人员可填，词汇有限） |
| `numerator_columns` | JSON NULL | 列名数组，如 `["ACTUAL_RECEIPT_DATE","PROMISED_DATE"]` |
| `denominator_columns` | JSON NULL | 列名数组 |

> **为什么必须加**：B2 对账只能出 `mismatch|unknown`，根因是中文 numerator 抽不出列名。治理侧能填"我用的是哪几列" ⇒ 机器比对从"中文 vs 结构"降级为"列集合比对"（B4 的 `extractSqlIdentifiers` 直接复用）。
> 中文 `numerator` / `denominator` / `grain` **保留原样**，降级为"人话说明"。

### 3.4 DTO 变更（`schemas.py`）

| DTO | 变更 |
|---|---|
| `OntologyMetricCreate` / `Update` / `Read` | 加 §3.1 的 9 个新字段（camelCase JSON 契约，ORM snake_case） |
| `KpiCatalogCreate` | **已有** `semantic_keywords` / `match_threshold`（无需改） |
| `KpiCatalogUpdate` | **加** `semantic_keywords` / `match_threshold`（现在**创建后不可改**） |
| `KpiCatalogRead` | **加** `semantic_keywords` / `match_threshold`（现在**前端看不到**） |

### 3.5 迁移

| 迁移文件 | 内容 | 长度 |
|---|---|---|
| `0136_metric_structured` | `ontology_metric` 扩列 + `ontology_metric_source` 建表 + `kpi_catalog` 扩列 | 19 字符 ✅ |
| `0137_metric_source_unique` | `UNIQUE(metric_id, class_id, role)` + `INDEX(class_id)` | 27 字符 ✅ |

- 全部新列 nullable / 有默认值 ⇒ downgrade 直接删列，无数据损失
- `ontology_metric_source` 是新表 ⇒ 删表不影响存量
- `kpi_catalog.metric_id` 的 FK（`fk_kpi_catalog_metric`）**已存在**，不动

---

## 4. 接口契约变更

| 端点 | 方法 | 变更 |
|---|---|---|
| `/api/v1/ontology/metrics` | POST/PUT | 请求体加 §3.1 九字段；`metricType=RATIO` ⇒ `denominatorMeasure` 必填 |
| `/api/v1/ontology/metrics/{id}` | GET | 响应加九字段 |
| `/api/v1/ontology/metrics/{id}` | DELETE | 行为不变（CASCADE 删 source 行） |
| `/api/v1/kpi-catalog` | POST | 不变（`semantic_keywords` 已有） |
| `/api/v1/kpi-catalog/{id}` | PUT | 请求体加 `semanticKeywords` / `matchThreshold` |
| `/api/v1/kpi-catalog` | GET | 响应加 `semanticKeywords` / `matchThreshold` |
| `/api/v1/kpi-catalog/{id}/status` | PUT | **状态机收紧**：DRAFT→PUBLISHED 需过发布闸门（§5.5），不满足 → **409 + 明确缺项** |

**新增响应字段（列表/详情）**：`metricId` + **对账 verdict**（`pass` / `mismatch` / `unknown` / 未挂）—— 前端"技术形态挂靠"列的数据源。

**错误码**（新增 1 类，复用现有信封）：
`MSG_KPI_METRIC_GATE_FAILED` —— 载荷列出缺失项（`metric_id` / `metric.status` / `execution_source` / `datasource_id` / `reconcile`），供前端逐条提示。

---

## 5. 实现要点

### 5.1 权威关系

```
kpi_catalog              治理 SSOT    业务语义 + 口径声明（中文）+ 粒度 + 业务负责人
   │ metric_id FK（已存在）
   ▼
ontology_metric          技术形态 SSOT 结构化 measure + 聚合阶 + 粒度键 + 过滤谓词 + 逃生舱
   │ 1:N
   ▼
ontology_metric_source   多源类 M:N   role = NUMERATOR | DENOMINATOR | FILTER | BASE

feature_definition      执行封装（降级）  calculation_logic + 窗口 + 刷新 + feature_value
```

### 5.2 运行时总体路由

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

### 5.3 三层对账与挂点

| # | 对账对 | 基准 | mismatch 行为 |
|---|---|---|---|
| 1 | **技术内部**：结构化 measure ⇄ `formula` SQL 列引用 | **结构化** | 标 `degraded`，Tier A 不可用 |
| 2 | **治理 ⇄ 技术**：治理 `*_columns` / `grain_keys` ⇄ 技术 measure 列集合 | **治理** | **阻断**：不注入 + audit + 告警 |
| 3 | **可执行 ⇄ 结构化**：`execution_source` SQL 列引用 ⇄ 结构化 measure 列 | **结构化** | Tier A 不可用，降 Tier B |

**挂点（修正 B2）**：**注入时实时对账**（Tier B 构建契约时）+ **发布时对账**（闸门）⇒ **不依赖任何编译入口**（`compileObject` 无生产调用方）。

**三档判定**：`pass`（治理列 ⊆ 技术列 ∧ grain 一致 ∧ RATIO 时 `aggregation_order` 非空） / `unknown`（治理侧结构化字段为空 —— 存量主流，注入但标记） / `mismatch`（技术跑偏 / grain 不一致 / RATIO 缺 `aggregation_order` ⇒ **不注入该 KPI**）。

**审计**：`audit_log`，`entity_type='kpi_metric_reconcile'`，**仅在有 finding（mismatch/unknown）时落**（`pass` 不落，避免噪声）；自 commit 同三库对账 `reconcile()`。

### 5.4 Tier B 契约注入

**注入点**：`nl2sql_prompts.py:256` `_buildPlanSystemPrompt` —— 已有 3 个同款 block（`featureCatalogText` / `dictionaryText` / `wikiRulesBlock`），照抄模式。

- 新增第 12 个 `UntrustedLabel.KPI_CONTRACT`（现 11 个，`app/security/prompt_sanitizer.py:51`；`wrapUntrusted` 在 `:118`）⇒ **红队 `ALL_LABELS` 断言须同步**
- 渲染内容：治理五件套 + 聚合阶 + 粒度键 + 源类(role) + **允许的 SQL 形态**
- **比率型文案（防倒置）**：

| `aggregation_order` | 允许且仅允许 | 禁止 |
|---|---|---|
| `WEIGHTED_RATIO` | `SUM(分子)/SUM(分母)` | `AVG(分子/分母)` |
| `AVG_OF_RATIOS` | `AVG(分子/分母)` | `SUM(分子)/SUM(分母)` |
| `NULL` ∧ `metric_type=RATIO` | **不注入该 KPI**（闸门已拦） | — |

### 5.5 发布闸门

```
KPI: DRAFT → PUBLISHED  ⟸ 必须同时满足：
  ① metric_id IS NOT NULL
  ② metric.status = PUBLISHED
  ③ metric.execution_source ≠ NONE
  ④ 对账 = pass（unknown 不允许发布）
  ⑤ datasource_id IS NOT NULL        ← dev 现状 12/12 全 NULL
不满足 → 409 + 明确缺哪一项
```

`ontology_metric.status: DRAFT → PUBLISHED` 走同一套闸门。

### 5.6 缓存动态性修复（`kpi_match_cache.py`）

| 洞 | 位置 | 修法 |
|---|---|---|
| C1 改名残留 | `:149` `old = self._by_code.pop(kpi_code, None)` | 改为**按 `id` 失效**（id 跨改名稳定） |
| C2 冷缓存 no-op | `:166-167` `if not self._loaded: return` | 置 dirty 标记 → 下次查询 lazy rebuild |
| C3 raw SQL 绕过 | `:201-212` listener 只 log | `seed_kpi_semantic_index.py` 改走 service；或脚本末尾显式 `onKpiChanged(None)` + `warmUp` |

### 5.7 `kpi_catalog.data_source` 精确化

现状：只被 `kpi_datasource_suggestion_service.py:98-105` 当字符串做**名字模糊匹配**。
改法：`source_table` 是**裸表名**（实测 `DWD_SUPPLIER_PRICE_LIST_HEADER`）⇒ 按 `+` 分割即可**精确 join** `ontology_class.source_table`（22 表引用 / **13 命中 = 59%**，distinct 表 10 张）。

### 5.8 前端

| 文件 | 改动 |
|---|---|
| `KpiCatalogPage.tsx`（462 行） | 表单：`metricId`（本体指标选择器）、`semanticKeywords`（Tags）、`grainKeys`/`numeratorColumns`/`denominatorColumns`；列表：新增"技术形态挂靠"列（metric 名 + 对账 verdict） |
| `MetricTab.tsx`（336 行） | 新增：`metricType` / `status` / `owner` / `aggregationOrder` / `grainKeys` / `numeratorMeasure` / `denominatorMeasure` / `executionSource`；结构化编辑器复用 `dimensionDefaults` 的 string↔JSON 模式（`:113-118`），**不引入新组件库** |

---

## 6. 测试

### 6.1 单测（纯函数）

| 文件 | 覆盖 |
|---|---|
| `test_metric_contract_reconcile.py` | 三层对账 pass/mismatch/unknown；grain 不一致；RATIO 缺 `aggregation_order`；列集合 ⊆ 判定；`execution_source` 判定 |
| `test_kpi_contract_block.py` | 契约块渲染；**比率型文案正反断言**（`WEIGHTED_RATIO ⇒ 只允许 SUM/SUM`）；mismatch 排除；unknown 标记；`wrapUntrusted` label 正确 |
| `test_kpi_match_cache_dynamic.py` | C1 / C2 / C3 各一测 |
| `test_metric_publish_gate.py` | 闸门 5 条件逐条 + 缺项报明确信息 |
| `test_metric_sql_binding.py` | Tier A 三级绑定解析 |

### 6.2 集成测试（真实 PG 5434 + neo4j-test）

| 文件 | 覆盖 |
|---|---|
| `test_kpi_metric_lifecycle.py` | 建 metric → 挂 KPI → 对账 → 发布 → 回滚全链路 |
| `test_l1_tier_a_direct.py` | **端到端：问句 → Tier A 直通 → 断言结果正确且 LLM 调用次数 == 0** |
| `test_kpi_contract_injection.py` | NL2SQL 端到端：契约文本**实际出现在 plan prompt 里** |

### 6.3 🔴 防假绿红线（逐条对应本仓既有踩坑）

| 红线 | 对应坑 |
|---|---|
| Tier A 测试必须断言 **LLM 调用次数 == 0** | 否则可能悄悄退化成 NL2SQL 仍绿 |
| 注入测试必须断言 **prompt 里真的出现契约文本**，不能只断言"函数被调用" | mock 内断言假绿 |
| 对账/闸门测试必须断言 **"是谁拒的"**，不能只断言状态码 | 状态码相同的假绿家族（12 处） |
| **unit 与 integration 必须分两个 pytest session** | 同 session 撞 seed ⇒ 假红 |
| 涉及 Milvus / Neo4j 的测试用隔离实例 | 裸 pytest 会删线上 |
| 每条守卫断言配**反向**（"删掉守卫应当变红"） | 守卫自检 |

### 6.4 真实数据验证脚本（开发门禁）

`backend/scripts/kpi_metric_realdata.py`（S1–S9，拆 4 模块：入口 / `_db` 读库 helper / `_inject` 契约注入 / `_lib` 常量与输出语义）—— 幂等、可复现，输出：迁移与结构、回填预览、三层对账逐 tier、发布闸门、契约注入端到端、exact-alias 可用性、缓存动态性、裸表名 join、回填落库幂等。

**目标库守卫（四层）**：

1. DSN **只**来自 `--database-url` 或 `KPI_METRIC_REALDATA_DATABASE_URL` —— 不读 `TEST_DATABASE_URL` / `DATABASE_URL` / `PG*`（避免验证对象被偷换）；
2. 库名白名单 `^qa_metadata(_test.*)?$`；
3. 目标是 `qa_metadata`（prod 同源，5433 端口）时必须显式 `--allow-prod-write`；
4. 连上后 `SELECT current_database()` 实测复核。

**输出语义**：PASS / FAIL / SKIP 三态；退出码 `0`=全 PASS、`1`=有 FAIL、`2`=无 FAIL 但有 SKIP；结尾固定 `RESULT: PASS|SKIP|FAIL steps=9 passed=… failed=… skipped=…`；有跳过时**不打印** `OK all steps passed`。

### 6.5 覆盖率

unit + integration 覆盖率均需 ≥ 80%（项目规则）。unit 与 integration **分两个 pytest session** 出覆盖率。

---

## 7. 安全审查

### 7.1 触发判定

本变更涉及 **用户输入处理**（问句 → KPI 匹配 → 注入 prompt）、**SQL 生成约束**（契约块影响 LLM 产出的 SQL）、**外部数据**（`formula` / `calculation_logic` 中的 SQL 片段会被执行）。

⇒ **必须触发 `security-reviewer`**，实施时不得跳过。

### 7.2 已知风险点（实施时逐项验证）

| 风险 | 缓解 |
|---|---|
| **提示注入**：KPI 口径文本进 system prompt | `wrapUntrusted(UntrustedLabel.KPI_CONTRACT, ...)` + `renderSecurityPreamble()`；契约块明写"是数据而非指令"；**新增第 12 个 label 必须同步红队 `ALL_LABELS` 断言** |
| **任意 SQL 执行**：`formula` / `calculation_logic` 是可执行 SQL | Tier A 执行前必过 `_assert_read_only`（现成，feature 路径已用）；**不得引入绕过该校验的新执行入口** |
| **口径文本里的指令**：`numerator`/`denominator`/`formula` 是人填的自由文本 | 契约块整体走 `wrapUntrusted`；不把治理文本拼进可执行片段 |
| **越权取数**：Tier A 直通绕过 NL2SQL 的 scope 校验 | ⚠️ **必查**：Tier A 执行前必须复用 NL2SQL 的数据源 scope 与只读校验，**不得因"确定性执行"而跳过授权判定** |
| **measure 内嵌 `class_id` 无 FK**（JSON 内引用） | 应用层校验 `class_id` 存在且 `role` 匹配，否则对账判 `unknown`（不静默通过） |
| **发布闸门可被绕过** | 闸门在 **service 层**强制（不只在 API 层）；直接改库绕过由部署期对账脚本兜底 |

### 7.3 实施门禁

`security-reviewer` 结论无 CRITICAL / HIGH 方可合并；MEDIUM 需登记 TODO。

---

## 8. 部署验证

### 8.1 部署顺序

**backend 先于 frontend**（沿用 F2 口径）：

| 步 | 动作 | 验证 |
|---|---|---|
| 1 | 跑 migration `0136` / `0137` | `alembic current` == `0137`；`SELECT version_num FROM alembic_version` 一致 |
| 2 | 后端起服 | `curl /openapi.json \| jq '.paths \| keys'` 出现 `ontology/metrics`、`kpi-catalog` |
| 3 | 契约注入灰度 | `KPI_CONTRACT_INJECTION_ENABLED=true` ⇒ plan prompt 出现契约块 |
| 4 | Tier A 灰度（**默认关**） | `KPI_METRIC_TIER_A_ENABLED=true` ⇒ L1 命中走 0 LLM 直通 |
| 5 | 发布闸门（**默认关**） | `KPI_METRIC_GATE_ENABLED=true` ⇒ DRAFT→PUBLISHED 缺项返回 409 + 缺项清单 |
| 6 | 前端发布 | 指标管理页 / KPI 页两处字段可见可编辑 |
| 7 | 真实数据验证 | `backend/scripts/kpi_metric_realdata.py --database-url <DSN>` 跑通。按库/模式区分（**三态退出码**）：**测试库 + `--apply-backfill` ⇒ 全 PASS / exit 0**；**测试库只读 ⇒ 无 FAIL 但有 SKIP（S9 恒 SKIP）⇒ exit 2**；**prod 只读 ⇒ S1 FAIL（迁移未应用）/ exit 1**（FAIL 优先于 SKIP）；「全跳过」也落 `2`，不会被读成通过 |

> ⚠️ 步骤 1 前须处理 **alembic 版本漂移**：alembic 读 `DATABASE_URL`，pytest 读 `TEST_DATABASE_URL`；换分支后 `alembic_version` 残留要 `UPDATE`。
> ⚠️ 迁移/测试**不得触碰生产 Milvus / Neo4j**。

### 8.2 灰度开关

| 开关 | 默认 | 作用 |
|---|---|---|
| `KPI_CONTRACT_INJECTION_ENABLED` | true | Tier B 契约注入 |
| `KPI_METRIC_TIER_A_ENABLED` | **false** | Tier A 直通（先只读对比两路答案一致性） |
| `KPI_METRIC_GATE_ENABLED` | **false** | 发布闸门（先观察再阻断） |

### 8.3 回滚

三个开关各自独立退回；migration 可 downgrade（新增列全 nullable）；Tier A 关开关即退回 NL2SQL。

### 8.4 数据前置（需业务方提供，代码不能编）

| 项 | 现状（2026-10-29） | 谁提供 |
|---|---|---|
| 12 条 KPI 的 `semantic_keywords` | 全空 | 业务方 + `seed_kpi_semantic_index.py`（**修 C3 后才生效**） |
| 12 条 KPI 的 `datasource_id` | **12/12 NULL** | 业务方（直接影响闸门条件⑤） |
| 12 条 `ontology_metric` 行 | prod **0 行**；回填 PLAN 已生成（12 新建 / 0 匹配，审核清单见 [`docs/kpi-metric-backfill-review.md`](../../../docs/kpi-metric-backfill-review.md)），**尚未落 prod** | 业务方审核清单后**人工**发起 `kpi_metric_backfill.py --apply --allow-prod-write` |

> 回填脚本默认**预览**，显式 `--apply` 才落库；目标是 `qa_metadata` 时还必须加 `--allow-prod-write`（写前自动 `pg_dump` 备份）。
>
> ⚠️ **运维调用方式已变**（Task 15）：`kpi_metric_backfill.py` **必须显式传 `--database-url`**（不读环境变量，因为本机默认 `DATABASE_URL` 指向 5432 的 OpenMetadata PG）；`seed_kpi_semantic_index.py` 同样改为显式 `--database-url`（env 仅兜底）+ `--allow-prod-write` + `--dry-run`。旧的「裸跑脚本」调用方式**不再有效**。

---

## 9. 关联

- **Spec**：[`docs/superpowers/specs/2026-10-28-kpi-metric-technical-form-design.md`](../../docs/superpowers/specs/2026-10-28-kpi-metric-technical-form-design.md)
- **Wiki**：[`Harness/wiki/kpi-metric-technical-form.md`](../../wiki/kpi-metric-technical-form.md)（架构参考 + §14 实施结果）
- **实跑证据**：[`realdata-run.txt`](realdata-run.txt)（A：prod 只读 S1 FAIL 为预期 / exit 1；B：隔离测试库回填 S9 幂等 9/9 PASS / exit 0；C：回填后只读复跑 0 FAIL 但有 SKIP / **exit 2**）
- **回填审核清单**：[`docs/kpi-metric-backfill-review.md`](../../../docs/kpi-metric-backfill-review.md)（2026-10-29 从 prod 只读生成，12 新建 / 0 匹配）
- **Rules**：[`Harness/rules/开发流程规范.md`](../../rules/开发流程规范.md)（10 阶段工作流）、[`Harness/rules/变更记录强制规范.md`](../../rules/变更记录强制规范.md)
- **Memory**：[`qa-system-kpi-metric-technical-form-design.md`](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-kpi-metric-technical-form-design.md)
- **关联变更**：[feat-kpi-catalog-governance](../feat-kpi-catalog-governance/summary.md)、[feat-complex-metric-pipeline](../feat-complex-metric-pipeline/summary.md)、[2026-10-18-kpi-datasource-write-flow-spec](../2026-10-18-kpi-datasource-write-flow-spec/summary.md)、[kpi-l1-metric-id](../kpi-l1-metric-id/summary.md)（未合并分支，**部分被本项取代**）
- **业务口径来源**：[`docs/data-knowledge/采购域.md`](../../../docs/data-knowledge/采购域.md)（§431-465 KPI 定义 / §550-581 表血缘）
- **提案（另一分支，同处需一并修正）**：`Harness/changes/2026-10-27-kpi-governance-into-nl2sql-proposal.md:156`

---

## 10. 实施结果（2026-10-29）

### 10.1 交付范围

**已落地（P1 + P1-数据工具链）**：

| 块 | 内容 |
|---|---|
| 迁移 | `0136_metric_structured`（`ontology_metric` 扩 9 列 + `ontology_metric_source` 建表 + `kpi_catalog` 扩 3 列）、`0137_metric_source_unique`（UNIQUE 三元组 + `class_id` 索引）；分支 head = `0137` |
| 服务层 | `kpi_metric_spec.py`（治理/技术两侧的规范化 Spec）、三层对账引擎、`kpi_contract_builder.py`（动态匹配 + 对账门禁 + 比率形态写死）、`kpi_metric_publish_gate.py`（5 项硬条件 + 409 缺项明细） |
| 注入链 | 第 12 个 `UntrustedLabel.KPI_CONTRACT`；`prompt/plan/service/chat` 四层透传；`KPI_CONTRACT_INJECTION_ENABLED` 灰度 |
| 缓存 | C1 按 id 失效 / C2 冷缓存重建 / C3 TTL 兜底绕过 ORM 写入 |
| 前端 | 本体指标结构化编辑器；KPI 目录页「技术形态挂靠」列 + 语义关键词 / 匹配阈值 / 治理列 |
| 数据工具链 | `kpi_metric_backfill.py`（默认预览、`--apply` 落库、`--allow-prod-write`、自动备份）；`docs/kpi-metric-backfill-review.md`（prod 只读生成，12 新建 / 0 匹配）；`seed_kpi_semantic_index.py` 接入守卫 |
| 验证 | `backend/scripts/kpi_metric_realdata.py`（S1–S9）+ 单测（守卫 / 输出三态 / 覆盖率） |

**未落地（按设计分期）**：Tier A 确定性直通（P2，默认关且未接线）、问句约束检测（P2）、结构化 → SQL 编译器（P3）、`feature_definition.metric_id` FK（P3）。

### 10.2 实测数字（见 [`realdata-run.txt`](realdata-run.txt)）

| 项 | 实测 |
|---|---|
| 目录 KPI 数 | 12（tier A 4 / tier B 5 / tier C 3） |
| 回填落库（隔离测试库） | 第 1 次 `created=12`；第 2 次 `created=0 updated=12`（幂等）；`ontology_metric` 行数 `0 → 12 → 12` |
| 对账 verdict | **12/12 `mismatch`**（PLAN 刻意保留治理列 vs 物理列偏差，T15-L） |
| 契约块（现状） | **0 字符**（全 mismatch ⇒ 对账拦截，不注入） |
| 契约块（治理列对齐后模拟） | 575 字符；prompt 4361 字符，含标题 / 比率形态指引 / KPI code |
| exact-alias | `KPI_SUPPLIER_OTD` 命中，`confidence = 1.0` |
| Jaccard 自然语言路径 | 「供应商准时交付率是多少」最高分 **0.0** ⇒ 不可达（遗留项 L1） |
| 裸表名 join | 22 个表引用 / 命中 13（59.1%） |
| 回填后只读复跑（隔离测试库） | `RESULT: SKIP`（8 PASS / 1 SKIP：S9 只读恒 SKIP）⇒ **exit 2**；S2.2 12/12 挂靠；S3.4 期望=实际 |
| prod 迁移状态 | `alembic_version = '0135'` ⇒ S1 FAIL（**预期**），S2–S9 SKIP |
| prod `system_config` | 28 行，KPI 开关 0 行（只读跑前/跑后一致） |

> **S2.2′ 的口径（T16-11，复审后升级为真归属校验）**：严格集合相等（`linked == plan`）在**回填前不成立** —— S2 在 S9 之前跑，未回填时 `linked = 0`，而脚本必须在回填前后都能跑。因此采用可判定的不变量：**要么 0 条（未回填），要么全量挂靠（不允许部分挂靠）**，挂靠 code 不得越出 PLAN，且**每条挂靠的 `ontology_metric.metric_name` 必须等于 PLAN 的 `metricName`**（`fetchLinkedMetricNames` 连接 `kpi_catalog.metric_id → ontology_metric`）。只数非空 `metric_id` 会放过「挂到了别的指标」；负向对照实测：把 `KPI_SUPPLIER_OTD` 改成指向另一条 metric ⇒ `FAIL S2.2 … 挂错 ['KPI_SUPPLIER_OTD']`，还原后 12/12 PASS。

### 10.3 测试与静态检查

| 项 | 结果 |
|---|---|
| 单测（本任务四文件：realdata_lib / s3 / spec / reconcile） | 89 passed；新模块覆盖率 `kpi_metric_spec.py` **100%**、`kpi_metric_realdata_lib.py` **96%**、`kpi_metric_realdata_s3.py` **100%**（合并 98%，134 语句 / 3 未覆盖） |
| KPI 单测扫面 | 371 passed |
| 集成（6 文件：backfill_apply / contract_injection / publish_gate_api / persistence / schema / publish_gate_http） | 71 passed（unit 与 integration **分两个 session**） |
| ruff | 新增文件 0 告警；改动文件无新增告警（`All checks passed!`） |
| 数据库 | 真实 PostgreSQL（5434 `qa_metadata_test_t16`，`alembic upgrade head` 到 0137）；无 sqlite |

`kpi_metric_realdata.py` / `_db.py` / `_s3.py` / `_s4.py` / `_inject.py` 是**真库入口脚本**，由 S1–S9 实跑覆盖（不适用 pytest 覆盖率口径）；纯函数层（`_lib` / `_s3` / `_spec`）已按上表覆盖。

### 10.4 三层防线的判别力边界（复审实测，务必如实理解）

| 退化 | 谁抓到 |
|---|---|
| `_checkRole` 的列比对被整体删掉 / 只删一个方向 | **单测**（整体置空 ⇒ `test_kpi_metric_reconcile.py` 8 failed）；S3.4 只抓「两个方向都丢」 |
| `MeasureSpec.columns()` 退化（技术列抽取崩） | **只有单测**（置空 ⇒ 14 failed）；S3.1 / S3.3 / S3.4 **全绿**（被 `grain_mismatch` 兜住） |
| 治理列与物理列错配 / 偏差登记手误 | S3.1 / S3.3（verdict 与偏差集合对不上） |

> **结论**：S3.4 是**端到端兜底**，不是唯一防线；真正的判别力在**单测层**。
> S3.4 自身的已知局限（刻意不修，靠单测兜）：① 只丢一个方向的列级 code 时无判别力；
> ② 技术列抽取与引擎**同源**（都用 `MeasureSpec.columns()`），该输入退化时 S3.4 会跟着绿。

**S2.2′ 归属校验的已知局限（Minor，登记不修）**：归属键是 `ontology_metric.metric_name`
（模型 `unique=True`、DB 有 `uq_metric_name`），而该字段**可通过 API 改名**
（`OntologyMetricUpdate.metric_name` → `PUT /api/v1/ontology/metrics/{id}`）。
改名后即使 `metric_id` 归属**正确**，S2.2′ 也会报 `FAIL … 挂错 [...]` ——
语义上把「改名」报成「挂错」。**当前不可达**（prod `ontology_metric = 0`、前端不编辑该字段），
且下次 `--apply` 的 `_desiredValues` 会把名字写回（自愈）。
⇒ 若将来开放改名，需把 S2.2′ 的文案区分「名称漂移」与「挂错」，或改用不可变的归属键。

---

## 11. 上线前必读：两条硬结论与风险清单

### 11.1 硬结论 ①：回填 ≠ 自动生成正确 SQL（范围边界）

本特性当前 NL2SQL **只消费结构化口径的「形状」**：聚合阶、粒度键、来源类 + `role`、允许的 SQL 形态（以及治理五件套的展示文本）。

**不消费结构化 `expr`** —— Tier A（确定性直通）属 **P2**，结构化 → SQL 编译器属 **P3**。因此：

> **回填 12 条指标后，LLM 仍需自己推导 SQL；结构化 `expr` 不会自动变成可执行 SQL。**
> 不要因为「库里有结构化口径」就认为「SQL 已经由机器生成」。

### 11.2 硬结论 ②：灰度开关默认值与真实风险窗口

prod `system_config` **没有任何 KPI 开关行** ⇒ 走代码默认：

| 开关 | 代码默认 | 现状含义 |
|---|---|---|
| `KPI_CONTRACT_INJECTION_ENABLED` | **True** | 满足匹配 + 对账条件即注入（无需翻开关） |
| `KPI_METRIC_GATE_ENABLED` | **False** | 发布闸门只观察、不阻断 |
| `KPI_METRIC_TIER_A_ENABLED` | **False** | Tier A 未接线，保持关 |

**实测修正（重要）**：回填后 **12/12 全部 `mismatch`**（PLAN 刻意保留「治理列 = 业务口径、技术列 = 物理列」的偏差，见 T15-L）⇒ **今天即使落库，契约块仍为空、注入实际不生效**（`kpi_contract_builder.py` 对 `mismatch` 直接 `return None`）。风险窗口在**业务方审核并对齐治理列之后**才打开 —— 那一刻起注入自动开始，**不需要任何人翻开关**。

⚠️ **这个风险是「延后 + 静默」的**：它不会在回填当天暴露，而是会在**某天某人把治理列改对的时候**自动打开 —— **不需要翻任何开关、不需要重启、不需要发版**，那一刻**没有任何部署信号**会提示「注入刚刚生效了」。危险不在回填当天，而在**某次看似无害的治理数据修正**。

⇒ 因此上线前的二选一（下条）**不能因为「今天块是空的」而推迟**：正因为它是静默的，才必须在回填/迁移之前就把开关策略定下来。

**上线前二选一**（须由人决定并执行）：

1. **先写 `KPI_CONTRACT_INJECTION_ENABLED=false`**，业务方审核对齐后再开启；或
2. **接受默认（True）**，并接受「治理列对齐即注入」的隐式生效。

⚠️ 本任务**只记录清单与风险，未改 prod 任何开关**（只读跑前/跑后 `system_config` 均为 28 行、KPI 开关 0 行）。

### 11.3 上线前清单

- [ ] prod 迁移 `0136` / `0137`（**尚未执行**）
- [ ] 业务方审核 [`docs/kpi-metric-backfill-review.md`](../../../docs/kpi-metric-backfill-review.md)（12 新建 / 0 匹配）
- [ ] **人工**发起 `kpi_metric_backfill.py --database-url <prod DSN> --apply --allow-prod-write`（S9 语义已在隔离测试库证明幂等；prod 未跑）
- [ ] 补齐 `semantic_keywords` / `datasource_id`（12/12 仍为 NULL）
- [ ] 决定 §11.2 的开关策略（先关后开 / 接受默认）
- [ ] 治理列对齐后重跑 `kpi_metric_realdata.py`，确认 verdict 从 `mismatch` 转 `pass` 且契约块非空
- [ ] `security-reviewer` 结论 + compose 冒烟（SSOT 清单另两项，**不在 Task 16 范围**）

### 11.4 运维调用方式变更（必须通知运维）

| 脚本 | 旧调用 | 新调用 |
|---|---|---|
| `kpi_metric_backfill.py` | 直接裸跑（读环境变量） | **必须** `--database-url <DSN>`；写 `qa_metadata` 还须 `--allow-prod-write`；默认只预览，`--apply` 才落库 |
| `seed_kpi_semantic_index.py` | 直接裸跑 | `--database-url <DSN>`（env `DATABASE_URL` 仅兜底）+ `--allow-prod-write`（prod）+ `--dry-run` 可预览 |
| `kpi_metric_realdata.py` | —（新增） | `--database-url <DSN>`；写库/回填还需 `--apply-backfill`；prod 只读也须 `--allow-prod-write`（按库名的显式 opt-in） |

> 为什么强制显式：本机 `backend/app/config.py:58` 的默认 `DATABASE_URL` 指向 **5432 的 OpenMetadata PG**，裸跑会打错库。

---

## SSOT 校验清单（实施合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（三层选型各 ≥ 2）
- [x] 第 3 段迁移文件名 ≤ 32 字符（`0136_metric_structured` 19 / `0137_metric_source_unique` 27）
- [ ] 第 7 段触发 `security-reviewer` 并填结论（**未做 —— 不在 Task 16 范围**）
- [ ] 第 8 段 docker compose 冒烟命令 + 输出贴出（**未做 —— 不在 Task 16 范围**）
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki 文档已更新（新增 `wiki/kpi-metric-technical-form.md` + 修正 `metric-pipeline.md` / `nl2sql-engine.md` 的 `sql_template` 漂移）
- [x] 至少 1 条 MEMORY 索引已在 MEMORY.md 添加
- [x] `backend/scripts/kpi_metric_realdata.py` 已跑通（prod 只读 + 隔离测试库，见 [`realdata-run.txt`](realdata-run.txt)）

> **控制器裁定**：**可合并到主干，但不得据此判定生产就绪。**
>
> 未完成的闸门逐条列出（不含糊）：
> ① `security-reviewer` 结论**未出**；
> ② compose 冒烟**未做**；
> ③ prod 仍在 alembic **`0135`**，**未迁移**（`0136`/`0137` 未应用）；
> ④ prod **未回填**（须业务方审核 `docs/kpi-metric-backfill-review.md` 后人工发起）；
> ⑤ 治理列**未对齐** ⇒ 契约块为空（12/12 `mismatch`，见 §11.2）。

---

## 遗留项 L1（P2 提案，本期不实施）

**现状（实测）**：L1 直通（KPI 关键词 → 本体指标）的软匹配分支**不可达**——
关键词与指标散文做 Jaccard 时，长词的值只有 `0.0909`（如
「供应商准时交付率」= 1 / 11），远低于阈值 `0.75`；因此实际只有
**exact-alias**（关键词与 `semantic_keywords` 完全相等）能命中。
补 `semantic_keywords` 数据也不改变这一点：**别把阈值下调当解法**。

**P2 提案**：在 Jaccard 之前加「**完整包含 ⇒ 1.0**」的短路——
关键词整串出现在指标名 / 描述 / `semantic_keywords` 任一字段中即视为
命中（相似度按 1.0 记），长短词一视同仁；阈值保持 `0.75` 不动。

**为什么留到 P2**：本期（方案 A）的结论是「L1 只走 exact-alias」；
加包含短路会**扩大 NL2SQL 召回面**，需要单独灰度 + 对照评估，
不属于本次回填/生成脚本的范围。

# 生产 KPI 回答修复 —— 逐步执行方案（1114 之后）

> **执行者**：生产宿主机（192.168.205.82）上的运维/业务方。
> **开发机只提供本文件 + 取证分析**（生产内网隔离、无 SSH 免密通道）。
> **本方案不含任何 DDL**：全部是 DML / 治理数据写入 / 开关行写入 ⇒ 不触发「生产 DDL 须单独评审」规则。
> **唯一需要人工逐条过目的写操作**是 §4 的治理数据写入（Step 1）与 §6 的开关写入（Step 3）。

---

## 0. 一句话结论（两个症状同源）

| 现象 | 根因（已在生产日志与代码中逐字取证） |
|---|---|
| 问「…采购订单完成率…直接返回：指标「采购订单完成率」（定义）」⇒ **Tokens 0 / 成本 0**，只回口径没数字 | 问句命中 KPI（containment）⇒ 但 `ontology_metric.execution_source` 为 **NULL** ⇒ `planKpiExecution` 第 4 步短路判 **TIER_C(execution_source_missing)** ⇒ **不调 LLM**，只回口径 ⇒ Tokens 0。<br>（2026-11-14 12:52 之前的形态是 **TIER_LEGACY**：`KPI_METRIC_TIER_A_ENABLED` 关 ⇒ `_resolveCalculationFeature` 按 `metric.formula` 等值找 `feature_definition`，生产该表 0 行 ⇒ `data=None` ⇒ 同样只回口径。两者都终结整轮，**都不降级到 LLM**。） |
| 问「…订单完成率，每个基地分析一下」⇒ 正常出数但**公式与指标定义不符** | 两个原因叠加：① `semantic_keywords` 只有全名「采购订单完成率」⇒ 简称问法**匹配不上**（diagnose `matches: []`）⇒ 无 KPI 命中 ⇒ 直接走 LLM；② `KPI_CONTRACT_INJECTION_ENABLED` 行**显式为 false**（2026-10-30 手工关的）⇒ **LLM 拿不到签字口径** ⇒ 自编公式 |

⇒ **生产现状：KPI 口径一条都没进 LLM（开关被显式关着），命中后也一条都不能确定性执行（`execution_source` 全空）。**

> 📌 上表是 **2026-11-14 21:0x 修复前**的状态。**三处写操作已执行并通过验收**，修复后的实测证据见 **§9**。

---

## 1. 修复链条（顺序不可颠倒；全部取证已完成，见 §1.4）

```
① KPI 治理数据 PUT（/api/v1/kpi-catalog/1）
   简称关键词（让 Q2 命中 containment）+ 分子分母自由文本写进真实列名（让 LLM 知道查哪两列）
        ↓  必须先于 ③
② 本体指标技术形态 PUT（/api/v1/ontology/metrics/1）
   executionSource=COMPILED（把 Q1 从 Tier C 拉出来）
   metricType=RATIO + aggregationOrder=WEIGHTED_RATIO（让契约块给出「禁止 AVG(分子/分母)」的形态指引）
   formula=SUM(IS_ONTIME) / COUNT(PO_NO)（去掉会进 prompt 的中文伪列名；让 B3 的 formula_semantics 干净通过 —— §1.6.3/§1.6.4）
        ↓  必须先于 ③
③ 契约注入开关 → true（/api/v1/admin/system-config/KPI_CONTRACT_INJECTION_ENABLED，行已存在 ⇒ API PUT）
        ↓
④ 验收（diagnose ×2 + 前端重问 ×2 + runtime 事件时间线）

✗ 不做：time_grain 回填（实测已回填过）    ✗ 不动：Tier A 闸（已开）/ containment 闸（已开）/ 发布闸门 / 向量开关
```

**为什么顺序不能换**

- **③ 必须在 ①② 之后**：契约注入只对「已匹配到 KPI 且走了 LLM」的问句起作用。`execution_source` 为 NULL 时 Q1 落在 **TIER_C** ⇒ 根本不调 LLM ⇒ 先翻开关 Q1 仍是 0-token。
- **② 的 `executionSource` 是把 Q1 拉出 Tier C 的唯一动作**：`planKpiExecution` 第 4 步 `if not source: return TIER_C(execution_source_missing)`。补上后：source 非空 → 过 `has_datasource` → 问句含显式时间「9月」而 `formula` 无 `{time_scope}`、`time_column` 空 ⇒ `time_param_ready=false` ⇒ **TIER_B(explicit_time_scope)** ⇒ 走 LLM + 契约。
- **① 的关键词必须在 ③ 之前**：`semantic_keywords` 不含简称 ⇒ Q2 `matches: []` ⇒ 契约块匹配不到 ⇒ 注入为空。
- **② 的 RATIO/WEIGHTED_RATIO 是防「辛普森悖论」的必要项**：`ratioSqlGuidance` 只在 `metric_type == RATIO` 时产出形态指引；当前 `metric_type=MEASURE` ⇒ 无指引 ⇒ LLM 可能写 `AVG(分子/分母)`。
- **① 里的自由文本必须写列名**：契约块**不渲染** `numerator_columns` / `denominator_columns`（§1.4.5）⇒ 不写进自由文本，LLM 拿不到 `IS_ONTIME` / `PO_NO`。
- **③ 不被对账阻断**：`verdict=unknown`（不是 `mismatch`）⇒ 注入放行；**前提是不动结构化列**（动了会触发 `_checkRole` ⇒ MISMATCH ⇒ 阻断，见 §1.4.4）。

---

## 1.4 全部取证结果（2026-11-14 20:56～21:0x CST 实测，四组全部到齐）

### 1.4.1 `system_config` 三个开关的真实行值

| key | value | updated_time |
|---|---|---|
| `KPI_CONTRACT_INJECTION_ENABLED` | **false** | 2026-10-30 07:04:15 |
| `KPI_MATCH_CONTAINMENT_ENABLED` | **true** | 2026-11-14 12:05:07 |
| `KPI_METRIC_TIER_A_ENABLED` | **true** | 2026-11-14 12:52:58 |

⇒ 🔴 **契约注入是「被显式关掉」，不是「行缺失 fail-closed」**（行存在、值 false、时间戳 2026-10-30）。
这比原假设更严重：**口径从来就没进过 LLM，是一次显式关闭的结果**（不是没人配）。

### 1.4.2 数据行现状

`ontology_metric` id=1：

| 列 | 值 |
|---|---|
| metric_name / metric_alias | `PO_COMPLETE_RATE` / `采购订单完成率` |
| metric_type | **MEASURE** |
| aggregation_order | **空** |
| **execution_source** | **NULL** 🔴 |
| status | PUBLISHED |
| time_grain | `month` ✓（已回填过） |
| time_column | **空** |
| formula | `SUM(及时到货行数) / COUNT(采购订单总行数)`（中文业务式，非可执行 SQL） |
| grain_keys | `["YMONTH, ITEM_CODE, SUPPLIER_CODE, PUR_SITE_CODE, BUYER_CODE"]`（单元素逗号串，渲染后逗号分隔仍可读） |
| numerator_measure | `{"agg":"SUM","expr":"IS_ONTIME","filter":null,"class_id":null,"class_alias":null}` |
| denominator_measure | **空** |

`kpi_catalog` `KPI_PUR_001`（id=1）：

| 列 | 值 |
|---|---|
| status / metric_id / datasource_id | PUBLISHED / 1 / 1 |
| match_threshold | 0.75 |
| **semantic_keywords** | **`{采购订单完成率}`**（只有全名 ⇒ 简称问法命中不了） |
| numerator / denominator | `Σ及时到货行数` / `Σ采购订单总行数`（**没写聚合函数、没写列名** ⇒ LLM 无法据此生成 SQL） |
| grain / unit / time_grain | `物料+供应商+基地+月份` / `%` / `month` ✓ |
| numerator_columns / denominator_columns | `["SUM(IS_ONTIME)"]` / `["COUNT(PO_NO)"]` |
| grain_keys | `[]` |

`feature_definition`：**0 行**。`ontology_metric_source`（metric_id=1）：**0 行** ⇒ 无源类绑定 ⇒ 本批不做确定性执行。

### 1.4.3 运行时事件时间线（`audit_log`，`entity_type='kpi_runtime_event'`）

| 时间(UTC) | kpi | tier | reason | match_method |
|---|---|---|---|---|
| 12:12:22 – 12:13:43（6 条） | KPI_PUR_001 | LEGACY | tier_a_disabled | containment |
| 12:43:23 / 12:43:35（2 条） | KPI_PUR_001 | LEGACY | tier_a_disabled | containment |
| **12:54:03 / 12:55:57（2 条）** | KPI_PUR_001 | **C** | **execution_source_missing** | containment |

⇒ **与开关时间戳逐字自洽**：containment 12:05 开 ⇒ 12:12 起 containment 生效；
Tier A 12:52 开 ⇒ 12:54 起从 LEGACY 变成 C（`execution_source` 为 NULL ⇒ Tier C）。

⇒ 🔴 **用户报的「只回口径 + Tokens 0」是 12:52 之前的旧行为**（日志里 `L1 …未命中 FeatureDefinition` 即 legacy 指纹）。
**当前真实状态**：Q1（全名）→ Tier C ⇒ 0-token「暂不可确定性计算」；Q2（简称）→ **无 KPI 命中** ⇒ 纯 LLM 且**无契约** ⇒ 公式自编。

### 1.4.4 `diagnose` 两个问句

Q1 =「统计一下9月的采购订单完成率，每个基地分析一下」：

```
injectionEnabled=false  injectionBlockEmpty=true  ambiguous=false
matches[0]: kpiCode=KPI_PUR_001, matchMethod=containment, confidence=0.9,
            metricId=1, metricType=MEASURE, executionSource=null,
            verdict=unknown, degraded=true, tierAEligible=false, tier=C,
            reasons=["governance_token_ambiguous","governance_grain_absent"],
            findings: 治理 token 命中多个候选，无法判定：SUM(IS_ONTIME), COUNT(PO_NO)
                     治理侧未声明结构化粒度键
            injected=false
```

Q2 =「统计一下9月的订单完成率，每个基地分析一下」：`matches: []`（**无命中**）

⇒ **`verdict = unknown` 不是 `mismatch`** ⇒ 契约注入**不会被对账门禁挡住**（`kpi_contract_builder._buildEntry` 只在 `mismatch` 时返回 None）。
⇒ `governance_token_ambiguous` 的副作用：`_reconcileGovernanceWithResolution` 在角色 token 歧义时**跳过 `_checkRole`** ⇒ 当前**不会**触发「治理侧声明了分母列但技术侧缺分母度量」的 MISMATCH。
⚠️ **因此不要动 `numeratorColumns` / `denominatorColumns` / `grainKeys`** —— 一旦治理 token 不再歧义，`_checkRole` 启用 ⇒ 技术侧 `denominator_measure` 为 NULL ⇒ 判 **MISMATCH** ⇒ 注入被阻断（这是最容易踩的坑）。

### 1.4.5 契约块实际渲染什么（`kpi_contract_builder.renderContractEntry`）

渲染字段：`kpi_name` / `business_definition` / **`kpi.numerator`（治理侧自由文本）** / **`kpi.denominator`（治理侧自由文本）** / `kpi.grain` / **`metric.grain_keys`（技术侧）** / `unit` / `owner` / `metric_name` / `metric_type` / `agg_function` / `aggregation_order` / **`allowed_sql_shape` + `forbidden_sql_shape`** / `source_classes` / `verdict` / degraded 警告。

🔴 **不渲染** `numerator_columns` / `denominator_columns` ⇒ **`SUM(IS_ONTIME)` / `COUNT(PO_NO)` 这两个真实列名不在契约块里** ⇒ 必须写进 `kpi.numerator` / `kpi.denominator` 自由文本，否则 LLM 根本不知道该用哪两列。
🔴 `allowed_sql_shape` 只在 `metric_type == RATIO` 且 `aggregation_order ∈ {WEIGHTED_RATIO, AVG_OF_RATIOS}` 时才产出（`ratioSqlGuidance`）⇒ 当前 `metric_type=MEASURE` + `aggregation_order` 空 ⇒ **无形态指引** ⇒ LLM 可能写 `AVG(分子/分母)`（辛普森错误）⇒ 必须改成 RATIO + WEIGHTED_RATIO。
🔴 `source_classes` 来自 `ontology_metric_source` ⇒ **当前 0 行 ⇒ 契约块里没有源表名** ⇒ 表名只能靠 NL2SQL 的 schema 召回（datasource_id=1）。

---

## 1.5 已由生产 `governance-summary` 证实的结论（2026-11-14 20:56 CST 实测）

实测输出：

```json
{"kpis":{"total":16,"byStatus":{"DRAFT":3,"PUBLISHED":13}},
 "metrics":{"total":17,"byExecutionSource":{"NONE":9,"ESCAPE_HATCH":8},"byStatus":{"PUBLISHED":1,"DRAFT":16}},
 "reconcile":{"windowDays":7,"mismatchCount":0,"passCount":0,"unknownCount":0,"lastMismatchAt":null,"reconcileError":null},
 "runtime":{"windowDays":7,"total":10,"byTier":{"C":2,"LEGACY":8},
            "degradedReasons":[{"reason":"tier_a_disabled","count":8},{"reason":"execution_source_missing","count":2}],
            "chatAnswers":36,"hitRate":0.2778,"runtimeError":null},
 "syncHealth":{"publishedKpiCount":13,"milvusKpiCount":1,"neo4jKpiCount":16,"kpiTotal":16,"syncHealthError":null},
 "gates":{"publishGateEnabled":false,"tierAEnabled":true,"syncEnabled":true}}
```

| 观察 | 含义（已回代码逐条核过） |
|---|---|
| `gates.tierAEnabled = true` | 🔴 **Tier A 闸已经是开的**。`gates` 走 `isKpiMetricTierAEnabled()`（`kpi_metric_switches.py:114`）= env 杀闸 true **且** `system_config` 行 `KPI_METRIC_TIER_A_ENABLED` 为真 ⇒ **该行存在且为 true**。⚠️ 本文档早期版本「三个灰度开关行缺失 ⇒ 实际=关」的推断**对 Tier A 不成立** |
| `runtime.byTier = {LEGACY:8, C:2}` | 8 条 `tier_a_disabled` = 开关打开**之前**的历史事件；2 条 `execution_source_missing` = 打开**之后**的新事件（Tier C ⇒ 0 token） |
| `metrics.byExecutionSource = {NONE:9, ESCAPE_HATCH:8}` | 🔴 **没有任何 metric 是 COMPILED** ⇒ Tier A 直通对所有 KPI 都不可能 |
| `metrics.byStatus = {PUBLISHED:1, DRAFT:16}` | 只有 1 个已发布 metric |
| `reconcile` 三个计数**全 0** | 近 7 天**一条对账落库行都没有**。`passCount` 只是「实际落库的 pass 行数」（干净 pass 从不落库，见 `_governanceReconcile` docstring）⇒ ⚠️ **本面板不可用作 verdict 判据**（0 行 ≠ 无 mismatch）。per-KPI verdict 只能看 `diagnose` |
| `syncHealth`: Milvus **1** / PUBLISHED **13**（Neo4j 16 齐） | 存量向量欠账 12 条。写钩子只在 create/update/delete 触发 ⇒ 存量 PUBLISHED 不会自动补。**与本次回答 bug 无关**（向量匹配开关关），下批补 |

### 由上表推出的关键不确定项（Step 0 必须先测出来）

用户报的症状（「只回口径 + Tokens 0」，日志含 `L1 metric 链路未命中 FeatureDefinition…`）是 **legacy 路径的指纹**，发生在 Tier A 开关打开**之前**。现在 Tier A 已开，Q1 的行为取决于 metric 1 的 `execution_source`：

- `ESCAPE_HATCH` ⇒ Q1 走 **Tier B(explicit_time_scope)**（`formula` 无 `{time_scope}` 占位符 ⇒ 不满足 Tier A 直通）⇒ LLM 路径但契约块仍空 ⇒ 症状变成「出数但公式不符」（症状 B），0-token 症状消失
- `NONE` / 空 ⇒ Q1 走 **Tier C** ⇒ 仍 0 token，只是文案从「（定义）」变成「（暂不可确定性计算）」

⇒ **先测 metric 1 的 `execution_source`，再决定写操作序列**。

---

## 1.6 读码新发现（2026-11-14 21:3x）：契约有**两套注入面、两套门禁**，外加三个必须避开的雷

### 1.6.1 两套注入面（`diagnose` 只覆盖其中一套 ⇒ 别把它当全部）

| 面 | 装配器 | 注入到哪里 | 门禁 |
|---|---|---|---|
| **B3** | `kpi_contract_reconcile.buildKpiContractBlock`（`nl2sql_service.py:306`） | **NL2SQL 的 plan system prompt**（选类与聚合之前） | 注入总闸 + containment 命中 + **metric 存在** + **3 道编译期对账**（`formula_semantics` / `grain_coverage` / `formula_mixing`）任一 **mismatch ⇒ 该 KPI 不注入**；unknown 放行 |
| **chat Tier B** | `kpi_contract_builder.buildKpiContractBlock`（`chat_service.py:1452`） | chat 侧 Tier B 契约块 | 注入总闸 + containment 命中 + metric 存在 + **三层对账 verdict ≠ mismatch**（`unknown` 放行） |

⇒ `diagnose` 走的是 **chat Tier B** 那套（`kpi_diagnose.py:395` 调 `buildKpiContractBlockWithCodes`）⇒ **它绿了不等于 B3 也会注入**。B3 那套的多一道 `grain_coverage` 必须单独确认（§2.4.2 的 (A)）。

> ✅ **实测补充（2026-11-14 21:3x）**：本批把 `IS_ONTIME` / `COUNT(PO_NO)` 写进了**治理自由文本**（`kpi.numerator` / `kpi.denominator`），而 **chat Tier B 面渲染的正是这两个自由文本** ⇒ **chat 面自己就能把列名带进 prompt**，列名**不是**靠 B3 送进来的。⇒ **B3 是双保险，不是唯一命脉**；即便 B3 因任何原因不注入，只要 chat 面在，列名口径仍能到达 LLM（实测 §9.3 的 SQL 已证明）。

### 1.6.2 🔴 雷 1：**`ontology_metric_source` 一旦绑上，契约反而被堵死**

链路（逐行核对）：

1. `resolutionForMetric` → `sourcePropertiesForMetric`（`kpi_alignment_resolver.py:233`）：`ontology_metric_source` 无行 ⇒ 返回 `()`。
2. 无源类但有治理 token ⇒ `kpi_metric_spec.py:104` 返回 `ResolutionOutcome(unknownTokens=全部 token)` ⇒ **所有治理 token 记为 unknown（歧义）**。
3. `_reconcileGovernanceWithResolution`（`kpi_metric_reconcile.py:487`）：`governance.numerator_columns`/`denominator_columns` 命中 unknown ⇒ `_hasAmbiguousTokens` 为真 ⇒ **跳过 `_checkRole`** ⇒ 不产 mismatch ⇒ verdict = `unknown` ⇒ **注入放行**（§1.4.4 的 `governance_token_ambiguous` 就是它）。

**反过来**：一旦给 metric 1 绑上源类 ⇒ token 变得可解析 ⇒ `_checkRole` 启用 ⇒
- `numerator` 侧：`technique.numerator` 有值（`numerator_measure = {"agg":"SUM","expr":"IS_ONTIME"}`）；
- `denominator` 侧：`technique.denominator` 为 **NULL** ⇒ `_checkRole` 判 **MISMATCH**（`REASON_GOVERNANCE_MEASURE_ABSENT`）；
⇒ verdict = **mismatch** ⇒ **两层注入面都不注入** ⇒ 症状 B 修不好。

⚠️ 而且即使补上 `denominator_measure`，治理 token 是 `"SUM(IS_ONTIME)"` / `"COUNT(PO_NO)"`（**带函数名**），`_resolveOneToken` 只按标识符精确匹配 ⇒ 解析不出来 ⇒ 落 `missingTokens` ⇒ `governance_token_unresolvable` = **MISMATCH** ⇒ 同样被堵。

⇒ **本批明确不动 `ontology_metric_source`（保持 0 行）**。要真正把源表钉进契约，是「治理结构化列 + 结构化度量 + 源类映射」三件一起补的独立立项（见 §8 遗留清单），不是本次的补丁。

### 1.6.3 🔴 雷 2：自由文本里的 **ASCII 标识符必须 ⊆ `metric.formula` 的标识符**

`_checkFormulaSemantics`（`kpi_contract_reconcile.py:69`）：
`extractSqlIdentifiers(kpi.numerator + " " + kpi.denominator) ⊆ extractSqlIdentifiers(metric.formula)`，
否则判 **mismatch ⇒ B3 不注入**。

- `extractSqlIdentifiers` 的正则 `[A-Za-z_][A-Za-z0-9_]*` **只认 ASCII** ⇒ 纯中文抽不出任何标识符；`sum`/`count` 等函数名在停用词表里。
- 当前 `metric.formula = "SUM(及时到货行数) / COUNT(采购订单总行数)"` ⇒ 指标集合为 **∅** ⇒ 走 `if not kpiTokens or not metricTokens:` 分支 ⇒ verdict = **unknown** ⇒ 放行。
  ⇒ **这也意味着：只要 `metric.formula` 保持中文，自由文本里可以写任何 ASCII 标识符都不会被拦。**
- 但 §3.3 会把 `metric.formula` 换成真 SQL ⇒ 指标集合变成 `{is_ontime, po_no}` ⇒ 自由文本里**多一个 ASCII 标识符（如表名 `DWS_PO_COMPLETE_RATE`）就判 mismatch**。

⇒ **铁律：自由文本里只许出现 `IS_ONTIME` 与 `PO_NO` 两个 ASCII 标识符，其余一律中文。**
（表名不用写 —— `IS_ONTIME` 全本体唯一，LLM 只能选 `DWS_PO_COMPLETE_RATE`，见 §2.4.1。）

### 1.6.4 🔴 雷 3：`metric.formula` 里的**中文伪列名会进 prompt**，会污染 LLM 的 SQL

B3 的渲染（`kpi_contract_reconcile.py:296`）会输出一行 `技术公式: {metric.formula}（聚合函数 {agg_function}）`。
当前这行的内容是 `技术公式: SUM(及时到货行数) / COUNT(采购订单总行数)（聚合函数 SUM）` ——
**「及时到货行数」「采购订单总行数」不是任何真实列名**，LLM 看到它可能照抄出中文列名。

⇒ §3.3 把 `metric.formula` 改成真 SQL（`SUM(IS_ONTIME) / COUNT(PO_NO)`），既消掉这行噪声，又让 §1.6.3 的子集检查变成**干净通过**（`{is_ontime,po_no} ⊆ {is_ontime,po_no}`）而不是靠 unknown 蒙混。

### 1.6.5 写操作安全性小结（为什么只动 4 个字段）

| 字段 | 动？ | 依据 |
|---|---|---|
| `kpi_catalog.semantic_keywords` | ✅ 动 | 只进匹配器，不进任何对账 |
| `kpi_catalog.numerator` / `denominator`（自由文本） | ✅ 动 | `governanceTokensOf` **只读结构化列**（`kpi_metric_reconcile.py:148`）⇒ 自由文本不参与三层对账；只被 B3 的 `formula_semantics` 扫描（§1.6.3） |
| `kpi_catalog.grain` | ⚠️ 仅在 (A) 报缺口时加词 | `grain_coverage` 门禁；`grainCoverageGaps` 只在 `dimension_defaults` 非空时才有内容 |
| `kpi_catalog.numerator_columns` / `denominator_columns` / `grain_keys` | ❌ **不动** | 一改就可能消歧义 ⇒ 触发 `_checkRole` ⇒ MISMATCH ⇒ 注入被堵 |
| `kpi_catalog.formula` | ❌ 不动 | 动了才触发 `formula_mixing`（且 `metric.formula` 一改，两者自然不等 ⇒ 该门禁自动消失） |
| `ontology_metric.metric_type` / `aggregation_order` / `execution_source` | ✅ 动 | 路由与形态指引 |
| `ontology_metric.formula` | ✅ 动 | §1.6.4（噪声消除 + 子集检查干净通过） |
| `ontology_metric.numerator_measure` / `denominator_measure` / `grain_keys` / `time_column` / `status` | ❌ **不动** | 填了会把 verdict 推向 mismatch / 触发发布闸门与流转矩阵；本批要的是稳定落在 Tier B |
| `ontology_metric_source` | ❌ **不动** | §1.6.2 |

---

## 2. Step 0 — 取证（只读，5 分钟，不改任何东西）

### 2.1 准备 admin token

诊断/治理接口都是 admin-only（`requireAdmin`），需要 `Authorization: Bearer <jwt>`。

**推荐做法（宿主机无需任何工具）**：浏览器登录生产前端 → F12 → Network → 任一请求 → 复制 `Authorization: Bearer xxx` 整段。

```bash
export TOKEN='<粘贴 Bearer 后面的 token>'
echo "token 长度: ${#TOKEN}"
```

> 若宿主机有 `python3`，也可现场登录（口令不回显、不进 argv）：
> ```bash
> read -rs ADMIN_PW; export ADMIN_PW
> TOKEN=$(python3 - <<'PY'
> import json, os, urllib.request
> req = urllib.request.Request(
>     "http://127.0.0.1:8000/api/v1/auth/login",
>     data=json.dumps({"username": "admin", "password": os.environ["ADMIN_PW"]}).encode(),
>     headers={"Content-Type": "application/json"},
> )
> print(json.load(urllib.request.urlopen(req))["accessToken"])
> PY
> )
> unset ADMIN_PW
> ```

### 2.2 查开关实际生效值（3 个开关 + 2 个旁路开关）

```bash
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -A -F" | " -t' <<'SQL'
SELECT key, value FROM system_config
 WHERE key LIKE 'KPI_%' OR key LIKE 'ENABLE_%'
 ORDER BY key;
SQL
```

**判读**：
- 灰度开关（`KPI_CONTRACT_INJECTION_ENABLED` / `KPI_MATCH_CONTAINMENT_ENABLED`）**行缺失 ⇒ 实际生效 = 关**（fail-closed）。
- ⚠️ `KPI_METRIC_TIER_A_ENABLED` 例外：**实测该行存在且为 true**（`gates.tierAEnabled=true`）⇒ **Tier A 已开**，别再执行开闸动作。
- `KPI_SYNC_ENABLED` 行缺失 ⇒ 实际 = **开**（唯一默认开）。
- `KPI_MATCH_VECTOR_ENABLED` 需要 env 也为 true，**本方案不开、不动**。
- 若上面查询**完全没有** `KPI_CONTRACT_INJECTION_ENABLED` 这一行 ⇒ 就是「契约注入从未生效」的实证（§6 用 INSERT 建行）。

### 2.3 查 KPI_PUR_001 治理侧现状

```bash
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -A -F" | " -t' <<'SQL'
SELECT id, kpi_code, status, metric_id, datasource_id, match_threshold,
       semantic_keywords, numerator, denominator, grain, unit, time_grain,
       numerator_columns, denominator_columns, grain_keys
  FROM kpi_catalog WHERE kpi_code = 'KPI_PUR_001';

SELECT id, metric_name, metric_alias, metric_type, aggregation_order,
       execution_source, status, time_grain, time_column, formula,
       numerator_measure, denominator_measure, grain_keys
  FROM ontology_metric WHERE id = 1;

SELECT s.id, s.metric_id, s.class_id, s.role, c.class_name, c.class_alias, c.source_table
  FROM ontology_metric_source s LEFT JOIN ontology_class c ON c.id = s.class_id
 WHERE s.metric_id = 1 ORDER BY s.role, s.class_id;

SELECT count(*) AS feature_definition_rows FROM feature_definition;
SELECT count(*) AS feature_definition_enabled FROM feature_definition WHERE is_enabled;
SQL
```

**判读（每一条都决定后续动作）**：

| 观察到的值 | 含义 | 动作 |
|---|---|---|
| `semantic_keywords` 只有「采购订单完成率」全名 | 弱问法（含「订单完成率」）**命中不了** | Step 1 必须补简称 |
| `numerator` / `denominator` 为空 | 契约块里**没有分子分母行** ⇒ LLM 仍会自编公式 | Step 1 必须补 |
| `metric_type = MEASURE` 且 `aggregation_order` 空 | 契约块**没有「允许且仅允许的 SQL 形态」** ⇒ SUM/AVG 形态不管 | Step 1 改 `RATIO` + `WEIGHTED_RATIO` |
| `execution_source` 为空 | Tier A 闸一开就判 TIER_C ⇒ 依然 0-token | Step 1 必须先补 |
| `ontology_metric_source` **0 行** | 无源类 ⇒ 无法走确定性编译（Tier A 真执行），本次只做「口径进 LLM」 | 记入 §7 待办，**不影响本次修复** |
| `feature_definition` **0 行** | legacy L1 无锚点 ⇒ Tier A 闸（已开）不会抢走任何正在服务的答案 | 正常 |
| `feature_definition` **> 0 行** | 有 KPI 正在走 legacy 确定性路径 | ⚠️ Tier A 闸**已经开了**（实测）⇒ 说明这些 KPI 已被改道；需逐个确认它们现在落在哪个 Tier（`audit_log` 的 `kpi_runtime_event`） |

### 2.4 查全部 KPI 的匹配短语（开 containment 前的必查项）

containment 判定是「**KPI 的短语 ⊂ 问句**」，只要某 KPI 的短语过宽（如「订单」），**任何提到订单的问句都会命中它**。

```bash
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -A -F" | " -t' <<'SQL'
SELECT kpi_code, kpi_name, status, semantic_keywords
  FROM kpi_catalog ORDER BY kpi_code;

SELECT c.key, c.value FROM system_config c
 WHERE c.key = 'KPI_MATCH_CONTAINMENT_ENABLED';
SQL
```

**判读**：逐条看 `semantic_keywords` / `kpi_name` 是否有 **≤ 3 字的宽泛词**（如「订单」「完成率」「交付」）。有 ⇒ 那些 KPI 一旦命中就会被推进 Tier 判定 ⇒ 对**尚未治理**（`execution_source` 空 / `metric_id` 空）的 KPI，会变成 TIER_C 0-token 答案。
⇒ **处置**：把这些 KPI 暂时改 `status = DRAFT`（匹配缓存只索引 `PUBLISHED`，见 `kpi_match_cache.py:175`），或先按 §4 同样治理它们。改 DRAFT 走 `PUT /api/v1/kpi-catalog/{id}`（`status` 流转矩阵允许 PUBLISHED→DRAFT）。

### 2.4.1 两项残余只读检查 —— ✅ **已跑完（2026-11-14 21:2x CST），结论见下**

```bash
# (i) 误命中面：13 条 PUBLISHED KPI 的短语里有没有比「订单完成率」更宽的词
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -A -F"|" -t' <<'SQL'
SELECT kpi_code, kpi_name, status, semantic_keywords::text
  FROM kpi_catalog WHERE status = 'PUBLISHED' ORDER BY kpi_code;
SQL
```

**实测结果（13 条）**：`semantic_keywords` **12 条为空 `{}`**，只有 `KPI_PUR_001` = `{采购订单完成率}`。
⇒ **误命中面为零**：containment 的短语 = 名称 + keywords + 别名；其余 12 条的名称（供应商准时交付率、采购周期、供应商 NCR 率…）都不可能 ⊂ 「统计一下9月的订单完成率，每个基地分析一下」。
⇒ **不需把任何 KPI 改 DRAFT**，§3.2 的关键词写入不会被别的 KPI 抢走。

```bash
# (ii) 列名映射：IS_ONTIME / PO_NO 在本体里有没有落到某个源类的物理列
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -A -F"|" -t' <<'SQL'
SELECT c.id, c.class_name, c.class_alias, c.source_table, p.property_name, p.source_column
  FROM ontology_class c
  LEFT JOIN ontology_property p ON p.class_id = c.id
 WHERE p.source_column ILIKE '%IS_ONTIME%'
    OR p.source_column ILIKE '%PO_NO%'
    OR p.property_name ILIKE '%IS_ONTIME%'
    OR p.property_name ILIKE '%PO_NO%';
SQL
```

**实测结果（13 行）**：
- `PO_NO` 出现在 **12 个类**（`ADS_SUPPLIER_ORDER_DETAIL` / `DWD_ARRIVAL_NOTICE_LINE` / `DWD_ARRIVAL_ORDER_DTL` / `DWD_GOODS_RECEIPT_DTL` / `DWD_GOODS_RECEIPT_LINE` / `DWD_PURCHASE_ORDER` / `DWD_PURCHASE_ORDER_DTL` / `DWD_PURCHASE_ORDER_LINE` / `DWD_PURCHASE_INVOICE_LINE` / `DWD_PURCHASE_RETURN_DTL` / `DWD_REQUISITION_ORDER_LINK` / `DWS_PO_COMPLETE_RATE` / `DWS_PO_COMPLETE_RATE_PROD`）。
- 🔴 **`IS_ONTIME` 全本体只有 1 处**：`DWS_PO_COMPLETE_RATE`（`source_table = DWS_PO_COMPLETE_RATE`，同时挂 `PO_NO` 与 `IS_ONTIME` 两个属性）。

⇒ **两个结论**：
1. ✅ 写 `SUM(IS_ONTIME)` / `COUNT(PO_NO)` 有据，LLM 的 schema 召回找得到这两列。
2. 🔴 **目标表被「列名唯一性」隐式钉死**：LLM 要算 `SUM(IS_ONTIME)` 就**只能**用 `DWS_PO_COMPLETE_RATE`（唯一暴露 `IS_ONTIME` 的类），而该类同时暴露 `PO_NO` ⇒ 分母也在这张表。
   ⇒ **不需要（也不应该）在契约里写表名**：表名一旦以 ASCII 标识符出现在自由文本里，会触发 §1.6 的「雷 2」把契约整块挡掉。

### 2.4.2 写操作前最后一条只读校验（4 个 SELECT，决定要不要多写一个字段）

> **为什么还要这一轮**：契约有**两套注入面、两套门禁**（§1.6）。chat 侧那套已由 `diagnose` 证明放行；**B3（注入 nl2sql plan prompt）那套**还有一道 `grain_coverage` 门禁要看 `ontology_metric.dimension_defaults`——它是**唯一还没验过、且可能反向阻断注入**的门。

```bash
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -A -F"|" -t' <<'SQL'
-- (A) metric 1 技术形态全貌（B3 三道门禁 + Tier 路由都读这些列）
SELECT id, metric_type, aggregation_order, execution_source, agg_function,
       formula, time_grain, time_column, filter_expression,
       dimension_defaults::text, numerator_measure::text, denominator_measure::text,
       grain_keys::text, status
  FROM ontology_metric WHERE id = 1;

-- (B) KPI 1 治理侧（formula 决定 formula_mixing；owner 决定写 ACL 是否放行）
SELECT id, kpi_code, status, metric_id, datasource_id, owner, version,
       formula, numerator, denominator, grain, time_grain,
       grain_keys::text, numerator_columns::text, denominator_columns::text,
       semantic_keywords::text, business_definition
  FROM kpi_catalog WHERE id = 1;

-- (C) 契约源类映射必须为 0 行（见 §1.6 雷 1：一旦 >0 ⇒ token 不再歧义 ⇒ role 校验激活 ⇒ denominator_measure 为 NULL ⇒ MISMATCH ⇒ 契约被堵）
SELECT count(*) AS metric_source_rows FROM ontology_metric_source WHERE metric_id = 1;

-- (D) 目标类 DWS_PO_COMPLETE_RATE 的全部属性（确认分组列/时间列/指标列都在这张表）
SELECT c.id, c.class_name, c.class_alias, c.source_table, c.object_type,
       c.valid_from, c.valid_to, p.property_name, p.source_column, p.data_type
  FROM ontology_class c
  LEFT JOIN ontology_property p ON p.class_id = c.id
 WHERE c.class_name IN ('DWS_PO_COMPLETE_RATE', 'DWS_PO_COMPLETE_RATE_PROD')
 ORDER BY c.class_name, p.id;
SQL
```

**判读**：
- **(A) `dimension_defaults` 为 NULL/空** ⇒ `grainCoverageGaps` 直接返回空 ⇒ B3 的 grain 门禁无 finding ⇒ **按 §3 原样执行**。
- **(A) `dimension_defaults` 非空** ⇒ 把返回的键与 `kpi.grain`（物料+供应商+基地+月份）比一遍：缺哪个键就把对应词补进 `grain`（**只加不减**，加词只会让缺口变小），再执行 §3。
- **(B) `formula` 非空且与 `metric.formula` 逐字相同** ⇒ §3.3 把 `metric.formula` 改成 SQL 后两者自然不等 ⇒ `formula_mixing` 门禁自动消失（该检查只在**相等**时判 mismatch）。
- **(B) `owner` 有值且不是 admin 的名字** ⇒ `updateKpi` 的 ACL（`assertCanModify`）可能 403 ⇒ 换 admin 账号的 token。
- **(C) 必须为 0** ⇒ 若 >0，先停下：说明 token 已经不歧义，§3.2 的自由文本写法要重新算（走 §1.6 雷 1 的分支）。
- **(D) 缺 `PUR_SITE_CODE`（基地）或缺日期列** ⇒ LLM 生不出「按基地 + 9 月」的 SQL ⇒ **先别开契约注入**，回来一起定。

### 2.5 诊断接口（两个问句各跑一次）

⚠️ **URL 里不能直接放中文** —— uvicorn/h11 会直接回 `Invalid HTTP request received.`（这正是之前取证的失败原因）。必须用 `--data-urlencode`。

```bash
Q1='统计一下9月的采购订单完成率，每个基地分析一下'
Q2='统计一下9月的订单完成率，每个基地分析一下'

curl -sS -G 'http://127.0.0.1:8000/api/v1/admin/kpi/diagnose' \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode "question=$Q1" | python3 -m json.tool 2>/dev/null || \
curl -sS -G 'http://127.0.0.1:8000/api/v1/admin/kpi/diagnose' \
  -H "Authorization: Bearer $TOKEN" --data-urlencode "question=$Q1"

curl -sS -G 'http://127.0.0.1:8000/api/v1/admin/kpi/diagnose' \
  -H "Authorization: Bearer $TOKEN" --data-urlencode "question=$Q2"
```

**判读要点**：

| 字段 | 期望（修前） | 说明 |
|---|---|---|
| `injectionEnabled` | `false` | 契约注入总闸实际生效值 |
| `injectionBlockEmpty` | `true` | 口径块是空的 ⇒ 病根 |
| `matches[]` | Q1 有命中、KPI_PUR_001；Q2 空 | Q2 空 ⇒ 弱问法命中不了 |
| `matches[].verdict` | `unknown`（**不是 `mismatch`**） | `mismatch` ⇒ 治理列与技术列打架 ⇒ 注入被阻断，**必须先修数据** |
| `matches[].tier` | `LEGACY` | Tier A 闸关 ⇒ 一律 LEGACY |
| `matches[].executionSource` | 空 / `NONE` | Step 1 要补的字段 |
| `matches[].degraded` | `true` | 正常（治理列不全） |

> 若 `Q1` 的 `matches` 也是空 ⇒ 说明连全名问句都命中不了（`semantic_keywords` 与问句 ngram 交集为空），此时 Step 1 的关键词补充就更关键。

---

## 3. Step 1 — 治理数据修复（写入，走 API，带审计）

### 3.1 先备份这两行（可回滚的凭据）

```bash
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -A -F" | " -t' <<'SQL' > /tmp/kpi_pur_001_backup.txt
SELECT 'KPI', id, kpi_code, status, metric_id, datasource_id, semantic_keywords::text,
       numerator, denominator, grain, unit, time_grain,
       numerator_columns::text, denominator_columns::text, grain_keys::text
  FROM kpi_catalog WHERE kpi_code = 'KPI_PUR_001';
SELECT 'METRIC', id, metric_name, metric_type, aggregation_order, execution_source, status,
       time_grain, time_column, formula, numerator_measure::text,
       denominator_measure::text, grain_keys::text
  FROM ontology_metric WHERE id = 1;
SQL
cat /tmp/kpi_pur_001_backup.txt
```

### 3.2 KPI 目录：`PUT /api/v1/kpi-catalog/1`

`KPI_ID = 1`（§1.4.2 实测）。

```bash
cat > /tmp/kpi_pur_001_patch.json <<'JSON'
{
  "semanticKeywords": ["采购订单完成率", "订单完成率", "采购完成率", "及时到货率"],
  "numerator": "SUM(IS_ONTIME)：按期、按量完成交付的采购订单行数（及时到货行数）",
  "denominator": "COUNT(PO_NO)：当期应交付的采购订单行数（采购订单总行数）",
  "grain": "物料+供应商+基地+月份"
}
JSON

curl -sS -X PUT "http://127.0.0.1:8000/api/v1/kpi-catalog/1" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data-binary @/tmp/kpi_pur_001_patch.json
```

**要点（每条都有代码依据）**：
- `semanticKeywords` 是**整表覆盖**（不是追加）⇒ 必须**保留原值「采购订单完成率」**再合并简称。少一个「订单完成率」⇒ Q2 仍然 `matches: []`（containment 判「KPI 短语 ⊂ 问句」，全名 ⊄ 简称问句）。
- 🔴 **`numerator` / `denominator` 必须写进真实列名**（`SUM(IS_ONTIME)` / `COUNT(PO_NO)`）：契约块**只渲染治理侧自由文本**，不渲染 `numerator_columns` / `denominator_columns`（§1.4.5）⇒ 不写列名，LLM 拿不到该查哪两列。原值「Σ及时到货行数 / Σ采购订单总行数」既没聚合函数也没列名。
- 🔴 **不要动 `numeratorColumns` / `denominatorColumns` / `grainKeys`**（保持原值）：治理 token 目前是 **ambiguous** ⇒ `_reconcileGovernanceWithResolution` **跳过 `_checkRole`** ⇒ verdict 停在 `unknown`（不阻断注入）。一旦不再歧义，`_checkRole` 启用 ⇒ 技术侧 `denominator_measure` 为 NULL ⇒ 判 **MISMATCH** ⇒ **注入被彻底阻断**（§1.4.4）。
- `grain` 保留原值「物料+供应商+基地+月份」（与问句「每个基地」相容；改短反而丢信息）。⚠️ 仅当 §2.4.2 的 (A) 显示 `dimension_defaults` 非空且有缺口时，才在此基础上**加词**（只加不减）。
- 🔴 **自由文本里不许出现表名等第三个 ASCII 标识符**（§1.6.3）：只写 `IS_ONTIME` 与 `PO_NO`，其余全部中文。表名不写也无所谓 —— `IS_ONTIME` 全本体唯一，LLM 只能选 `DWS_PO_COMPLETE_RATE`（§2.4.1）。
- 该 PUT 会：写 `audit_log` + `kpi_catalog_history`、`revision_count+1`、**刷新该 KPI 的匹配缓存**（新 `semantic_keywords` 立即生效，不用重启）、并触发 Milvus/Neo4j 同步钩子（best-effort，失败不影响主流程）。

### 3.3 本体指标技术形态：`PUT /api/v1/ontology/metrics/1`

```bash
cat > /tmp/metric_1_patch.json <<'JSON'
{
  "metricType": "RATIO",
  "aggregationOrder": "WEIGHTED_RATIO",
  "executionSource": "COMPILED",
  "formula": "SUM(IS_ONTIME) / COUNT(PO_NO)"
}
JSON

curl -sS -X PUT "http://127.0.0.1:8000/api/v1/ontology/metrics/1" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data-binary @/tmp/metric_1_patch.json
```

**要点**：
- `metricType=RATIO` + `aggregationOrder=WEIGHTED_RATIO` ⇒ 契约块会多出**唯一防「辛普森悖论」的一行**：
  `允许且仅允许的 SQL 形态：SUM(分子)/SUM(分母)；禁止：AVG(分子/分母)`。
  （`ratioSqlGuidance` 只对 `metric_type == RATIO` 生效 —— `kpi_contract_builder.py:ratioSqlGuidance`；当前 metric_type=MEASURE ⇒ **没有任何形态指引**，LLM 会自选。）
- 🔴 `executionSource=COMPILED` 是**把 Q1 从 Tier C 拉出来**的唯一动作：当前 `execution_source` 为 NULL ⇒ `planKpiExecution` 第 4 步短路判 **TIER_C(execution_source_missing)** ⇒ 0 token 且**契约永远用不上**（Tier C 不调 LLM）。
  补上之后：source 非空 → 过 `has_datasource`（datasource_id=1 ✓）→ 问句含显式时间「9月」而 `formula` 无 `{time_scope}` 占位符、`time_column` 为空 ⇒ `time_param_ready=false` ⇒ **TIER_B(explicit_time_scope)** ⇒ 走 LLM + 契约。
  **为什么不选 ESCAPE_HATCH**：ESCAPE_HATCH 的语义是「`formula` 原文即可执行 SQL」，而本指标的 `formula` 是中文业务式 ⇒ 是撒谎；将来一旦对账变 PASS 且时间参数就绪，Tier A 会把中文式当 SQL 发给 Oracle。COMPILED 走编译器，缺结构化度量时**编译失败即降级**（`chat_service._resolveTierAFormula` fail-closed），不碰业务库。
- 🔴 **`formula` 改成真 SQL**（§1.6.3 雷 2 + §1.6.4 雷 3）：
  - 原值 `SUM(及时到货行数) / COUNT(采购订单总行数)` 会**原样进 prompt**（B3 的「技术公式」行）⇒ LLM 可能照抄中文伪列名；
  - 改成真 SQL 后：B3 的 `formula_semantics` 从「unknown 蒙混」变成**干净通过**（治理自由文本的 `{is_ontime, po_no}` ⊆ 公式的 `{is_ontime, po_no}`）；
  - 同时 `kpi.formula` 若原本与旧公式逐字相同，改完自然不等 ⇒ `formula_mixing` 门禁自动消失。
  - ⚠️ 因此 §3.2 的自由文本里**只许有 `IS_ONTIME` / `PO_NO` 两个 ASCII 标识符**（不许写表名）——多一个 ASCII 标识符就判 mismatch ⇒ B3 整块不注入。
  - 该字段 `nullable=False`，写回合法；`updateMetric` 的 `{time_scope}` 一致性校验只做**单向**（含占位符才要求 `time_column`）⇒ 本公式无占位符 ⇒ 不触发。
- **本步不填** `numeratorMeasure` / `denominatorMeasure` / `grainKeys` / `timeColumn`：
  - 填了但治理侧 `numeratorColumns` 没对齐 ⇒ `mismatch` ⇒ 注入被阻断；
  - 不填 ⇒ 层 2 判 `unknown`（**不阻断注入**）⇒ `degraded=true` ⇒ 该 KPI 不会进 Tier A 直通，稳定落在 Tier B（LLM + 契约）。
- `status` 保持原值不动（不触发发布闸门；`updateMetric` 只在 `status` 在 payload 里时才过闸门与流转矩阵）。

### 3.4 复核（Step 1 后立刻看一次）

```bash
curl -sS -G 'http://127.0.0.1:8000/api/v1/admin/kpi/diagnose' \
  -H "Authorization: Bearer $TOKEN" --data-urlencode "question=$Q1"
```

**必须满足**（chat Tier B 面，§1.6.1）：
- `verdict` **不是** `mismatch`（应为 `unknown` —— 治理 token 仍歧义 ⇒ 放行）；
- `executionSource` = `COMPILED`；
- `tier` = **`B`**（`reason` = `explicit_time_scope`）—— ⚠️ 旧版本文档写「仍是 LEGACY」是**错的**：Tier A 闸 **2026-11-14 12:52 就已开**，`LEGACY` 只出现在开关打开之前的历史事件里。

**再查 B3 那套面有没有挡路**（`diagnose` 不覆盖它，§1.6.1）：

```bash
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -A -F"|" -t' <<'SQL'
SELECT created_at, after_json->>'metric_id', after_json->>'kpi_findings'
  FROM audit_log
 WHERE entity_type = 'kpi_metric_reconcile'
   AND created_at > now() - interval '1 day'
 ORDER BY created_at DESC LIMIT 5;
SQL
```
判读：
- **无行** ⇒ 编译期三道对账全部无 finding ⇒ B3 会注入 ✅；
- **有行且 findings 里出现 `formula_semantics` / `grain_coverage` / `formula_mixing` 的 `mismatch`** ⇒ B3 **不注入**该 KPI ⇒ 契约仍然到不了 LLM ⇒ **停下**，按 finding 的 `detail` 定位（最常见是 §1.6.3 的子集不成立 或 §2.4.2 (A) 的 grain 缺口）。

---

## 4. Step 2 — 回填 time_grain / time_column（**实测已回填 ⇒ 跳过**）

> ✅ **2026-11-14 实测：`kpi_catalog.time_grain = month`（KPI_PUR_001）、`ontology_metric.time_grain = month`（metric 1）—— 已回填过。**
> Phase 1 那个「两表 time_grain 计数 = 0」的旧数字与行查询矛盾，**以行查询为准**。
> 本批修复走 Tier B，**不依赖** `time_column`（Tier A 直通才需要）⇒ **本步跳过，不执行回填脚本**。
> 若下批做确定性执行，仍需把 `time_column` 补上 + `formula` 加 `{time_scope}` 占位符。

```bash
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -A -F" | " -t' <<'SQL'
SELECT 'kpi_total', count(*), 'kpi_with_grain', count(*) FILTER (WHERE time_grain IS NOT NULL)
  FROM kpi_catalog;
SELECT 'metric_total', count(*), 'metric_with_grain', count(*) FILTER (WHERE time_grain IS NOT NULL),
       'metric_with_timecol', count(*) FILTER (WHERE time_column IS NOT NULL)
  FROM ontology_metric;
SQL
```

**实测结论：两表已 100% 有 `time_grain`** ⇒ **跳过本步**（幂等回填无事可做）。
上面的全表计数 SQL 可留作体检记录用，**不是本步的前置动作**。
**若仍有 NULL** ⇒ 跑幂等回填脚本（脚本在镜像里，`/app/scripts/kpi_backfill_time_grain.py`）：

```bash
# ① dry-run（只读，不写库）
docker exec qa-backend sh -c 'python -m scripts.kpi_backfill_time_grain --database-url "$DATABASE_URL"'

# ② apply（写正式库；脚本自带 pg_dump 备份；幂等）
docker exec qa-backend sh -c 'python -m scripts.kpi_backfill_time_grain --database-url "$DATABASE_URL" --apply --allow-prod-write'

# ③ 幂等复跑（判据：changes = 0）
docker exec qa-backend sh -c 'python -m scripts.kpi_backfill_time_grain --database-url "$DATABASE_URL" --apply --allow-prod-write'
```

- `--allow-prod-write` 只看**库名**（dev/prod 同名）⇒ 执行前请自行核对 DSN 指向的是生产 `qa_metadata`。
- 脚本输出里的 `[schema-not-migrated]` 表示 0143 的列还没建全 ⇒ **apply 会拒绝**，先解决迁移。
- 回填**是 Tier A 的硬前置**（不是整洁性）：显式时间问句（「9月」）走 Tier A 的前提是
  `metric.formula` 含 `{time_scope}` 占位符 **且** `metric.time_column` 非空（`chat_stream_output.py:487-491`）。
  本次修复走 Tier B，**不依赖**这两项；但下一批做确定性执行时必须齐。

---

## 5. Step 3 — 开闸（三把开关，逐把开、逐把验）

### 5.1 开关写法（`system_config` 列名是 `key` / `value`）

**实测状态（§1.4.1）**：`KPI_METRIC_TIER_A_ENABLED=true`（已开）、`KPI_MATCH_CONTAINMENT_ENABLED=true`（已开）、`KPI_CONTRACT_INJECTION_ENABLED=**false**`（行存在，2026-10-30 手工关的）。
⇒ **本步只翻一把：契约注入**。它的行**存在** ⇒ 用 admin API PUT（写审计痕迹），**不需要 SQL INSERT**：

```bash
curl -sS -X PUT "http://127.0.0.1:8000/api/v1/admin/system-config/KPI_CONTRACT_INJECTION_ENABLED" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data '{"value":"true"}'
```

> ⚠️ **必须先做 §3.2 / §3.3 再翻这把**：契约注入只对「已匹配到 KPI」的问句起作用，而 Q1 现在落在 **Tier C**（`execution_source` 为 NULL ⇒ 不调 LLM）⇒ 先翻开关，Q1 仍是 0-token。

- **行已存在** ⇒ 用 admin API（写审计）：
  ```bash
  curl -sS -X PUT "http://127.0.0.1:8000/api/v1/admin/system-config/KPI_METRIC_TIER_A_ENABLED" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    --data '{"value":"true"}'
  ```
- **行不存在** ⇒ admin API **没有 CREATE 端点**（行由迁移/种子脚本管理，PUT 缺失行直接 404）⇒ 用 SQL 插入：
  ```bash
  docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
  INSERT INTO system_config (key, value, description, updated_time)
  VALUES ('KPI_METRIC_TIER_A_ENABLED', 'true',
          'KPI Tier A 执行器灰度开关（2026-11-14 灰度）', now())
  ON CONFLICT (key) DO UPDATE
    SET value = EXCLUDED.value, description = EXCLUDED.description, updated_time = now();
  SQL
  ```
  插入后可用 API 再 PUT 一次（留下 audit_log 痕迹），值相同无副作用。

- **env 杀闸**：`KPI_CONTRACT_INJECTION_ENABLED` / `KPI_MATCH_CONTAINMENT_ENABLED` / `KPI_METRIC_TIER_A_ENABLED`
  在 `docker/.env` 里的默认是 **true**（只是杀闸，默认不干预）。**若生产 `.env` 里显式写了 `false`，改 `.env` 后必须重启容器才生效** —— 本方案假设它们没被写成 false（§2.2 只能看到行，看不到 env；请在宿主机 `grep -E '^KPI_.*ENABLED' docker/.env` 确认一次，**只确认是否存在，不要打印其它行的内容**）。

### 5.2 开闸顺序与验收

| 序 | 开关 | 生产实测状态 | 动作 |
|---|---|---|---|
| ① | `KPI_METRIC_TIER_A_ENABLED` | **已开 = true**（12:52:58） | **不动**。只核对 env 杀闸不是 false（`grep -E '^KPI_.*ENABLED' docker/.env`，只确认存在性） |
| ② | `KPI_MATCH_CONTAINMENT_ENABLED` | **已开 = true**（12:05:07） | **不动**。但要注意它本身**救不了 Q2**：containment 判「KPI 短语 ⊂ 问句」，而短语只有全名 ⇒ 简称问法不命中 ⇒ 必须靠 §3.2 补简称 |
| ③ | `KPI_CONTRACT_INJECTION_ENABLED` | **关 = false**（行存在，2026-10-30 手工关的） | 🔴 **本批唯一要翻的开关**。行已存在 ⇒ 用 admin API PUT（写审计），**不需要 SQL** |

**每开一把都要能一键关回去**（回滚 = 把该行 value 改回 `false`）。

### 5.3 明确不动的开关

| 开关 | 处置 | 理由 |
|---|---|---|
| `KPI_METRIC_GATE_ENABLED` | **保持关** | 发布闸门：一旦打开，PUBLISHED 的 RATIO 指标缺分子/分母落点会被阻断，可能挡住现存 KPI 的发布/编辑 |
| `KPI_MATCH_VECTOR_ENABLED` | **保持关** | 双闸（env 默认 false + 行）；向量召回是另一批的能力，且向量候选会把弱问法也拉到 KPI，反而放大误命中面 |
| `KPI_SYNC_ENABLED` | 保持现状（默认开） | KPI 写钩子会自动同步向量/图，本次改 KPI 时它已生效，无需干预 |

---

## 6. Step 4 — 验收清单

1. **前端复问 Q1**（全名问句）：应有 token 数 **> 0**、给出按基地分组的完成率数字，口径 = `SUM(IS_ONTIME)/COUNT(PO_NO)`（**先聚合后相除**，不是 `AVG(分子/分母)`）。
2. **前端复问 Q2**（弱问句）：同上，且**不再**只回口径。
3. **diagnose 两个问句**：`injectionEnabled=true`、`injectionBlockEmpty=false`；Q1 的 `matches[0].tier` 应为 **`B`**（不再是 `C`）、`verdict` 仍为 `unknown`（不是 `mismatch`）；Q2 的 `matches[]` 应出现 `KPI_PUR_001`（`matchMethod=containment`）。
4. **运行时事件时间线**（判据最硬的一条 —— Tier 真的换了）：
   ```bash
   docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -A -F"|" -t' <<'SQL'
   SELECT created_at, after_json->>'kpi_code', after_json->>'tier',
          after_json->>'reason', after_json->>'match_method'
     FROM audit_log WHERE entity_type = 'kpi_runtime_event'
    ORDER BY created_at DESC LIMIT 5;
   SQL
   ```
   期望：最新 2 行是 `KPI_PUR_001 | B | explicit_time_scope | containment`（Q1、Q2 各一行）。
   ⚠️ 若仍是 `C | execution_source_missing` ⇒ `executionSource` 没写进去（§3.3 没生效）⇒ 查 PUT 返回值与 `ontology_metric.execution_source`。
5. **治理看板**：`runtime.byTier` 应开始出现 `B`；`C` 的计数**不再新增**（存量 2 条是历史）。
   ⚠️ `reconcile` 段**全 0 不代表失败也不代表通过**（§1.4.4）⇒ 不要用它当判据。
6. **生成的 SQL 正确性**：前端展开本轮 `executed_sqls`，确认
   - 分子是 `SUM(IS_ONTIME)`、分母是 `COUNT(PO_NO)`（不是 `AVG(分子/分母)`）；
   - `GROUP BY` 里有基地列（`PUR_SITE_CODE` 之类），且 `WHERE` 里有 9 月时间窗。
   ⇒ 若 SQL 里出现中文列名或表名 ⇒ 契约里的列名没被召回（§2.4.1 (ii)）⇒ 回到该节处理。
7. **后端日志**：不应再出现 `L1 回退路径未命中 FeatureDefinition … 执行降级为无数据回答`。
8. **数据面零副作用**：本批**没有**跑 Tier A 确定性执行（`execution_source=COMPILED` 但 reconcile 停在 `unknown` ⇒ `tier_a_eligible=false`）⇒ Oracle 业务库只有 LLM 自己生成的 SELECT，无编译执行。
9. 🔴 **两套注入面都要绿**（§1.6.1）：`diagnose` 只覆盖 chat Tier B 面；**B3 面**（注入 NL2SQL plan prompt）另查一次「编译期对账有没有落 mismatch 行」：
   ```bash
   docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -A -F"|" -t' <<'SQL'
   SELECT created_at, after_json->>'metric_id', after_json->>'kpi_findings'
     FROM audit_log WHERE entity_type = 'kpi_metric_reconcile'
      AND created_at > now() - interval '1 day'
    ORDER BY created_at DESC LIMIT 5;
   SQL
   ```
   期望：**无行**。若出现 `mismatch` ⇒ B3 不注入 ⇒ 契约到不了 LLM ⇒ 症状 B 会复现 ⇒ 按 `detail` 定位（常见：自由文本多写了 ASCII 标识符 ⇒ §1.6.3；或 grain 缺口 ⇒ §2.4.2 (A)）。
   ⚠️ **口径更正（2026-11-14 21:3x 实测）**：`RECONCILE_ENTITY_TYPE`（`kpi_metric_audit.py:19`）= `"kpi_metric_reconcile"`，与 B3 编译期（`kpi_contract_reconcile.py:41`）**是同一个 entity_type**；且**两处都只在有 findings 时才落库**。⇒「无行」的真实含义是「**两套面都没产出 findings**」（这是好事），但**无法区分「通过」与「根本没跑」**。⇒ **B3 面的正向证据只能是最终 SQL 本身**（见 §9.3），不要把「无行」当成 B3 生效的证明。
10. **后端日志补一条**：`grep 'KPI 治理契约装配失败' ` 不应命中；`grep 'B3 不注入' ` 不应命中（命中即 §1.6.1 的 B3 面被挡）。

---

## 7. Step 5 — 回滚手册

**本批只做了三处写操作**，回滚也只针对这三处：

| 回滚对象 | 动作 |
|---|---|
| 契约注入闸（本批翻的） | `PUT /api/v1/admin/system-config/KPI_CONTRACT_INJECTION_ENABLED` 写回 `{"value":"false"}`（恢复 2026-10-30 的原状）。关掉后回答仍由 LLM 产出（Tier B），**不会**退回 0-token |
| KPI 治理数据 | 用 §3.1 的备份还原：`PUT /api/v1/kpi-catalog/1` 写回原 `semanticKeywords` / `numerator` / `denominator` / `grain` |
| 本体指标技术形态 | `PUT /api/v1/ontology/metrics/1` 写回 `{"metricType":"MEASURE","aggregationOrder":null,"executionSource":null}` ⚠️ `aggregationOrder` / `executionSource` 传 `null` 表示**显式清空**（`exclude_unset` 下不传 = 不改）⇒ 归零 `execution_source` 会把 Q1 打回 **Tier C**（0-token），这是**预期的回滚语义** |

| 不回滚项 | 理由 |
|---|---|
| `KPI_METRIC_TIER_A_ENABLED`（本来就是 true） | 不是本批开的，回滚它等于改变发版后的既有状态 |
| `KPI_MATCH_CONTAINMENT_ENABLED`（本来就是 true） | 同上 |
| `time_grain` 回填 | 本批**未执行**回填脚本（实测已回填过）⇒ 无需回滚 |

---

## 8. 明确不做的事（本方案边界）

- ❌ 不做 DDL、不建迁移、不加索引。
- ❌ 不动发布闸门（`KPI_METRIC_GATE_ENABLED`）、不动向量匹配（`KPI_MATCH_VECTOR_ENABLED`）。
- ❌ 不把 `KPI_PUR_001` 改 DRAFT（它是 PUBLISHED 的业务 KPI，改了会从匹配缓存里消失，反而更查不到）。
- ❌ 不填 `numeratorColumns` / `denominatorColumns` / `numeratorMeasure` / `denominatorMeasure`（会撞 `mismatch`，把注入彻底堵死）。
- ❌ 不在本批做**确定性执行**（Tier A 真跑 SQL）：那需要完整的本体建模 —— 源类 `source_table`、属性 `source_column`、`ontology_metric_source` 的 BASE/NUMERATOR/DENOMINATOR 角色、JOIN 路径、治理列与技术列逐一对齐。**这一项是下一批的独立立项**（先做源类与属性体检，再补 `ontology_metric_source`，最后补结构化度量）。
- ❌ 不修「legacy L1 命中但执行不出数时直接终结为 0-token 口径答案」这个**行为缺陷**（应降级到 LLM 而不是把「定义」当答案）—— 属代码缺陷，下批改代码（`chat_stream_output.py:691-728` 的 `l1 is not None` 早退分支）。

---

## 附：本次修复后的状态对照

| 能力 | 修前（生产实测） | 修后 |
|---|---|---|
| 弱问法（简称）命中 KPI | ❌ `matches: []` | ✅ containment 命中（前提：§3.2 补了简称） |
| 口径进入 LLM prompt | ❌ 契约块恒空（开关显式 false） | ✅ 契约块含业务定义 + 分子/分母（含真实列名）+ 粒度 + 聚合阶 + **允许/禁止 SQL 形态** |
| 命中后的执行路径 | Q1 → Tier C（`execution_source` NULL）⇒ 0-token 口径答案 | Q1 → Tier B ⇒ LLM 按签字口径出数 |
| 未命中时（Q2 现在） | 直接 LLM，无口径 ⇒ 公式自编 | 先命中 ⇒ 进契约 ⇒ 按签字口径出数 |
| 确定性执行（Tier A） | ❌ | ❌（本批不做，见 §8） |
| token 成本 | 0（假回答） | > 0（真实计算） |

### 遗留（下一批独立立项，不在本批）

| 项 | 现状 | 需要什么 |
|---|---|---|
| 确定性执行（Tier A 真跑 SQL） | `ontology_metric_source` 0 行、`denominator_measure` 空、`time_column` 空、`formula` 无 `{time_scope}` | 补源类映射（BASE/NUMERATOR/DENOMINATOR 角色）→ 补 `ontology_property.source_column` → 补结构化度量 → 补 `time_column` + 占位符 → 治理列与技术列逐一对齐（对账变 PASS） |
| 治理 token 歧义 | `SUM(IS_ONTIME)` / `COUNT(PO_NO)` 命中多个候选 ⇒ verdict 永远 `unknown` ⇒ 永远 `degraded` | 消歧后才能对账变 PASS |
| 🔴 **把源表名钉进契约**（本批**故意不做**，§1.6.2/§1.6.3） | `ontology_metric_source` 0 行 ⇒ 契约块**不含表名**；本批靠「`IS_ONTIME` 全本体唯一」隐式钉表 | 要显式写表名，必须**三件一起补**（否则 `formula_semantics` 或 `_checkRole` 会把契约整块挡掉）：<br>① 治理结构化列改成裸列名（`IS_ONTIME` / `PO_NO`，不带 `SUM(...)`）；<br>② `ontology_metric.numerator_measure` + `denominator_measure` 补齐（否则 `_checkRole` 因 denominator 为 NULL 判 MISMATCH）；<br>③ `ontology_metric_source` 插一行指向 `DWS_PO_COMPLETE_RATE`（无 API，只能 SQL ⇒ 须单独评审）。<br>三件齐了以后，契约里可以写 `源表与角色：DWS_PO_COMPLETE_RATE（BASE）`，且 verdict 才能从 `unknown` 变 `pass`。**顺序不可颠倒：先补 ①②，最后才动 ③。** |
| 存量向量欠账 | Milvus 2 / PUBLISHED 13（Neo4j 16 齐）—— 本批编辑 `KPI_PUR_001` 时写钩子补了它自己，仍欠 **11** 条 | 显式跑一次补同步（写钩子只在 create/update/delete 触发，存量不会自动补） |
| legacy L1 行为缺陷 | 「命中但执行不出数」时返回 0-token 口径答案并终结整轮，应降级到 LLM | 改代码（`chat_stream_output.py` 的 `l1 is not None` 早退分支） |

---

## 9. 执行记录（2026-11-14 21:1x～21:3x CST，生产实测）

### 9.1 三处写操作全部落库

| 写操作 | 落库证据（生产实测回读） |
|---|---|
| ① KPI 治理数据 PUT | `numerator = SUM(IS_ONTIME)：按期、按量完成交付的采购订单行数（及时到货行数）`<br>`denominator = COUNT(PO_NO)：当期应交付的采购订单行数（采购订单总行数）`<br>`grain = 物料+供应商+基地+月份`<br>`semantic_keywords = {采购订单完成率, 订单完成率, 采购完成率, 及时到货率}`<br>`metric_id = 1` |
| ② 本体指标技术形态 PUT | `formula = SUM(IS_ONTIME) / COUNT(PO_NO)`<br>`execution_source = COMPILED` / `metric_type = RATIO` / `aggregation_order = WEIGHTED_RATIO`<br>`dimension_defaults = null`（⇒ `grainCoverageGaps` 返回 `[]` ⇒ B3 的 `grain_coverage` 闸不判 mismatch）<br>`grain_keys = ["YMONTH, ITEM_CODE, SUPPLIER_CODE, PUR_SITE_CODE, BUYER_CODE"]`（含基地列 `PUR_SITE_CODE`） |
| ③ 契约注入开关 PUT | `diagnose` 回执 `injectionEnabled=true` / `injectionBlockEmpty=false` / `injected=true` |

### 9.2 门禁状态（`diagnose` 回执）

| 字段 | 值 | 判定 |
|---|---|---|
| `matches[0].kpiCode` / `matchMethod` / `confidence` | `KPI_PUR_001` / `containment` / `0.9` | ✅ 简称问法命中（写 ① 的关键词生效） |
| `metricType` / `executionSource` | `RATIO` / `COMPILED` | ✅ 已拉出 Tier C |
| `verdict` / `degraded` / `tierAEligible` | `unknown` / `true` / `false` | ✅ `unknown` 放行，**未撞 mismatch** |
| `tier` | `B` | ✅ 走 LLM |
| `reasons` | `["governance_token_ambiguous","governance_grain_absent"]` | ✅ 结构化列保持歧义 = **保护伞还在**（证明结构化列一个字没动） |
| `ambiguous` | `false` | ✅ 匹配不歧义 |
| `syncHealth.errors` | `[]` | ✅ |
| `kpiVectorCount` | 1 → 2 | 写钩子补了被编辑的那条（向量匹配开关关着，无影响） |

### 9.3 运行时事件时间线（判据最硬的一条）

```
2026-11-14 13:21:06 | KPI_PUR_001 | B | explicit_time_scope | containment   ← 新
2026-11-14 13:20:37 | KPI_PUR_001 | B | explicit_time_scope | containment   ← 新
2026-11-14 13:20:07 | KPI_PUR_001 | B | explicit_time_scope | containment   ← 新
2026-11-14 13:11:20 | KPI_PUR_001 | C | execution_source_missing | containment ← 写操作前
2026-11-14 12:55:57 | KPI_PUR_001 | C | execution_source_missing | containment ← 写操作前
2026-11-14 12:54:03 | KPI_PUR_001 | C | execution_source_missing | containment ← 写操作前
```

⇒ **C → B 翻转完成。** `explicit_time_scope` 是 **B 的正常原因**（问句含显式时间「9月」而 `formula` 无 `{time_scope}` 占位符、`time_column` 为空 ⇒ 待时间参数化 ⇒ 降级 B 而非 A），**不是错误**。

### 9.4 生成 SQL 的形状验收（症状 B 的判据）

| 要点 | 产出 | 判定 |
|---|---|---|
| 表 | `DWS_PO_COMPLETE_RATE`（唯一一张） | ✅ |
| 分子 | `SUM(IS_ONTIME) AS ONTIME_PO_LINES` | ✅ |
| 分母 | `COUNT(PO_NO) AS TOTAL_PO_LINES` | ✅ |
| 比率形态 | `SUM(IS_ONTIME) / NULLIF(COUNT(PO_NO), 0) AS PO_COMPLETE_RATE` | ✅ **先聚合后相除**，不是 `AVG(分子/分母)` |
| 分组 | `GROUP BY PUR_SITE_CODE, PUR_SITE_NAME` | ✅ 按基地（「每个基地分析一下」没被丢掉） |
| 时间窗 | `WHERE YMONTH = '2026-09'` | ✅ |
| 物料过滤 | `ITEM_TYPE_CODE IN ('A02','A03','A04','A05')` | ✅ **业务方配置的默认口径规则**（问句未提，是预置规则 ⇒ **非幻觉**） |

⇒ **症状 B（公式与指标定义不符）已修复。**

### 9.5 治理看板（`governance-summary`，`generatedAt=2026-11-14T13:30:54Z`）

| 段 | 值 | 判定 |
|---|---|---|
| `metrics.byExecutionSource` | `NONE:8, COMPILED:1, ESCAPE_HATCH:8` | ✅ metric 1 是**唯一** COMPILED（修前是 NONE） |
| `runtime.byTier` | `B:3, C:3, LEGACY:8` | ✅ 3 条 B = 本轮三次提问；C 3 条 = 写操作前；LEGACY 8 条 = 12:52 开闸前。**窗口 7 天** ⇒ 老事件仍挂着，**不代表现在还在出 C** |
| `runtime.degradedReasons` | `tier_a_disabled:8`（老）、`execution_source_missing:3`（老）、`explicit_time_scope:3`（新 B） | ✅ 新事件原因正常 |
| `reconcile` | `mismatchCount:0, passCount:0, unknownCount:1` | ✅ mismatch 0；unknown 1 = 本 KPI。`passCount:0` 是「干净通过从不落行」的老口径，**不可当判据** |
| `gates` | `publishGateEnabled:false, tierAEnabled:true, syncEnabled:true` | ✅ Tier A 闸开着；发布闸按约定保持关 |
| `syncHealth` | `publishedKpiCount:13, milvusKpiCount:2, neo4jKpiCount:16` | ⚠ **Milvus 仍欠 11 条向量**（已知存量欠账；写钩子只在 create/update/delete 触发 ⇒ 存量不会自动补；向量匹配开关关着 ⇒ 与本 bug 无关） |

### 9.6 本批结论

| 症状 | 状态 | 判据 |
|---|---|---|
| A：Tokens 0 / 只回口径 | ✅ **已修复** | 事件时间线 `C → B`；`execution_source=COMPILED`；`tier=B` |
| B：公式与指标定义不符 | ✅ **已修复** | SQL = `SUM(IS_ONTIME)/NULLIF(COUNT(PO_NO),0)`，按基地分组，9 月窗口 |

**仍待补的取证**（不影响上面两条结论）：前端 `executed_sqls` **原文**（本节验的是 planner 计划卡形状）、回答正文的**每基地数值**、**Tokens / 成本**的最终数值。

**本批未做（边界不变）**：Tier A 确定性执行（仍 `tierAEligible=false`）、发布闸门、向量匹配开关、Milvus 存量补同步、legacy L1 行为缺陷。

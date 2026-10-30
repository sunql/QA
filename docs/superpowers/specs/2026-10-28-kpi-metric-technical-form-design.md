# KPI 指标技术形态治理 → NL2SQL 动态消费 — 设计稿

> **状态**：🟡 OPEN（设计已批准，待实施）
> **日期**：2026-10-28
> **基线**：`main` @ `3ecd780c`（alembic head = `0135`）
> **相关分支**：`feat/kpi-l1-metric-id`（`fd84d092`，main 领先 60 / 分支领先 4，基点 `cc4b8986`）
> **相关提案**：`Harness/changes/2026-10-27-kpi-governance-into-nl2sql-proposal.md`（分支 `docs/kpi-governance-nl2sql-proposal`）
> **相关变更记录**：`Harness/changes/kpi-l1-metric-id/summary.md`（draft，未部署）
> **上游背景**：`Harness/changes/feat-complex-metric-pipeline/summary.md`（辛普森悖论口径基线）

---

## 1. 目的

把「业务/治理侧产生的口径信息」与「本体侧的指标技术形态」真正接上，让 NL2SQL 生成 SQL 时按**实际配置**动态取到该指标的口径，从而提高指标准确性。

**当前问题的实质**（非"数据没填"，而是"链路没有消费者"）：

1. `ontology_metric`（名义上的技术形态载体）在 NL2SQL 链路里**零消费者** —— 即使补齐 12 行，也**不会影响任何一条生成的 SQL**。
2. 口径分散在 **6 个家**，只有 1 个是活的，且活的那个与本体无关联。
3. **L1 KPI 确定性直通路径已经存在、已写好、0 LLM、0 幻觉**，却因一处等值匹配而 **100% 失效**，全部流量落回 NL2SQL。

## 2. 范围与非范围

### 2.1 In scope

- **数据模型**：`ontology_metric` 结构化扩列；新增 `ontology_metric_source`（多源类 M:N）；`kpi_catalog` 结构化扩列 + 语义关键词可配置
- **对账模型**：三层对账（技术内部 / 治理⇄技术 / 可执行⇄结构化）+ 三档判定（pass / mismatch / unknown）+ 审计落库
- **运行时**：
  - Tier A 确定性直通复活（`kpi.metric_id → ontology_metric` 绑定链）
  - Tier B NL2SQL 契约注入（`kpi_contract` 块）
  - 发布闸门（KPI 与 metric 双向）
- **缓存动态性**：修复 `KpiMatchCache` 的改名残留 / 冷缓存 no-op / raw SQL 绕过三个洞
- **`kpi_catalog.data_source` 精确化**：按 `+` 分割后精确 join `ontology_class.source_table`，替代现状的名字模糊匹配（见 §12-3）
- **前端**：`KpiCatalogPage` 与 `MetricTab` 的字段增量
- **测试**：单测 / 集成 / E2E 三层，含防假绿红线
- **上线**：迁移、灰度开关、回滚手册

### 2.2 Out of scope（明确不做）

- **本体补齐**：8 张 KPI 源表不在本体（`DWD_INCOMING_INSPECTION` / `DWD_NCR` / `DWD_PAYMENT` / `DWD_SUPPLIER_CERTIFICATION` / `DWD_PURCHASE_CONTRACT` / `DWD_PURCHASE_REQUISITION`），且本体 **DWS 层 = 0 / ADS 层 = 0**（54 类 = ODS 21 + DWD 30 + 空 3）。这是**独立数据工作项**，不进本 spec。本 spec 只保证「已有的口径不丢、不漂移」。
- **结构化 → SQL 编译器**（`execution_source=COMPILED`）：Phase 3 可选，见 §13。
- **`feature_definition` 表结构重构**：本 spec 只给它加 `metric_id` FK（可选），不改其既有语义。
- **requires_auth 假绿家族 12 处**：另立批次。
- **`MetricPromotionService` / `compileObject` 等既有死代码清理**：另立批次（本 spec 明确**不使用**它们作为对账挂点，见 §6.4）。

---

## 3. 现状冻结（as of 2026-10-28 `main` @ `3ecd780c`）

### 3.1 三个口径载体对比

| 维度 | `kpi_catalog` | `feature_definition` | `ontology_metric` |
|---|---|---|---|
| dev 行数 | **12** | 6 | **0** |
| 技术形态 | `formula` 中文自由文本 | **`calculation_logic` 完整可执行 Oracle SQL** | `formula` 自由文本 |
| 描述 | `business_definition` | `feature_definition` | ❌ 无 |
| 粒度 | `grain` 中文（供应商+工厂+月） | `entity_type` 单值 | `dimension_defaults` JSON（**零消费者**） |
| 分子/分母 | ✅ 有（中文） | ❌ 无 | ❌ 无 |
| 数据源绑定 | `datasource_id` FK（dev **12/12 全 NULL**） | `datasource_id` FK（6/6 全绑 =1） | `target_class_id` FK（**单值**） |
| 生命周期 | `status` DRAFT/PUBLISHED | `status` + `is_enabled` + `version` | ❌ 无 |
| 窗口/刷新 | ❌ | ✅ | ❌ |
| 预计算值 | ❌ | ✅ `feature_value`（1/6 有值，且 stale @2026-09-19） | ❌ |
| 语义关键词 | `semantic_keywords`（dev **12/12 全空**）+ `match_threshold` | ❌ | ❌ |
| 与本体挂钩 | `data_source` 血缘字符串（22 表引用 / 13 命中 = 59%） | SQL 内表名（3/9 命中） | FK（0 行） |
| **进 NL2SQL** | ❌ | ❌（只有散文描述经 `<feature_catalog>` 进 prompt） | ❌（**代码层零消费者**） |

> 结论：三者各有对方没有的东西，**没有一个是完整的**。`kpi_catalog` 有治理语义缺技术形态；`feature_definition` 有技术形态缺治理语义与本体挂钩；`ontology_metric` 是 4 字段空壳。

### 3.2 `ontology_metric` 在 NL2SQL 里零消费者（逐点核实）

| 检查点 | 结果 |
|---|---|
| `app/services/nl2sql_service.py:34-35` | 只 import `OntologyClass, OntologyJoin` —— **不 import `OntologyMetric`** |
| `app/services/nl2sql_prompts.py` | 全文 **0 个** "etric" |
| `app/services/nl2sql_schema.py:251` `buildSchemaText(classes, ...)` | 只渲染类与属性，**无 metric 分支** |
| `app/services/chat_recall.py` | 语义召回 `typeFilter="class"` —— **metric 不召回** |
| `app/domain/query_plan.py` | 注释明写 **"formula 没有确定性渲染器"** |
| `app/services/ontology_service.py:876-946` `createMetric` | 唯一生产创建路径（`POST /ontology/metrics`）：PG → audit → id_mapping → Neo4j → Milvus。**这条链是活的** |

⇒ **分支 B3（契约注入）是全系统唯一能把指标技术形态送进 plan prompt 的机制。**

### 3.3 L1 KPI 确定性直通路径 —— 存在但 100% 失效

```
chat_service.py:435      preamble = await self._runL1AndL4Preamble(dto, session, user)
  └─ chat_stream_output.py:207  _runL1AndL4Preamble
     └─ :222                match = await self._kpiMatcher.match(dto.question)
        └─ kpi_semantic_match_service.py:137  match()
           ├─ :212 _matchExactAlias   （问句含 KPI code / alias ⇒ confidence=1.0）
           └─ :226 _matchByKeywords   （Jaccard，需 semantic_keywords 非空 + 阈值 0.75）
     └─ chat_service.py:1456  _buildL1Response(match, session)
        ├─ :1480  _resolveKpiCatalog(code)        查 kpi_catalog
        ├─ :1520  _executeCalculationLogic(kpi, code, session, match=match)
        │   ├─ :1499  _resolveCalculationFeature  ★ kpi.formula == feature.feature_definition
        │   │                                       （散文描述列 vs 技术公式列 ⇒ 恒不命中）
        │   ├─ _assert_read_only(feat.calculation_logic)
        │   ├─ adapter.execute_read_only(...)     ← 确定性执行，**0 LLM**
        │   └─ scheduleMetricResultEvidence(...)  落 METRIC_RESULT 证据
        └─ 返回 ChatResponse(routing_layer=L1, token_cost_usd=0.0)
     任何异常 → logger.warning → 返回 None → 继续 NL2SQL 流水线
```

**断点唯一且致命**：`_resolveCalculationFeature` 用 `FeatureDefinition.feature_definition == kpi.formula` **等值匹配**。
`feature_definition` 是**散文描述**列，`kpi.formula` 是**技术公式**列 —— 两个语义不同的列做等值比较。

**dev 库实测**：`ontology_metric` 0 行、`kpi_catalog.metric_id` 非空 0 条、12 条 KPI 的 `formula` 与 6 条 feature 的 `feature_definition` 等值命中 **0 条** ⇒ **L1 直通 100% 失效，全部落回 NL2SQL**。

> 这就是"根据实际配置动态获取结果"该落的地方 —— 路径已存在，只需修绑定。

### 3.4 语义匹配与缓存现状

| 项 | 实况 |
|---|---|
| `kpi_catalog.semantic_keywords` | **12/12 全空**（dev） |
| `match_threshold` | 12/12 = 默认 0.75 |
| ⇒ Jaccard 匹配 | **恒不命中**（无关键词）⇒ 只有 exact-alias 快车道可用 |
| `KpiCatalogUpdate` | **无** `semantic_keywords` / `match_threshold` ⇒ 创建后不可改 |
| `KpiCatalogRead` | **无** `semantic_keywords` / `match_threshold` ⇒ 前端看不到 |
| `KpiCatalogPage.tsx` | 表单**无** `semanticKeywords` 字段 ⇒ 业务方无法维护 |

**`KpiMatchCache` 三个洞**（`app/services/kpi_match_cache.py`）：

| 洞 | 位置 | 现象 |
|---|---|---|
| **C1 改名残留** | `:149` `old = self._by_code.pop(kpi_code, None)` | `kpi_code` 改名时 pop 不到 ⇒ 旧 code + 旧关键词**永久残留** ⇒ 陈旧命中 |
| **C2 冷缓存 no-op** | `:166-167` `if not self._loaded: return` | 冷启动期写入的 KPI 永不进索引 |
| **C3 raw SQL 绕过** | `:201-212` ORM listener **只 log**；实际失效靠 service 层主动调 `onKpiChanged` | `backend/scripts/seed_kpi_semantic_index.py` 用**裸 SQL UPDATE** 写关键词 ⇒ **写完运行时行为不变** |

> C3 就是 dev 库 12 条 `semantic_keywords` 全空的成因链。

### 3.5 口径形状多样性（决定数据模型）

12 条 KPI 实测为 **5 种形状**：

| 型 | 例子 | 形状 |
|---|---|---|
| A 加总型 | `KPI_COST_SAVING` | `SUM((STANDARD_PRICE - ACTUAL_PRICE) * QTY)` |
| B 平均型 | `KPI_PURCHASE_CYCLE_TIME` | `AVG(GOODS_RECEIPT_DATE - PR_CREATED_DATE)` |
| C 带过滤平均型 | `KPI_SUPPLIER_DELAY_DAYS` | `AVG(ACTUAL_RECEIPT_DATE - PROMISED_DATE) WHERE 延误 > 0` |
| D 比率型（条件计数比） | `KPI_SUPPLIER_OTD` | `count(A<=P) / count(P在周期内)` |
| E 比率型（差值比） | `KPI_PURCHASE_PRICE_VARIANCE` | `(实际-标准) / 标准` |

⇒ 分子/分母的值**不是"列名"**，而是「(聚合函数, 表达式, 过滤谓词)」三元组 ⇒ 必须**两层模型**（measure 层 / metric 层）。
⇒ 且 **9/12 条 KPI 跨 2–3 张表**（3 条单表：`KPI_PAYMENT_ON_TIME` / `KPI_SUPPLIER_DEFECT_RATE` / `KPI_SUPPLIER_FPY`）⇒ `ontology_metric.target_class_id` 的**单值 FK 表达不了其中 9 条真实口径** ⇒ 必须 M:N。

### 3.6 准确性的两个正交维度

| 维度 | 问题 | 现状 |
|---|---|---|
| **结构正确性** | SQL 的数学/形态对不对 | ✅ 已有三级守卫：L1 静态形态 / L2 歧义示警 / L3 数据不变量（`nl2sql_semantic_guard.py`） |
| **口径一致性** | 是不是按治理定义的分子/分母/粒度算的 | ❌ **完全缺失** |

现有守卫全是"通用数学规则"，与"这个指标的口径是什么"无关；今天的指标准确性只靠两条**通用** prompt 提示撑着：`_AGGREGATE_SCHEMA_HINT_TEXT`（`nl2sql_prompts.py:144`）+ `_LAYER_PRIORITY_HINT`（`nl2sql_prompts.py:157`，注入于 `:411`）。
⇒ **Tier B 契约注入的真正价值 = 把通用提示升级为按指标定制的口径契约。**

### 3.7 分支现状

`feat/kpi-l1-metric-id` = 4 commits（`66e2542f` B1+B5 / `1b29e139` B2 / `aaba0a43` B3 / `fd84d092` B4），25 文件 +2391/−22，基点 `cc4b8986`。

| 件 | 与本设计关系 |
|---|---|
| B1 | `_resolveCalculationFeature` 两级解析 —— **正是 §7.2 的三级绑定前两级** ⇒ 可复用 |
| B2 | 挂在 `compileObject` 上，而 `compileObject` **零生产调用方**（唯一 `KnowledgeCompilerService()` 实例在 `knowledge_reconcile_worker.py:60`，只调 `reconcile()`）⇒ **对账永不触发**，而提案把 B2→B3 列为硬依赖 |
| B3 | 机制正确（唯一 metric→prompt 通道），但比率型文案**写反**（见 §12） |
| B4 | 分母契约守卫 + `extractSqlIdentifiers` ⇒ **复用**（本设计三层对账的列集合比对用它） |
| B5 | `/metric` 自动 DRAFT KPI ⇒ 与本设计无冲突 |

---

## 4. 核心设计决策

### 4.1 权威关系（方案 A）

```
kpi_catalog              治理 SSOT    业务语义 + 口径声明（中文）+ 粒度 + 业务负责人
   │ metric_id FK（已存在）
   ▼
ontology_metric          技术形态 SSOT 结构化 measure + 聚合阶 + 粒度键 + 过滤谓词 + 逃生舱 SQL
   │ 1:N
   ▼
ontology_metric_source   多源类 M:N   role = NUMERATOR | DENOMINATOR | FILTER | BASE

feature_definition      执行封装（降级）  calculation_logic + 窗口 + 刷新 + feature_value
   ▲ 可从 calculation_logic 反向编译回 ontology_metric 的结构化列
```

**决策理由**：

- `ontology_metric` 是**唯一被"本体"承认的技术形态容器**（`MetricTab` 有 CRUD UI、Neo4j/Milvus 同步链已通、`POST /ontology/metrics` 是活的），扩成真容器成本最低、语义最正。
- `feature_definition` 语义是"按 entity 的**特征**度量"（单值 `entity_type`、6 行），`kpi_catalog` 语义是"按口径的**指标**"（复合粒度、12 行）⇒ **不同级，不合并**。它降级为"执行封装"，其 `calculation_logic` 可反向编译为结构化列 —— **一次编译，多处复用，不浪费已有资产**。
- 不新建中枢表：`ontology_metric` 已经是这个角色，只是没实现；再开一张等于承认上一张选错。

### 4.2 双轨制

| 轨 | 载体 | 定位 |
|---|---|---|
| **机器轨（真相）** | `numerator_measure` / `denominator_measure` / `aggregation_order` / `grain_keys` / `filter_expression` | 结构化、可机器对账、可编译 |
| **人轨（逃生舱）** | `formula` | 人类可读的等价 SQL 片段；表达机器轨装不下的形状（窗口函数、同环比、分位数、跨表 pivot…） |

**裁决**：**结构化是真相**。`formula` 与结构化不等 ⇒ 标 `degraded`，Tier A 不可用（但 Tier B 仍可注入）。

### 4.3 分级直通

| Tier | 条件 | 行为 | LLM | 准确 |
|---|---|---|---|---|
| **A** | metric 已发布 + 有可执行口径 + 对账 pass + 问句无范围约束 | 确定性执行，直接返回结果 | **0** | 口径本身 |
| **B** | metric 有结构化口径，对账 pass\|unknown | NL2SQL 生成 + 注入契约块 | 1 | 受约束 |
| **C** | metric 缺失 / 对账 mismatch | NL2SQL 无契约（现状） | 1 | 现状 |

---

## 5. 数据模型

### 5.1 `ontology_metric` 扩展

**轨道 2（新增，结构化 = 真相）**

| 列 | 类型 | 说明 |
|---|---|---|
| `metric_type` | varchar(20) NN default `'MEASURE'` | `MEASURE` / `RATIO` / `DIFFERENCE` / `DERIVED` |
| `numerator_measure` | JSON NULL | 基础度量定义（形状见下） |
| `denominator_measure` | JSON NULL | 同上；`metric_type=RATIO` 时必填 |
| `aggregation_order` | varchar(24) NULL | `WEIGHTED_RATIO` / `AVG_OF_RATIOS` / `NONE` —— **辛普森开关** |
| `grain_keys` | JSON NULL | `["supplier_code","plant_code","year_month"]` |
| `filter_expression` | Text NULL | 全局过滤谓词 |

**measure 的 JSON 形状**（`numerator_measure` / `denominator_measure` 共用）：

```json
{
  "agg": "SUM | COUNT | AVG | COUNT_DISTINCT",
  "expr": "ACTUAL_RECEIPT_DATE - PROMISED_DATE",
  "filter": "IQCRESULT = 'FAIL'",
  "class_id": 12,
  "class_alias": "DWD_INCOMING_INSPECTION"
}
```

**轨道 1（保留）**：`formula`（Text NN，逃生舱）、`agg_function`（varchar(20)，仅 `metric_type=MEASURE` 有意义）。

**管理性（新增，闸门落点）**

| 列 | 类型 | 说明 |
|---|---|---|
| `status` | varchar(20) NN default `'DRAFT'` | `DRAFT` / `PUBLISHED` —— 技术形态自身成熟度 |
| `owner` | varchar(100) NULL | **技术口径负责人**（≠ `kpi_catalog.owner` 的业务负责人） |
| `execution_source` | varchar(16) NULL | `COMPILED` / `ESCAPE_HATCH` / `NONE` —— **本指标是否有可直接执行的只读 SQL**（语义见 §7.2） |

> **`execution_source` 的取值语义**（唯一判定来源，不做自动嗅探）：
> - `ESCAPE_HATCH` ⇒ 可执行 SQL 就是 `formula` 原文（人工登记，Phase 2 使用）
> - `COMPILED` ⇒ 可执行 SQL 由结构化列编译而来（Phase 3）
> - `NONE` ⇒ 不可执行 ⇒ **不允许 Tier A 直通**（可发布，但只能走 Tier B）

> **不加** `version` / `revision_count` —— 变更审计走 `audit_log`（YAGNI）。

### 5.2 `ontology_metric_source`（新表，多源类 M:N）

```
id         PK
metric_id  FK → ontology_metric.id  ON DELETE CASCADE
class_id   FK → ontology_class.id    ON DELETE RESTRICT
role       varchar(20) NOT NULL   -- NUMERATOR | DENOMINATOR | FILTER | BASE
UNIQUE(metric_id, class_id, role)
INDEX(class_id)
```

- `ontology_metric.target_class_id` **保留**（不破坏现有 API/UI/同步链），语义 = `role=BASE` 的那个类；新表为权威。
- `role` 必填 —— "这个类在口径里扮演什么角色"正是当前完全缺失的信息。

### 5.3 `kpi_catalog` 扩展

| 列 | 类型 | 说明 |
|---|---|---|
| `grain_keys` | JSON NULL | 结构化粒度键（业务人员可填，词汇有限） |
| `numerator_columns` | JSON NULL | 列名数组，如 `["ACTUAL_RECEIPT_DATE","PROMISED_DATE"]` |
| `denominator_columns` | JSON NULL | 列名数组 |

**为什么必须加**：B2 的对账现在只能出 `mismatch|unknown`，根因是**中文 numerator 抽不出列名**。让治理侧能填"我用的是哪几列"，机器比对就从"中文 vs 结构"降级为"列集合比对"（B4 已有 `extractSqlIdentifiers`）。

> 中文 `numerator` / `denominator` / `grain` **保留原样**，降级为"人话说明" —— 双轨制的另一轨。

### 5.4 语义关键词配置化

| 改动 | 理由 |
|---|---|
| `KpiCatalogUpdate` 加 `semantic_keywords` / `match_threshold` | 现在创建后不可改 |
| `KpiCatalogRead` 加 `semantic_keywords` / `match_threshold` | 现在前端看不到 |
| `KpiCatalogPage.tsx` 加 `semanticKeywords` 控件 | 业务方无法维护 ⇒ 第一道门在运营层不存在 |

### 5.5 迁移

```
0136_metric_structured    ontology_metric 扩列 + ontology_metric_source 新表 + kpi_catalog 扩列
0137_metric_source_unique  UNIQUE(metric_id, class_id, role) + INDEX(class_id)
```

- 全部新列 nullable / 有默认值 ⇒ downgrade 直接删列，无数据损失。
- `ontology_metric_source` 是新表 ⇒ 删表不影响存量。
- `kpi_catalog.metric_id` 的 FK **已存在**，不动。

---

## 6. 对账模型

### 6.1 三层对账与各自裁决基准

| # | 对账对 | 基准（谁是真相） | mismatch 行为 |
|---|---|---|---|
| 1 | **技术内部**：结构化 measure ⇄ `formula` SQL 的列引用 | **结构化**（双轨裁决，§4.2） | 标 `degraded`，Tier A 不可用 |
| 2 | **治理 ⇄ 技术**：治理 `numerator_columns` / `denominator_columns` / `grain_keys` ⇄ 技术 measure 列集合 | **治理**（治理是签字方） | **阻断**：不注入 + 落 audit + 告警 |
| 3 | **可执行 ⇄ 结构化**：`execution_source` 指向的 SQL 列引用 ⇄ 结构化 measure 列 | **结构化** | Tier A 不可用，降 Tier B |

> **一句话口径**：**口径的内容以治理签字为准；口径的技术表达以结构化为准。** 这两句不矛盾 —— 前者管"治理 vs 技术"，后者管"技术内部"。

### 6.2 三档判定

| 判定 | 条件 | 行为 |
|---|---|---|
| `pass` | 治理列集合 ⊆ 技术列集合 ∧ `grain_keys` 一致 ∧（`metric_type=RATIO` ⇒ `aggregation_order` 非空） | 正常注入；**允许 Tier A** |
| `unknown` | 治理侧结构化字段为空（未填）—— **存量主流** | 注入但标 `unknown` + 落 audit |
| `mismatch` | 技术侧有列而治理侧无（技术跑偏）/ `grain_keys` 不一致 / `RATIO` 但 `aggregation_order` 空 | **不注入该 KPI** + 落 audit + 告警 |

> 列集合比对复用 B4 的 `extractSqlIdentifiers`（`nl2sql_semantic_guard.py`，分支新增，B2/B4 共用的低层纯函数）—— "声明列 ⊆ 分母列集合，宁漏不误报"。

### 6.3 审计

- 落 `audit_log`，`entity_type = 'kpi_metric_reconcile'`
- 仅在产生 finding（`mismatch` / `unknown`）时落 —— `pass` 不落，避免噪声
- 沿用 `kpi_contract_reconcile.py` 的自 commit 同三库对账 `reconcile()` 模式

### 6.4 对账挂点（修正 B2）

**B2 挂在 `compileObject` 上是错的** —— 该方法零生产调用方，对账永不触发。

本设计改为：**在注入时实时对账**（Tier B 契约构建时）+ **在发布时对账**（闸门）。
⇒ B2 的"编译期对账 + audit"由这两处取代，**不再依赖任何编译入口**。

---

## 7. 运行时链路

### 7.1 总体路由

```
问句
 ├─ KPI 语义匹配命中？（读配置里的 semantic_keywords）
 │   ├─ 否 ─────────────────────────────────▶ Tier C：NL2SQL 无契约（现状）
 │   └─ 是 → 取 kpi.metric_id → ontology_metric(+sources)
 │        ├─ metric.status=PUBLISHED
 │        │  ∧ execution_source ≠ NONE
 │        │  ∧ 对账 = pass
 │        │  ∧ 问句无"范围/分组/筛选"约束      ⇒ **Tier A**
 │        │     └─ 确定性执行 → 直接返回结果（0 LLM / 0 幻觉）
 │        ├─ metric 有结构化口径，对账 pass|unknown ⇒ **Tier B**
 │        │     └─ NL2SQL + 注入 kpi_contract 契约块
 │        └─ metric 缺失 / 对账 mismatch            ⇒ **Tier C**
 │              └─ NL2SQL 无契约 + 落 audit 记 drift
```

### 7.2 Tier A：确定性直通复活

**三级绑定解析**：

```
1) kpi.metric_id → ontology_metric                              ← P2 主路径
      ├─ execution_source = ESCAPE_HATCH → 可执行 SQL = ontology_metric.formula
      └─ execution_source = COMPILED     → 可执行 SQL = 编译产物（Phase 3）
2) kpi.metric_id → ontology_metric → feature_definition.metric_id（新增 FK）  ← P3
      └─ 可执行 SQL = feature.calculation_logic
3) 兜底（现状保留，向后兼容，Phase 2 即生效）
      └─ FeatureDefinition.feature_definition == kpi.formula 等值
```

> **分期对应**：Phase 2 只激活 **1（ESCAPE_HATCH）+ 3**；第 2 级依赖 `feature_definition.metric_id` FK（Phase 3 才引入）。
> 但**第 1 级一旦命中就短路**，第 3 级仅在第 1/2 级都落空时才走。

**新增一列**：`ontology_metric.execution_source varchar(16) NULL` —— 语义见 §5.1。

> **为什么不用"自动嗅探 formula 是不是 SQL"**：自动判定不可测、不可复现，且会把人写的说明误当可执行。**显式枚举**才可治理。

**问句约束探测（Tier A 的适用边界）**：Tier A 执行的是**固定口径的 SQL**，不接收问句里的时间范围/筛选条件。
⇒ **默认策略**：问句命中 KPI 但含下列任一类约束时，**不走 Tier A**，降级 Tier B。

| 类别 | 触发词示例 | 为什么必须降级 |
|---|---|---|
| 时间范围 | 近N月 / 今年 / 上月 / 最近N天 / YTD | 固定口径 SQL 的时间窗写死在指标定义里 |
| 分组维度 | 按X / 各X / 分组 | 固定口径 SQL 的 `GROUP BY` 写死 |
| 排名/筛选 | TopN / 前N / 排名 / 高于 / 低于 | 固定口径 SQL 无 HAVING/ORDER BY 语义 |
| 实体过滤 | 某供应商 / 某物料 / 某工厂（专有名词） | 固定口径 SQL 无外部谓词注入位 |

判定复用已有启发式范式（`_shouldInjectAggregateSchemaHint`，`nl2sql_prompts.py:169`）；**判定为"含约束"时一律降 Tier B，不做部分执行**。

> 该探测是**保守方向**的：宁可多降 Tier B（少一次直通机会），不可误判后给出忽略约束的答案。

### 7.3 Tier B：契约注入

**注入点**：`nl2sql_prompts.py:256` `_buildPlanSystemPrompt` —— 已有 3 个同款 block（`featureCatalogText` / `dictionaryText` / `wikiRulesBlock`），照抄模式。

```
buildKpiContractBlock(session, question):
  1. matchAll(question, limit=3)                 ← 动态，按配置
  2. 每个 KPI：kpi.metric_id → ontology_metric + ontology_metric_source
  3. 对账门禁（§6）→ mismatch 的那个 KPI 不注入，其余照常
  4. 渲染：治理五件套 + 聚合阶 + 粒度键 + 源类(role) + 允许的 SQL 形态
  5. wrapUntrusted(UntrustedLabel.KPI_CONTRACT, text)   ← 第 12 个 label（现 11 个）
```

**比率型文案（必须写死"允许哪种写法"，不让 LLM 选）**：

| `aggregation_order` | 允许且仅允许 | 禁止 |
|---|---|---|
| `WEIGHTED_RATIO` | `SUM(分子)/SUM(分母)` | `AVG(分子/分母)` |
| `AVG_OF_RATIOS` | `AVG(分子/分母)` | `SUM(分子)/SUM(分母)` |
| `NULL` ∧ `metric_type=RATIO` | **不注入该 KPI**（闸门已拦） | — |

> `UntrustedLabel`（`app/security/prompt_sanitizer.py:51`）当前 11 个 label，`wrapUntrusted` 在 `:118`。新增 `KPI_CONTRACT` 后 **12 个**，红队 `ALL_LABELS` 断言须同步。

### 7.4 错误处理与降级（**永不静默**）

| 故障 | 行为 |
|---|---|
| KPI 匹配不可用 / cache 未 warmUp | → Tier C，warning |
| `metric_id` 悬空 | → Tier C，warning（FK 兜底 + 防御性代码） |
| 对账 `mismatch` | → 该 KPI 不注入；**其余命中 KPI 照常注入**；落 audit |
| 对账 `unknown` | → 注入 + 标 `unknown` + 落 audit |
| Tier A 执行失败（SQL 错 / 只读校验不过 / 超时） | → **降级 Tier B**（不是报错）；warning + 落 audit |
| Tier A 命中但问句含范围约束 | → 直接走 Tier B（设计内降级，非故障） |

> Tier A 失败必须降级而非报错：沿用 L1 现有模式（`_buildL1Response` 的 try/except → None → 继续流水线）。

### 7.5 发布闸门

```
KPI: DRAFT → PUBLISHED  ⟸ 必须同时满足：
  ① metric_id IS NOT NULL
  ② metric.status = PUBLISHED
  ③ metric.execution_source ≠ NONE          （要能算）
  ④ 对账 = pass（unknown 不允许发布）
  ⑤ datasource_id IS NOT NULL                （现在 dev 12/12 全 NULL！）
不满足 → 409 + 明确缺哪一项
```

`ontology_metric.status: DRAFT → PUBLISHED` 走同一套闸门（结构化齐全 + 对账 pass）。

> 闸门受独立开关 `KPI_METRIC_GATE_ENABLED` 控制（默认 **false**）—— 数据没补齐前不开。

---

## 8. 缓存动态性修复

| 洞 | 修法 |
|---|---|
| **C1 改名残留** | `onKpiChanged` 改为**按 `id` 失效**（id 跨改名稳定）；或 service 层同时传 `old_code` |
| **C2 冷缓存 no-op** | `refreshOne` 在 `!_loaded` 时置 dirty 标记 → 下次查询 lazy rebuild（与 `getAll` 抛 `RuntimeError` 的语义协调） |
| **C3 raw SQL 绕过** | `seed_kpi_semantic_index.py` 改走 `KpiCatalogService`；或脚本末尾显式 `onKpiChanged(None)` + `warmUp` |

---

## 9. 前端

### 9.1 `KpiCatalogPage.tsx`（现 462 行）

| 位置 | 改动 |
|---|---|
| 表单 | `metricId`（本体指标选择器）、`semanticKeywords`（Tags 数组）、`grainKeys` / `numeratorColumns` / `denominatorColumns`（结构化列） |
| 列表 | 新增列「技术形态挂靠」= metric 名 + 对账 verdict（pass/mismatch/unknown/未挂） |

### 9.2 `MetricTab.tsx`（现 336 行）

| 新增字段 | 控件 |
|---|---|
| `metricType` | Select：MEASURE / RATIO / DIFFERENCE / DERIVED |
| `status` | Select：DRAFT / PUBLISHED |
| `owner` | Input |
| `aggregationOrder` | Select：WEIGHTED_RATIO / AVG_OF_RATIOS / NONE |
| `grainKeys` | Tags |
| `numeratorMeasure` / `denominatorMeasure` | 结构化编辑器 |
| `executionSource` | Select：COMPILED / ESCAPE_HATCH / NONE |

> `dimensionDefaults` 现在是 string↔JSON 手搓（`MetricTab.tsx:113-118`）⇒ 结构化编辑器复用同一模式，不引入新组件库。

---

## 10. 测试策略

TDD RED → GREEN → IMPROVE，覆盖率 ≥ 80%，真实 PostgreSQL 集成测试（禁 sqlite 内存库）。

### 10.1 单测（纯函数，不碰库）

| 文件 | 覆盖 |
|---|---|
| `test_metric_contract_reconcile.py` | 三层对账：pass / mismatch / unknown；grain 不一致；RATIO 缺 `aggregation_order`；列集合 ⊆ 判定；`execution_source` 判定 |
| `test_kpi_contract_block.py` | 契约块渲染：**比率型文案正反断言**（`WEIGHTED_RATIO ⇒ 只允许 SUM/SUM`）；mismatch 被排除；unknown 标记；`wrapUntrusted` label 正确 |
| `test_kpi_match_cache_dynamic.py` | C1 / C2 / C3 各一测 |
| `test_metric_publish_gate.py` | 闸门 5 条件逐条 + 缺项报明确信息 |
| `test_metric_sql_binding.py` | Tier A 三级绑定解析 |

### 10.2 集成测试（真 PG 5434 + neo4j-test）

| 文件 | 覆盖 |
|---|---|
| `test_kpi_metric_lifecycle.py` | 建 metric → 挂 KPI → 对账 → 发布 → 回滚全链路 |
| `test_l1_tier_a_direct.py` | **端到端：问句 → Tier A 直通 → 断言结果正确且 LLM 调用次数 == 0** |
| `test_kpi_contract_injection.py` | NL2SQL 端到端：契约文本**实际出现在 plan prompt 里** |

### 10.3 🔴 防假绿红线

| 红线 | 理由 |
|---|---|
| Tier A 测试必须断言 **LLM 调用次数 == 0** | 否则可能悄悄退化成 NL2SQL 仍绿 |
| 注入测试必须断言 **prompt 里真的出现契约文本**，不能只断言"函数被调用" | mock 内断言假绿 |
| 对账/闸门测试必须断言 **"是谁拒的"**，不能只断言状态码 | 状态码相同的假绿家族（12 处） |
| **unit 与 integration 必须分两个 pytest session** | 同 session 撞 seed ⇒ 假红 |
| 涉及 Milvus / Neo4j 的测试用隔离实例 | 裸 pytest 会删线上 |
| 每条 SQL 断言要带**反向**（"删掉守卫应当变红"） | 守卫自检 |

---

## 11. 上线与灰度

### 11.1 开关

| 开关 | 默认 | 作用 |
|---|---|---|
| `KPI_CONTRACT_INJECTION_ENABLED` | true（分支已有） | Tier B 契约注入 |
| `KPI_METRIC_TIER_A_ENABLED` | **false** | Tier A 直通（默认关，先只读对比） |
| `KPI_METRIC_GATE_ENABLED` | **false** | 发布闸门（先观察再阻断） |

### 11.2 数据迁移（需业务方提供，代码不能编）

| 项 | 现状 | 谁提供 |
|---|---|---|
| 12 条 KPI 的 `semantic_keywords` | 全空 | 业务方 + 种子脚本（修 C3 后才生效） |
| 12 条 KPI 的 `datasource_id` | **12/12 NULL** | 业务方（直接影响闸门条件⑤） |
| 12 条 `ontology_metric` 行 | **0 行** | 由治理口径 + `docs/data-knowledge/采购域.md` 血缘反向合成 |

### 11.3 回滚

三个开关各自独立退回；migration 可 downgrade；Tier A 关开关即退回 NL2SQL。

---

## 12. 必须修正的既有缺陷（合并前/随本设计一起修）

| # | 缺陷 | 位置 | 修法 |
|---|---|---|---|
| 1 | **B2 挂在无调用方的 `compileObject` 上** ⇒ 对账永不触发 | `knowledge_compiler_service.py` `_compileSqlMetadata` | 改为注入时 + 发布时实时对账（§6.4） |
| 2 | **B3 比率型文案写反** | 分支 `nl2sql_prompts.py:311`：「禁止先聚合后相除以外的写法」字面 = **强制先聚合后相除** = 辛普森悖论错误写法 | 按 §7.3 重写；提案原文 `Harness/changes/2026-10-27-...proposal.md:156` 同一处也错，一并修 |
| 3 | **`kpi_catalog.data_source` 是表血缘但被当"数据源"用** | `kpi_datasource_suggestion_service.py:98-105` 只做名字模糊匹配 | `source_table` 是**裸表名**（实测 `DWD_SUPPLIER_PRICE_LIST_HEADER`）⇒ 按 `+` 分割即可**精确 join** `ontology_class.source_table`（22 引用 / 13 命中 = 59%） |
| 4 | **C1/C2/C3 缓存三洞** | `kpi_match_cache.py` | §8 |

---

## 13. 分期与交付物

| 期 | 内容 | 依赖 | 交付物 |
|---|---|---|---|
| **P1** | 数据模型（§5）+ 三层对账（§6）+ 发布闸门（§7.5）+ Tier B 契约注入（§7.3）+ 语义关键词配置化（§5.4）+ 缓存三洞（§8）+ 前端（§9）+ `data_source` 精确 join（§12-3） | — | migration 0136/0137 + 后端 + 前端 + 测试 + 变更记录 |
| **P1-数据** | 数据回填与合成工具：`ontology_metric` 12 行（治理口径 + `采购域.md` 血缘反向合成）+ `semantic_keywords` 回填 + `datasource_id` 回填。**产出需业务方审核后才落库** | P1 代码 | 回填脚本（默认预览，`--apply` 落库）+ 审核清单 |
| **P2** | Tier A 直通复活（`ESCAPE_HATCH`）+ 问句约束探测（§7.2） | P1 + P1-数据 | + 端到端直通测试 + 灰度手册 |
| **P3** | 结构化 → SQL 编译器（`COMPILED`）+ `feature_definition.metric_id` 投影 | P2 | 可选，视 P2 效果决定 |

> **实施计划的范围 = P1 + P1-数据**。P2 / P3 各自独立立项。
> **分支处置（已决）**：**选项 3** —— 只取 B4 的守卫 + `extractSqlIdentifiers` 到新分支；B1/B3 在新分支按本设计重写；不背 60-commit rebase。
> ⇒ 本 spec 落在 `docs/kpi-metric-technical-form-spec`（= `main` @ `3ecd780c` + 本 spec 一个 commit）。
> ⇒ **实施分支从 `docs/kpi-metric-technical-form-spec` 切出**（而非直接切 main，以便携带本 spec）。

---

## 14. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| 业务方不愿填结构化列 | 对账长期 `unknown` | 前端做"从中文口径生成候选列"（LLM 辅助 + 人工确认）；灰度期先手填 |
| P1 结束时 `ontology_metric` 仍 0 行 | 闸门会挡住所有 KPI 发布 | 闸门开关独立，数据补齐前不开 |
| 本体缺 8 张表 + DWS 层 = 0 | 召回不到，问不准 | **独立工作项**，不进本 spec；本 spec 只保证"已有的口径不丢" |
| prod 本体 69 类 vs dev 54 类 | dev 结论可能不适用 prod | 上线前在 prod 跑**只读**对账报告 |
| Tier A 忽略问句约束 | 答非所问 | 范围/分组关键词探测 → 降级 Tier B；灰度期做两路答案一致性对比 |
| measure 的 `class_id` 是 JSON 内嵌引用（DB 无法加 FK） | 悬空 class_id | 应用层校验 `class_id` 存在且 `role` 匹配，否则对账判 `unknown` |

---

## 15. 未决项（需后续决策，不阻塞本设计）

1. **`feature_definition` 是否保留为独立载体** —— P3 若做编译器，需决定它与 `ontology_metric` 的最终关系（投影 or 并存）。
2. **结构化 → SQL 编译器是否值得做** —— 取决于 P2 的 Tier A 覆盖率与人工维护成本。
3. **本体补齐的排期** —— 独立工作项，8 张表 + DWS 层。
4. **`requires_auth` 假绿家族 12 处** —— 另立批次。
5. **既有死代码清理**（`compileObject` / `MetricPromotionService` / `/kpi-catalog/search` 无前端调用 / `dimension_defaults` 零消费者）—— 另立批次。

---

## 16. 附：关键证据索引

| 结论 | 证据 |
|---|---|
| `ontology_metric` NL2SQL 零消费者 | `nl2sql_service.py:34-35`；`nl2sql_prompts.py` 0 个 "etric"；`nl2sql_schema.py:251`；`chat_recall.py` `typeFilter="class"` |
| L1 直通存在但失效 | `chat_stream_output.py:207/222` → `chat_service.py:1456/1499/1520`；dev 库 formula↔feature_definition 等值命中 0 |
| 语义关键词全空 | dev 库 `SELECT semantic_keywords FROM kpi_catalog` 12/12 NULL |
| C3 raw SQL 绕过 | `backend/scripts/seed_kpi_semantic_index.py` 裸 SQL UPDATE；`kpi_match_cache.py:201-212` listener 只 log |
| 9/12 条 KPI 跨 2–3 表 | dev 库 `kpi_catalog.data_source` 展开 22 表引用 / 13 命中 `ontology_class.source_table` |
| 本体 DWS/ADS = 0 | dev 库 `ontology_class` 分层统计：ODS 21 + DWD 30 + 空 3 |
| `target_class_id` 单值不够 | 9/12 KPI `data_source` 含 `+`（多表；3 条单表：PAYMENT_ON_TIME / DEFECT_RATE / FPY） |
| B2 挂死路径 | `knowledge_compiler_service.py` `compileObject` 零生产调用方；唯一实例 `knowledge_reconcile_worker.py:60` 只调 `reconcile()` |
| B3 文案倒置 | 分支 `nl2sql_prompts.py:311` |
| 提案同处亦错 | `Harness/changes/2026-10-27-kpi-governance-into-nl2sql-proposal.md:156` |
| alembic head | `alembic heads` → `0135` |

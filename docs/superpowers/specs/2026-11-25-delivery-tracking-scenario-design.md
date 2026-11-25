# 到货跟踪场景（P5）设计文档

日期：2026-11-25
分支：`feat/ontology-evolution`
状态：**已评审通过（2026-11-25）** —— 进入实现计划

## 1. 需求与目标

在采购 Copilot 新增「到货跟踪」场景（与 P4 的「待到货 / 逾期未到」「供应商风险速览」并列），
满足四条业务诉求：

1. 按某个时间段的订单，看是否都已经到货；
2. 看某一个或多个供应商的到货情况；
3. 企业默认的月度周期是每月 24 日到次月 24 日；
4. 能查看具体的订单信息，以及已到货 / 未到货的物料明细。

**核心交付物**：**一个本体类 + 一个专用只读工具 + 一个结果渲染器 + 一个场景配置行**。

## 2. 关键结论：需要编码，不能纯模板配置

P4 已把「场景外壳」（卡片墙 / 槽位表单 / 提问模板 / 冲突让位 / 结果渲染器分发）全部做成
DB 配置。新场景的**外壳**因此可以零代码，但**取数层必须写代码**，四处硬缺口：

| 缺口 | 说明 |
|---|---|
| ① 现有 `pending_delivery_query` 谓词是「只看欠交」 | 三段 SQL 全部带欠交谓词，只返回未到货行，**没有分母**，取不到「已到货」物料 |
| ② 24 日周期不是静态日期 | `copilot_scenario.chips` 是 DB 静态 JSON，写死的 `dateFrom/dateTo` 不随日期滚动；`monthsBack` 是自然月，非 24 日锚点 |
| ③ 无对应结果渲染器 | `RESULT_RENDERER_WHITELIST` 中 `generic_table` 是空壳（前端未实现），到货跟踪需要「概览 + 双 Tab 明细」两层结构 |
| ④ **本体缺行级到货量的新鲜类** | 见第 3 节 —— 需先建本体类，工具才能通过本体属性名取数 |

## 3. 取数层：走本体，不硬编码表名列名

### 3.0 核心原则（2026-11-25 用户明确要求）

> 「前面查询的这些对象的信息在本体里面都有记录，最好通过本体类理解对接，
> 不要每次都是去探查实际数据，变成固定的『硬编码』了。」
> 「每个智能体都确定唯一的一个数据源就好了……从技术层面也没有不同数据源用同一个
> 智能体的能力，除非都是配置化的，不需要编码。」
> 「**数据源使用 THBI 这个数据源，一切数据对象都从这个源头获取，按照这个硬规则来。**」

#### 3.0.1 硬规则：一切对象出自 THBI

| 项 | 值 |
|---|---|
| 数据源 | `data_source.id=1`（THBI Oracle @ 192.168.205.70:1521/X3V71ORA，用户名 `THBI`） |
| schema | `THBI`（Oracle owner） |
| 取数范围 | **只允许 `THBI` schema 下的对象**；不跨 schema、不连其它数据源 |

**推论（贯穿全篇）**：

1. **既有 9 条绑定全部合规** —— 实测 9 个类均为 `datasource_id=1` + `schema_name=THBI`，
   与硬规则一致，无需迁移；
2. **待到货场景必须迁到 THBI** —— 它现在查 `ZJTH.PORDERQ` / `ZJTH.ITMMASTER` /
   `ZJTH.FACILITY` / `ZJTH.BPSUPPLIER`（`ZJTH` 是同实例下的**另一个 owner**），
   违反硬规则 ⇒ 迁移是**必须项，不是可选项**（3.8）；
3. **`datasourceId` 切换槽位取消** —— 场景不再提供 admin 切数据源的下拉
   （切到非 THBI 源会违反硬规则）。既有 `pending_delivery` 场景的
   `datasourceId` 隐藏槽位一并移除；
4. **工具不再按「默认活跃源」兜底** —— 改为一律按绑定的 `datasource_id` 取数，
   绑定缺失即 fail loud（不再有「静默落默认源」这条路径）。

#### 3.0.2 本体对接：复用既有绑定设施

**工具 SQL 只写本体属性名，物理表名与物理列名由本体绑定解析**。
仓内**已有完整的绑定设施**（M1-1.1 建的），本方案直接复用，不新造：

| 设施 | 承载什么 | 关键列 |
|---|---|---|
| `procurement_source_binding` | 本体类 → **数据源 + schema + 物理表** | `class_id` / `datasource_id` / `schema_name` / `source_table` / `binding_role` / `is_candidate` / `confirmed_by` / `valid_to` |
| `procurement_property_binding` | 本体属性 → **物理列** | `property_id` / `source_binding_id` / `source_column` / `key_ordinal` / `transform_expression` |
| `semantic_compiler_bindings.compileSourceBindings()` | 把绑定编译成「类+数据源 → 表/列」 | — |
| `procurement_binding_validation` | 绑定合法性校验（列名白名单 / 表达式安全 / 候选隔离） | — |

**这就是用户要的机制的正式实现**：

- 「一个 Agent 一个数据源」⇒ `procurement_source_binding.datasource_id` 就是锚点；
- 「跨源时对象名、属性名会变」⇒ 换**绑定行**即可，工具 SQL 不动；
- 「除非都是配置化的，不需要编码」⇒ 跨源差异全部收敛在绑定表里。

**既有绑定现状**（dev 库，9 个类，全部 `confirmed_by=m1-b-confirm`、`is_candidate=false`、
`datasource_id=1` + `schema_name=THBI`）；属性绑定覆盖 645 行，**候选 0 条**
（`is_candidate` 隔离机制生效，候选不得进入读路径）。

**前车之鉴（不可忽视）**：`backend/scripts/procurement_class_source_table_realdata.py`
记录了 2026-11-18 的真实事故 —— 迁移把采购域类的 `source_table` 绑到 DWD 词表，
而本体 SSOT 绑的是 ERP 词表 ⇒ NL2SQL 指向**不存在的表**，且 `seed_ontology.seed()`
在干净库上**直接崩**（`source_table` 查重与 `class_name` 唯一约束冲突）。
⇒ 本方案必须建类 + 建绑定同步，且跑真实数据门禁。

### 3.1 本体现状盘点（2026-11-25 只读探针 + PG 实查）

采购订单相关的本体类与绑定：

| 本体类 | BO | 绑定 | 属性 | 数据行数 | 数据截止 | 关键属性是否齐 |
|---|---|---|---|---|---|---|
| 本体类 | BO | 绑定 | 属性 | 数据行数 | 数据截止 | 说明 |
|---|---|---|---|---|---|---|
| `DWD_PURCHASE_ORDER_DTL` | `PO` 采购订单 | ✅ 已绑 `THBI.DWD_PURCHASE_ORDER_DTL` | 30（已确认） | 1,036,069 | **2026-11-24** | ✅ **订单行表**（`PO_NO`+`PO_LINE_NO`+`PO_QTY`+`ITEM_CODE`+`SUPPLIER_CODE`）；**无已收量列** |
| `DWD_GOODS_RECEIPT_DTL` | `GR` 收货 | ✅ 已绑 `THBI.DWD_GOODS_RECEIPT_DTL` | 41（已确认） | 4,037,142 | **2026-11-25** | ✅ **收货表**（`PO_NO`+`PO_LINE_NO`+`RCV_QTY_STU`+`RCV_DATE`+`SUPPLIER_CODE`+`ITEM_CODE`） |
| `DWD_PURCHASE_ORDER_LINE` | `POL` 采购订单行 | ✅ 已绑 `THBI.DWD_PURCHASE_ORDER_LINE` | 29（已确认） | 927,648 | **2026-08-08** | ⚠️ 属性最全（含 `RECEIVED_QTY`/`LAST_RECEIPT_DATE`）但**数据旧**，本期 0 单 |
| `DIM_FACILITY` | — | ❌ **未绑**（类已存在，117 属性含 `FCY_0`/`FCYNAM_0`） | 117 | 41 | — | ⚠️ 只缺源绑定 |
| `ODS_ERP_PORDERQ` | — | ❌ 未建类 | — | 1,036,089 | 2026-11-24 | （备选，见 3.2 备选方案） |

**关键修正（2026-11-25 用户指出）**：

- **`DWD_PURCHASE_ORDER_DTL` 是订单行表**（对应 `PORDERQ` 订单行），不是订单头 ——
  它有 `PO_NO` + `PO_LINE_NO` + `PO_QTY` + `ITEM_CODE`，行粒度。修正前文误判。
- **`DIM_FACILITY` 类已存在**（117 属性，`FCY_0`/`FCYNAM_0` 都在，本体已登记），
  **只缺源绑定** —— 不需要新建类。

**核心结论：到货率可以完全用本体已绑定的类算出来，不必新建类**

本体里「已收量」的正规载体是 **`DWD_GOODS_RECEIPT_DTL`（收货，BO=GR）**，
它有 `RCV_QTY_STU`（收货库存单位数量）且**数据新鲜（至 2026-11-25）**。
与订单行 `DWD_PURCHASE_ORDER_DTL` 按 `PO_NO + PO_LINE_NO` 关联即可：

```
订单行（DWD_PURCHASE_ORDER_DTL）         收货（DWD_GOODS_RECEIPT_DTL）
  PO_NO, PO_LINE_NO, PO_QTY      ⟕        PO_NO, PO_LINE_NO, RCV_QTY_STU, RCV_DATE
```

**实测验证（本期窗口 2026-10-24 ~ 2026-11-24）**：

| 指标 | 本体口径（订单行 ⋈ 收货） | ERP 直算（`ODS_ERP_PORDERQ`） | 差异 |
|---|---|---|---|
| 订单数 | 4,844 | 4,844 | 一致 |
| 全到货 / 部分 / 未到 | 3,986 / 448 / 410 | 3,984 / 448 / 412 | **差 2 单** |
| 订单到货率 | **82.29%** | 82.25% | 0.04pp |
| 量级率 | **40.51%** | 40.51% | **完全一致** |

**2 单差异归因**（探针实证）：`H12611POH0022`（订购 1）、`T72611POH0014X`（订购 500）
两单的 ERP 行上 `RCPQTYSTU_0 = 0`，但**收货单已存在且数量足够** ⇒
**收货单已开、ERP 行累计收货字段未回写**（两个口径的时点差）。
本体口径（以收货单为准）**更贴近业务事实**。

**关联命中率**：
- 收货行 `DWD_GOODS_RECEIPT_LINE`（旧批次表，至 2026-08-08）与订单行本期窗口命中 **0%**
  ⇒ **该表不可用于本期**，必须用 `DWD_GOODS_RECEIPT_DTL`（至 2026-11-25）；
- `DWD_GOODS_RECEIPT_DTL` 整体有 `PO_NO` 的占 72.03%（余 28% 为无订单来源的收货，
  如退货/调整类），**不影响**按订单关联的部分。

**决策**：**不新建 `ODS_ERP_PORDERQ` 类**，改为：

1. **补 `DIM_FACILITY` 的源绑定**（类已存在，只缺绑定）；
2. **给 `DWD_GOODS_RECEIPT_DTL` 补齐到货跟踪所需属性的绑定核验**
   （`PO_NO`/`PO_LINE_NO`/`RCV_QTY_STU`/`RCV_DATE`/`SUPPLIER_CODE`/`ITEM_CODE`
   是否都在已绑定的 41 个属性内，见 3.2 核验项）；
3. 到货跟踪与待到货**都用本体已绑定的类**，不碰未建类的 `ODS_ERP_*`。

**备选方案（已排除，存档备查）**：若日后需要走 ERP 行级累计量口径
（`ODS_ERP_PORDERQ` 的 `QTYSTU_0`/`RCPQTYSTU_0`），则新建该类 + 绑定，
属性映射为：`PO_NO`→`POHNUM_0`、`PO_LINE_NO`→`POPLIN_0`、`ORDER_DATE`→`ORDDAT_0`、
`SUPPLIER_CODE`→`BPSNUM_0`、`MATERIAL_CODE`→`ITMREF_0`、`ORDER_QTY`→`QTYSTU_0`、
`RECEIVED_QTY`→`RCPQTYSTU_0`、`PROMISED_RECEIPT_DATE`→`EXTRCPDAT_0`、
`LAST_RECEIPT_DATE`→`LASRCPDAT_0`、`RETURNED_QTY`→`RETQTYSTU_0`。
该口径量级率 40.51%（与主路径**完全一致**）、订单率 82.25%（vs 主路径 82.29%），
差异仅 2 单边界（0.04pp）。

### 3.2 绑定补充（复用既有设施，不新建类）

**需要补的绑定**：

| 本体类 | 缺什么 | 补什么 |
|---|---|---|
| `DIM_FACILITY` | 源绑定（`procurement_source_binding` 0 条） | `datasource_id=1` / `schema_name=THBI` / `source_table=DIM_FACILITY` / `binding_role=BASE` / `is_candidate=false` / `confirmed_by=<确认人>` |
| `DWD_GOODS_RECEIPT_DTL` | ✅ **无需补** | 41 个属性**全部已绑定已确认**（`confirmed_by=m1-b-confirm`），含到货跟踪所需全部：`PO_NO` / `PO_LINE_NO` / `RCV_QTY_STU` / `RCV_QTY_PUU` / `RCV_DATE` / `SUPPLIER_CODE` / `SUPPLIER_NAME` / `ITEM_CODE` / `ITEM_DESC` / `STK_UOM` / `PUR_SITE_CODE` |
| `DWD_PURCHASE_ORDER_DTL` | ✅ **无需补** | 30 个属性全部已绑定已确认，含 `PO_NO` / `PO_LINE_NO` / `PO_QTY` / `ITEM_CODE` / `SUPPLIER_CODE` / `SUPPLIER_NAME` / `ORDER_DATE` / `EXP_RCV_DATE` / `PUR_SITE_CODE` / `REF_RCV_NO` |

⇒ **本次唯一需要新增的本体资产是 `DIM_FACILITY` 的一条源绑定**（类与属性都已存在）。

**建绑定约束（防重演 2026-11-18 事故）**：

1. 源绑定与属性绑定**同步登记**（不留半截状态）；
2. 绑定行必须 `is_candidate=false` 且有 `confirmed_by`/`confirmed_time`
   （DB CHECK 已钉：`候选 ↔ 无确认人`）；
3. 绑定后跑**真实数据门禁**（对照
   `procurement_class_source_table_realdata.py` 的形态）：干净库跑迁移链 ⇒
   类存在、绑定指向真实存在的表、10 个属性的物理列全部存在、`seed_ontology.seed()` 不撞唯一约束；
4. 探针已验证 `THBI.ODS_ERP_PORDERQ` 150 列，10 个目标列全部存在且非空率 100%
   （`ORDER_DATE` 空 0、`QTYSTU_0`/`RCPQTYSTU_0`/`BPSNUM_0`/`ITMREF_0` 全非空）。

### 3.2.1 Business Object：给既有 `POL` 换绑（选项 A）—— **本轮推迟**

`uq_business_object_header_class` 唯一索引 ⇒ 一个本体类只能作一个 BO 的头类。
选项 A 的内容：把既有 `POL`（采购订单行）BO 的头类从
`DWD_PURCHASE_ORDER_LINE`（旧批次，2026-08-08）**换绑**到
`DWD_PURCHASE_ORDER_DTL`（新鲜全量，至 2026-11-24）。

| 项 | 值 |
|---|---|
| `code` | `POL`（**沿用既有码**，不新增 `POL_ERP`） |
| `name` | 采购订单行（不变） |
| `header_class_id` | `DWD_PURCHASE_ORDER_DTL`（原 `DWD_PURCHASE_ORDER_LINE`） |
| `primary_domain_id` | `PROCUREMENT`（不变） |
| `graph_label` | `DWD_PURCHASE_ORDER_DTL` |

**语义依据**：业务对象「采购订单行」应当只有**一个当前口径**；
`DWD_PURCHASE_ORDER_LINE` 是同一业务对象的**旧抽取批次**。

**⚠ 本轮执行范围（用户 2026-11-25 决定：暂不动 `purchase_history_query`，推迟删类）**：

⇒ **本轮不执行 BO 换绑，也不删 `DWD_PURCHASE_ORDER_LINE`**；
BO 换绑与删类作为**独立后续立项**（本规格只记录依赖链，供后续执行）。

**依赖链（已查清，后续换绑/删类时按此顺序，不可颠倒）**：

`DWD_PURCHASE_ORDER_LINE` 实测有 4 处活引用：

| # | 引用方 | 迁移动作 |
|---|---|---|
| 1 | `business_object.POL.header_class_id` | 换绑到 `DWD_PURCHASE_ORDER_DTL`（**先做**，否则 `POL` 指向墓碑 ⇒ 口径漂移） |
| 2 | **`purchase_history_query` 工具**（`agent_tools_procurement.py:231` `_HISTORY_TABLE`） | 改指新类；**金额口径有坑，见 3.8** |
| 3 | **6 个采购 KPI 的口径锚点**（`ontology_metric_source` 的 NUMERATOR / DENOMINATOR / FILTER / BASE 四角色） | 指标源行换指新类 + 重算 `feature_value` |
| 4 | 图谱 JOIN 边（`apply_dim_imaterial_join_edges.py` / `apply_supplier_join_edges.py` / `sync_business_graph_from_thbi.py`） | 边的 `sourceClass` 改指新类 |

**涉及的 6 个 KPI**（`feature_definition` 中 `status=ACTIVE`）：
`SUPPLIER_OTD_3M`、`SUPPLIER_DEFECT_RATE_3M`、`SUPPLIER_PRICE_VARIANCE_3M`、
`SUPPLIER_PO_COMPLETION_RATE`、`SUPPLIER_RISK_SCORE`、`MATERIAL_SHORTAGE_RISK`。

⚠️ **其中 `SUPPLIER_RISK_SCORE` 与 `SUPPLIER_OTD_3M` 是上一轮「供应商风险速览」在用的**
⇒ 换绑/删类会**直接影响已上线场景**，必须连带重跑该场景的实测与回归。

**后续换绑时的口径影响（必须显式说明）**：KPI 换绑后取数表从 927,648 行（旧批次）
换成 1,036,069 行（新鲜批次），**6 个 KPI 的历史值口径随之变化** ⇒
`feature_value` 需重算，且重算前后数值差异要**显式比对**（不静默漂移）。

**本轮的口径割裂（如实记录，不掩盖）**：

| 数据用途 | 本轮取数表 | 数据截止 |
|---|---|---|
| 待到货 / 到货跟踪场景 | `DWD_PURCHASE_ORDER_DTL`（新鲜） | 2026-11-24 |
| 6 个采购 KPI（含风险速览） | `DWD_PURCHASE_ORDER_LINE`（旧批次） | 2026-08-08 |
| `purchase_history_query` | `DWD_PURCHASE_ORDER_LINE`（旧批次） | 2026-08-08 |

⇒ **场景数据与 KPI 数据暂时不同源**（两者本就是不同口径：订单行 vs KPI 聚合）。
这是本轮「推迟删类」的直接后果，属已知欠账，须在界面上避免把两者并列比较。


### 3.3 工具如何用本体取数

工具 SQL 模板**只写属性名**，运行时经**绑定编译**解析为物理表列：

```
SELECT
  <PO_NO>, <PO_LINE_NO>, <SUPPLIER_CODE>, <MATERIAL_CODE>,
  <PO_QTY>, <RCV_QTY_STU>, <EXP_RCV_DATE>, <RCV_DATE>
FROM <源表>   -- 由源绑定解析（datasource_id + schema_name + source_table）
WHERE <ORDER_DATE> >= DATE :from AND <ORDER_DATE> < DATE :to
```

解析实现**复用既有设施**（不新造）：

| 环节 | 用什么 |
|---|---|
| 类 → 数据源/物理表 | `semantic_compiler_bindings.compileSourceBindings()`（读 `procurement_source_binding`） |
| 属性 → 物理列 | 同上（读 `procurement_property_binding`） |
| 只取生效绑定 | `valid_to IS NULL`（软版本化：生效 = `valid_to IS NULL`） |
| 候选隔离 | `isPublishableBinding()` —— `is_candidate=true` 的绑定**不得进读路径** |
| 标识符安全 | `procurement_binding_validation.validateColumnName()` 等（列名白名单 / 表达式安全） |

**关键纪律**：绑定是**可治理资产**，解析必须**在工具执行时从 DB 读**，
不能进代码常量、不能缓存成常量 ⇒ 本体改了绑定，工具行为随之变（这正是目的）。

**解析失败处理（fail-closed）**：

| 情形 | 行为 |
|---|---|
| 类不存在 / 已墓碑（`valid_to` 非空） | `NotFoundError`（提示先建类） |
| 该类在该数据源**无生效绑定** | `ValidationError`（提示「本体类未绑定数据源」） |
| 绑定仍是**候选**（`is_candidate=true`） | `ValidationError`（候选不得进读路径） |
| 必需属性缺失或未绑定物理列 | `ValidationError`（列出缺失属性名） |
| 物理列在目标数据源不存在 | `ValidationError`（提示本体与数据源漂移） |

### 3.4 数据源绑定：每个 Agent 一个

用户原则：「每个智能体都确定唯一的一个数据源就好了」+ **THBI 硬规则**（3.0.1）。

- `DELIVERY_TRACKING_AGENT` 绑定的数据源 = 源绑定行的 `datasource_id`
  = **`1`（THBI Oracle）** + `schema_name=THBI`；
- 工具执行时按绑定解析出的 `datasource_id`/`schema_name`/`source_table` 取数，
  **不在代码里写 `ZJTH.` / `THBI.` 前缀**；
- **场景不提供 `datasourceId` 切换槽位**（THBI 硬规则 ⇒ 切源即违规）：
  场景配置里删掉该隐藏槽位；既有 `pending_delivery` 场景的同名槽位一并移除；
- 工具**不再按「默认活跃源」兜底** —— 一律按绑定的 `datasource_id`；
  绑定缺失即 fail loud（`ValidationError`），**不静默落默认源**。

**跨数据源的行为**（回应用户「没有同一智能体跨数据源的能力，除非都是配置化的」）：

- 本方案**不支持**一个 Agent 同时跨多个数据源取数；
- 若业务要求换源，正确做法是**换绑定行**（同 `class_id` + 新 `datasource_id`/`schema_name`/
  `source_table` + 新属性绑定；旧行 `valid_to` 置值做软失效），
  而不是让工具里写死表前缀；
- 对象的**业务名**（订单号、供应商、订购量…）在本体层稳定，物理表列名随源变化 ——
  这正是本体层存在的意义。

### 3.5 字典表关联（同样走本体绑定）

供应商名 / 物料名通过本体类 `DIM_SUPPLIER` / `DIM_IMATERIAL` 关联
（两者本体均已登记，**绑定已确认**，属性绑定 248 / 229 条）：

- `DIM_SUPPLIER`（3,500 行，键唯一）：`BPSNUM_0` → `BPSNAM_0`
  —— 与 `ODS_ERP_PORDERQ` 的供应商码域 **100% 匹配**（1,191/1,191）；
- `DIM_IMATERIAL`（350,922 行，键唯一）：`ITMREF_0` → `ITMDES1_0`
  —— 与 `ODS_ERP_PORDERQ` 的物料码域 **100% 匹配**（306,939/306,939）。

**⚠ 字典覆盖率坑（探针实测）**：本体词表里的 `DIM_*` 与 `ODS_ERP_*` 是**两批不同抽取**，
`DIM_*` 的名称列覆盖率明显低于 `ODS_ERP_*`：

| 字典 | 行数 | 本期取名缺失 | 本体状态 |
|---|---|---|---|
| `THBI.ODS_ERP_ITMMASTER` | 366,611 | **2 行** | ❌ 未建类 |
| `THBI.DIM_IMATERIAL` | 350,922 | 1,883 行（9.4%） | ✅ 已绑 |
| `THBI.ODS_ERP_BPSUPPLIER` | 3,812 | **0 行** | ❌ 未建类 |
| `THBI.DIM_SUPPLIER` | 3,500 | 110 行 | ✅ 已绑 |

**决策**：名称从 **MDM `entity_mapping`** 取（`name` 字段），不 JOIN 数仓字典 ——
理由：(1) MDM 是企业主数据的 SSOT，供应商名本就以 MDM 为准；
(2) 免去「再为 `ODS_ERP_BPSUPPLIER` / `ODS_ERP_ITMMASTER` 建类」的工作量；
(3) 上一轮「供应商风险速览」已用 MDM 名称，跨场景一致。

若后续要改用数仓字典，则需一并建 `ODS_ERP_BPSUPPLIER` / `ODS_ERP_ITMMASTER` 两个类
（本期不做，见第 11 节）。

### 3.6 供应商码体系已打通

MDM `entity_mapping`（`entity_type=SUPPLIER`，3,500 行，`source_system=ERP`）的
`enterprise_code == source_code` 比例 **100%**，且 ERP 码集（1,193 个）**100% 落在** MDM 码集内。
因此：

- 供应商检索（编码 / 企业键 / 全名 / 简称）→ 复用 `Supplier360Service._resolveSupplier` 解析阶梯；
- 解析出的 `enterpriseCode` 可直接作为 `<SUPPLIER_CODE> IN (...)` 的过滤值。

实测（本期窗口）：`B125` → 1,006 行、`D1` → 8,354 行、`C1` → 7,657 行、`Q001` → 599 行。

### 3.7 到货明细表 `ODS_ERP_YPRECEIPTD` 的限制

82,435 行 / 19,351 订单 / 截止 2026-11-24，但 `BPSNUM_0` **100% 为空** ⇒ 不能直接按供应商过滤，
只能经订单行关联取供应商（实测 1,116 行中仅 3 行取不到名）。
且其覆盖订单数远小于 227,907，**不足以支撑「部分到货」判定**。

**决策**：到货判定基于订单行 ⋈ 收货单关联（本体口径，见 4.1），不依赖 ERP 到货明细表；
实际到货日期取收货类的 `RCV_DATE`（实测非空率 100%）。

### 3.8 待到货场景一并迁到本体 + THBI（用户 2026-11-25 决定：必须迁）

`pending_delivery_query` 目前**硬编码** `ZJTH.PORDERQ` / `ZJTH.ITMMASTER` /
`ZJTH.FACILITY` / `ZJTH.BPSUPPLIER` 与 ERP 原始列名（`QTYSTU_0` / `RCPQTYSTU_0` /
`EXTRCPDAT_0` / `BPSNUM_0` / `ITMREF_0` / `POHNUM_0` / `POPLIN_0` / `QTYUOM_0` …），
且查的是 **`ZJTH` owner** —— 违反 3.0.1 的 THBI 硬规则 ⇒ **迁移是必须项**。

**迁移方案：切到 THBI 数仓层（方案 A，定案）**

| 待到货现在用 | 迁移后用（THBI） | 本体状态 |
|---|---|---|
| `ZJTH.PORDERQ`（订单行） | `THBI.ODS_ERP_PORDERQ` | ❌ 本次**新建类 + 绑定** |
| `ZJTH.ITMMASTER`（物料名） | `THBI.DIM_IMATERIAL` | ✅ 已绑（248 属性） |
| `ZJTH.BPSUPPLIER`（供应商名） | `THBI.DIM_SUPPLIER` | ✅ 已绑（229 属性） |
| `ZJTH.FACILITY`（基地名） | `THBI.DIM_FACILITY` | ✅ 类已存在（117 属性）→ **补源绑定**（41 行） |

**THBI 侧对象核实（探针实测，全部就位）**：

- `THBI.DIM_FACILITY`：41 行，含 `FCY_0` + `FCYNAM_0`（本体类已有这两个属性），
  基地码形态与待到货现用的一致（`T1` 滨海总厂 / `T3` 模具厂 / `C1` / `J1` / `H1`…）
  ⇒ 现有 `siteKeys` 的「编码精确 / 名称模糊」过滤逻辑可原样复用；
- `THBI.DWD_PURCHASE_ORDER_DTL` 有 `PUR_SITE_CODE`（采购地点）—— 对应现有的 `Q.POHFCY_0`；
- **站点码域 100% 覆盖**：本期窗口 19,908 行 JOIN `DIM_FACILITY` **0 缺失**；
- 名称覆盖（本期窗口）：物料名缺 1,883 行（9.4%，`DIM_IMATERIAL`）、
  供应商名缺 110 行（0.55%，`DIM_SUPPLIER`）⇒ 印证 3.5 的决定：
  **名称改走 MDM `entity_mapping`**，避免数仓字典的缺失。

**迁移后的口径变化（必须显式告知）**：

| 维度 | 迁移前（ZJTH 源层） | 迁移后（THBI 数仓层） |
|---|---|---|
| 近 12 月行数 | 84,049 | **192,467**（2.3 倍） |
| 数据截止 | 2026-10-23 | **2026-11-24** |
| 站点维度 | `POHFCY_0` | `PUR_SITE_CODE`（同义，语义化） |
| 供应商/物料名 | ERP 表 | MDM（`entity_mapping.name`） |
| 欠交量 | `QTYSTU_0 - RCPQTYSTU_0` | `PO_QTY - 已收量`（本体口径，见 4.1） |

⇒ 待到货的现有实测结果**会变**（数据更多、更全、更新），
需重跑本地验收并**显式更新口径基线**（不静默漂移）。

**迁移实施要点**：

1. `pending_delivery_query` 的 SQL 模板从硬编码表列改为**绑定编译产物**
   （与到货跟踪共用同一套解析路径）；
2. 删掉 `_PENDING_TABLE` / `_PENDING_ITEM_TABLE` / `_PENDING_FACILITY_TABLE` /
   `_PENDING_SUPPLIER_TABLE` 四个常量与全部裸列名；
3. 欠交量口径改为本体口径：`PO_QTY`（`DWD_PURCHASE_ORDER_DTL`）
   − `RCV_QTY_STU`（`DWD_GOODS_RECEIPT_DTL`，按 `PO_NO`+`PO_LINE_NO` 聚合）；
4. 站点过滤从 `Q.POHFCY_0 IN (...)` / `f.FCYNAM_0 LIKE ...` 改为
   `<PUR_SITE_CODE>` / `<FCYNAM_0>` 属性绑定；
5. 移除场景的 `datasourceId` 隐藏槽位（3.0.1 推论 3）。

**顺带迁移：`purchase_history_query`**（用户 2026-11-25 决定：**暂不动，推迟删类**）

该工具当前硬编码 `_HISTORY_TABLE = "DWD_PURCHASE_ORDER_LINE"`（`agent_tools_procurement.py:231`）。
用户决定本轮**不改该工具、不删 `DWD_PURCHASE_ORDER_LINE`** ⇒
`DWD_PURCHASE_ORDER_LINE` 保留，其上挂的 6 个 KPI 口径锚点与图谱 JOIN 边**本轮也不动**。

**但「金额改从发票关联取」的设计决定已定（为将来改指时备好）**：

用户选定：金额口径改从 `DWD_PURCHASE_INVOICE_LINE` 关联取。
**探针实测该路径不可行**，必须记录原因（避免后来者重踩）：

| 候选 | 实测结论 |
|---|---|
| `DWD_PURCHASE_INVOICE_LINE` | ❌ **不可行**：数据截止 **2026-07-11**（比订单行旧 4 个月）；`PO_NO`/`PO_LINE_NO` **大量为 NULL**（样例 5 行全 NULL）；本期订单行关联命中率 **0.00%**；且该类**既无源绑定也无属性绑定** |
| `DWD_PURCHASE_INVOICE_DTL`（发票头行） | ⚠️ **部分可行但有硬伤**：数据新鲜（至 **2026-11-24**）、有 `PO_NO`+`PO_LINE_NO`+`NET_UNIT_PRICE`+`INV_QTY`，抽样验证 `INV_QTY × NET_UNIT_PRICE` 与旧批次 `LINE_AMOUNT_EXCL_TAX` 吻合；**但** ① `PO_NO` 空值率逐年恶化（2024: 20.5% → 2025: 56.4% → 2026: 44.7%）② **该表无索引**（`ALL_IND_COLUMNS` 查无），与 103 万行订单表 join **直接超时** ③ 一订单行最多对应 **11 条发票行**（381 键重复）⇒ 金额须 `SUM(INV_QTY × NET_UNIT_PRICE)` 聚合 |
| 保留旧类取金额 | ✅ `DWD_PURCHASE_ORDER_LINE` 有 `LINE_AMOUNT_EXCL_TAX`/`INCL_TAX`（非空率 99.998%）⇒ **本轮 `purchase_history_query` 不动即可保住金额口径** |

⇒ **本轮结论**：`purchase_history_query` 与 `DWD_PURCHASE_ORDER_LINE` 保持原样
（用户决定），金额口径暂不变；将来若要改指新类，须先解决发票关联的
**覆盖率**（`PO_NO` 空值）与**性能**（缺索引）两个问题，或改用价格表
（`DWD_SUPPLIER_PRICE_LIST_HEADER`，本体已绑，17,749 行）另建关联路径。

## 4. 口径定义（探针实测结论）

### 4.1 到货判定：行级量（本体口径），不看订单状态

```
已收量（行级）  RECEIVED = SUM(<RCV_QTY_STU>)     -- 收货类按 PO_NO+PO_LINE_NO 聚合
行级欠交量      OPEN_QTY = <PO_QTY> - RECEIVED    -- 同为库存单位
订单三态：
  全到货    SUM(OPEN_QTY) = 0
  部分到货  有欠交 且 有已收（RECEIVED > 0）
  未到货    所有行 RECEIVED = 0
```

（`<属性名>` = 本体属性，由绑定编译解析为物理表列，见 3.3。
`<PO_QTY>` 来自订单行类 `DWD_PURCHASE_ORDER_DTL`，
`<RCV_QTY_STU>` 来自收货类 `DWD_GOODS_RECEIPT_DTL`，两者按 `PO_NO`+`PO_LINE_NO` 关联。）

**为什么不用订单头 `RCPLINNBR_0 >= LINNBR_0`**：头行两种判定在近 12 月仅差 4 单、本期差 0 单，
但头表另有 11,817 张「已关闭（CLEFLG_0=2）却仍有欠交」的订单，且其欠交总量级异常
（4.5e18），把闭单当已完结会**假绿**。行级量判定与既有 `pending_delivery_query` 同源，
两个场景可对照。

**为什么用收货单而不是订单行自带的累计收货字段**：订单行 `DWD_PURCHASE_ORDER_DTL`
**没有**累计收货列；本体里「已收量」的载体是收货类 `DWD_GOODS_RECEIPT_DTL`。
（备选的 ERP 行级累计量口径见 3.2 存档段，两者差异仅 2 单边界。）

### 4.2 核心指标：订单到货率为主，量级率为参考

| 指标 | 角色 | 定义 | 本期实测（本体口径） |
|---|---|---|---|
| **订单到货率** | **主指标** | 全到货订单数 / 总订单数 | **82.29%**（3,986 / 4,844） |
| 量级加权到货率 | 参考 | `SUM(RECEIVED) / SUM(<PO_QTY>)` | 40.51% |
| 订单三态 | 辅指标 | 全到货 / 部分到货 / 未到货 订单计数 | 3,986 / 448 / 410 |

**为什么量级率不能当主指标（探针实证，重要）**：

本期 19,908 行中，「完全未到货」行（`RECEIVED = 0`）有 2,251 行，但它们贡献了
**39.00% 的订购量**；其中订购量 ≥ 10 万的大额行仅 75 行，却贡献了 **22.68% 的订购量**。

样例订单 `T72610POH1134`（供应商 B161）：

```
POPLIN 1000  ITMREF 501010080001  NL-4限位器压缩弹簧   订购 2,650,000  已收 0
POPLIN 2000  ITMREF 501011050001  F507限位器压缩弹簧   订购   298,000  已收 0
POPLIN 4000  ITMREF 501014810001  FE-5 压缩弹簧       订购   430,000  已收 0
…（共 7 行，仅 3 行有部分收货）
```

**这不是录入错误** —— 订购量分布本身正常（P50=230、P90=8,250、P99=76,728、
P999=500,000、MAX=2,650,000），是真实的业务大单。
剔除大额行后期率收敛到 **80.04%**（阈 1 万），与订单到货率 82.29% 仅差 2.2pp
⇒ 两者本应一致，差异纯粹来自少数超大额未到货行的**权重放大**。

**决策**：
- 主指标用**订单到货率**（订单个数口径，不受大单权重影响）；
- 量级率作为**参考值展示，并附口径说明**（「量级加权，受超大额未到货行影响」）；
- **不做**「剔除大额行」的处理 —— 那会删掉真实业务大单，分子分母都不含它们，
  业务不会接受；
- 脏数据（欠交 ≥ 1e6）仍按既有机制剔除并回报 `excludedRows`（本期 6 行），
  但**须说明**：剔除量级行不改变率值结论（剔前后订单率 82.29% vs 82.28%）。

### 4.3 时间窗口

- **锚定字段**：下单日期 `<ORDER_DATE>`（与需求「某个时间段的订单」一致，且与既有场景同口径）。
- **默认口径**：自然月窗口 `monthsBack=12`（`today - 12 月` 起），
  覆盖近 12 月 192,467 行 / 46,770 张订单。
- **24 日锚点（可选参数）**：`anchorDay=24` + `periodIndex=N`。

  **计算规则**（左闭右开，与探针实测边界一致）：

  ```
  anchor = 当月锚点日；若 anchor > 今天，则 anchor = 上月锚点日
  periodIndex=0（本期）  = [anchor 上月同日, anchor)
  periodIndex=-1（上期） = [anchor 前两月同日, anchor 上月同日)
  periodIndex=N         = [anchor + (N+1) 月同日, anchor + N 月同日)
  ```

  即**本期 = 已结束的最近一个完整锚点周期**（今天 2026-11-25 ⇒ 本期 = 2026-10-24 ~ 2026-11-24）。
  实测各期数据量：

  | 期 | 窗口 | 行数 | 订单数 |
  |---|---|---|---|
  | 本期（0） | 2026-10-24 ~ 2026-11-24 | 19,910 | 4,844 |
  | 上期（-1） | 2026-09-24 ~ 2026-10-24 | 22,419 | 5,089 |
  | 上上期（-2） | 2026-08-24 ~ 2026-09-24 | 21,819 | 4,973 |
  | 下期（1，未结束） | 2026-11-24 ~ 2026-12-24 | 1,200 | 159 |

  锚点日上限 28（避免 29/30/31 在短月无对应日；2 月边界实测 `2026-02-28~2026-03-28` 正常）。
- **优先级**：`dateFrom/dateTo` 显式区间 > 24 日锚点 > `monthsBack`
  （沿用 `PendingDeliveryFilters` 语义：给了显式区间则忽略 `monthsBack`）。

### 4.4 脏数据处理

沿用既有机制：欠交量 ≥ 1e6 判为源侧录入错误并剔除，但**显式回报** `excludedRows`，
不静默丢。数仓层全量 54 行 / 本期 6 行（与源层 43 行同量级）。

**注意**：剔除欠交脏行**不改变**到货率结论（本期订单率 82.29% → 剔后 82.28%），
也**不解决**量级率与订单率的差距（见 4.2，差距源于真实大单）。

### 4.5 未到货 ≠ 逾期

本期 2,251 条完全未到货行中，**2,072 条的预期到货日已过**（`<PROMISED_RECEIPT_DATE> <= 窗口末`），
仅 179 条尚未到期（92%）。因此「未到货」行绝大多数是**真逾期**，不是「还没到期的正常在途」。

订单三态中的「未到货」因此可直接理解为「基本已逾期」，但**卡上仍用「未到货」措辞**
（与需求一致），不擅自改成「逾期」—— 逾期判定属于「待到货 / 逾期未到」场景的职责，
两个场景措辞边界要清晰。

## 5. 架构

### 5.1 组件

| 组件 | 类型 | 位置 | 职责 |
|---|---|---|---|
| **`DIM_FACILITY` 源绑定** | 本体资产 | 新增 seed（`procurement_source_binding`） | 基地字典的唯一缺口（类与 117 属性已存在）；待到货 `siteKeys` 过滤用 |
| **`POL` BO 换绑** | 本体资产 | **本轮推迟**（独立立项） | 头类 `DWD_PURCHASE_ORDER_LINE` → `DWD_PURCHASE_ORDER_DTL`（选项 A）；连带迁 6 个 KPI 口径源与图谱边；依赖链见 3.2.1 |
| 绑定解析 | 服务（**复用**） | `backend/app/services/semantic_compiler_bindings.py` | 类+数据源 → 物理表/列；候选隔离；fail-closed |
| `delivery_tracking_query` | 工具 | `backend/app/services/agent_tools_procurement.py` + `seed_agent_tool_configs.py` | 概览 + 双层明细取数（订单行 ⋈ 收货单，经绑定解析物理表列） |
| `DELIVERY_TRACKING_AGENT` | Agent | `backend/scripts/seed_agents.py` + `seed_procurement_agent_profiles.py` | 编排与输出纪律；**绑定唯一数据源（THBI）** |
| `delivery_tracking` | 场景配置 | `backend/scripts/seed_copilot_scenarios.py` | 卡片墙 / 槽位 / 模板 / 渲染器绑定 |
| `DeliveryTrackingResultCard` | 前端组件 | `frontend/src/components/copilot/` | 结果卡（概览 + 双 Tab） |
| `pending_delivery` 渲染器扩展 | 前端 | `PendingDeliveryResultTabs.tsx` | 分发到新卡 |
| **待到货迁移** | 改造 | `agent_tools_procurement.py`（`pending_delivery_query`） | 去硬编码，改走本体绑定 + THBI（见 3.8） |
| 真实数据门禁 | 脚本 | `backend/scripts/procurement_class_source_table_realdata.py` 同款 | 绑定后证明生效（防重演 2026-11-18 事故） |

### 5.2 为什么不复用 `pending_delivery_query`

两者语义正交：

- 待到货：**欠交视角**（只关心没到的），输出催货建议；
- 到货跟踪：**到货视角**（关心整体到货率与已到/未到分布），输出到货达成情况。

共用部分通过抽公共件实现，不共用 SQL 模板：
`_PendingDialect`（方言碎片）、`PendingDeliveryFilters`（过滤/窗口）的思路可提取，
但窗口语义不同（到货跟踪要 24 日锚点），故新写。

**数据源（THBI 硬规则）**：迁移完成后，**两个场景同源同口径** —— 都走
`THBI.ODS_ERP_PORDERQ`（经本体绑定），都从 MDM 取名称。
用户在两个场景看到的数字可以对得上。

迁移前后的口径差异（待到货）已在 3.8 列出，界面须标注数据层以免误读：
近 12 月行数 84,049 → **192,467**（2.3 倍），数据截止 2026-10-23 → **2026-11-24**。

### 5.3 工具契约

```
delivery_tracking_query(
  monthsBack: int?      # 默认 12；给了 dateFrom/dateTo 则忽略
  dateFrom: str?        # YYYY-MM-DD，与 dateTo 成对
  dateTo: str?
  anchorDay: int?       # 24 日锚点（可选），1..28
  periodIndex: int?     # 锚点期序，0=本期，-1=上期
  supplierKeys: str[]   # 供应商码列表（多选，缺省=全部）
  materialKeys: str[]   # 物料列表（可选）
  orderKeys: str[]      # 订单号列表（可选，下钻指定订单）
  limit: int            # 订单明细上限，默认 50
)
```

**无 `datasourceId` 参数**（THBI 硬规则：数据源由本体绑定唯一确定，不由调用方指定）。

**返回结构**（camelCase，与 `PendingDeliveryResult` 风格一致）。
下列数值为 2026-11-25 探针在**本期窗口（2026-10-24 ~ 2026-11-24）**的实测值：

```jsonc
{
  "via": "business_db",
  "datasourceId": 1,
  "dataLayer": "thbi_ods_erp",
  "window": { "dateFrom": "2026-10-24", "dateTo": "2026-11-24", "mode": "anchorDay" },
  "summary": {
    "totalLines": 19910,
    "totalOrders": 4844,
    "orderedQty": 98495267.255,
    "receivedQty": 39901874.255,
    "orderArrivalRate": 82.29,    // 主指标：全到货订单 / 总订单 = 3986/4844
    "qtyArrivalRate": 40.51,      // 参考：量级加权 = 39901874/98495267（受超大额未到货行影响）
    "allArrivedOrders": 3986,
    "partialArrivedOrders": 448,
    "notArrivedOrders": 410,
    "excludedRows": 6             // 欠交≥1e6 的脏行，本期 6 行
  },
  "bySupplier": [
    { "supplierCode": "B125", "supplierName": "浙江力航汽车部件有限公司",
      "enterpriseKey": 195779358766766,
      "totalOrders": 29, "allArrivedOrders": 1, "partialArrivedOrders": 16,
      "notArrivedOrders": 12, "orderArrivalRate": 3.45,
      "orderedQty": 15155100.0, "receivedQty": 2176026.0, "qtyArrivalRate": 14.36 }
  ],
  "byOrder": [
    { "poNo": "T22611POH0223", "supplierCode": "B125",
      "supplierName": "浙江力航汽车部件有限公司",
      "orderDate": "2026-10-28", "status": "partial",   // all | partial | notArrived
      "totalLines": 1, "arrivedLines": 0, "notArrivedLines": 1,
      "lines": [
        { "poLine": 3000, "materialCode": "5A2100000002",
          "materialDesc": "kun10/kun30/N1/N2方形焊接螺母M6",
          "orderedQty": 1047000.0, "receivedQty": 118261.0, "openQty": 928739.0,
          "expectedDate": "2026-11-07", "lastReceiptDate": "2026-11-24",
          "status": "partial" }   // all | partial | notArrived
      ]
    }
  ]
}
```

**⚠ 量纲异常（必须处理）**：本期 `orderedQty` 合计 9.85e7，而 `receivedQty` 仅 4.0e7 ——
**行级到货率只有 40.5%**，远低于订单到货率 82.29%。原因：单行订购量存在百万级脏值
（`5A2100000002` 一行订购 1,047,000）。既有 `pending_delivery_query` 已用「欠交量 ≥ 1e6 剔除」
处理同类问题，但**本场景的分子分母都受影响**——若只剔欠交侧不剔订购侧，率值仍然失真。

**决策**：本场景的率值计算**必须把订购量 ≥ 1e6 的行整体排除**（分子分母同时排除），
并在 `summary.excludedRows` 显式回报剔除行数。剔除后需重算率值并在卡上标注口径。
**验收要求**：剔脏后行级到货率须与订单到货率同量级（否则口径仍有洞，须在实施时复核）。

### 5.4 数据流

```
前端场景卡 → POST /copilot/messages { scenarioCode, slots }
  → 后端 _renderScenarioQuestion（冲突让位 + 模板渲染）
  → Agent 编排 → DELIVERY_TRACKING_AGENT 调 delivery_tracking_query
  → 工具按窗口/供应商过滤 → 三段 SQL（summary / bySupplier / byOrder）
  → 结果写 turn steps → 前端按 result_renderer="delivery_tracking" 分发到结果卡
```

槽位 → 工具参数的映射沿用既有约定：渲染器把槽位值拼进提问的「参数尾注」，
规划器按工具 `input_schema` 提取。

### 5.5 场景配置

```jsonc
{
  "scenario_code": "delivery_tracking",
  "name": "到货跟踪",
  "description": "按周期/供应商跟踪订单到货达成，含已到/未到物料明细",
  "icon_code": "car",
  "agent_code": "DELIVERY_TRACKING_AGENT",
  "slot_schema": [
    { "key": "monthsBack", "type": "number", "label": "时间窗", "required": true,
      "conflictKeywords": ["时间窗", "个月", "时间段", "日期", "上半年", "下半年", "本年度", "本年"],
      "displayTemplate": "近{value}个月" },
    { "key": "anchorDay", "type": "number", "label": "月度周期锚点日",
      "conflictKeywords": ["周期", "24日", "结算周期"] },
    { "key": "periodIndex", "type": "number", "label": "周期期序",
      "conflictKeywords": ["本期", "上期", "上个月"] },
    { "key": "supplierKeys", "type": "entityMulti", "label": "供应商",
      "conflictKeywords": ["供应商", "厂家"],
      "codePattern": "(?<![0-9A-Za-z])[A-Z]\\d{2,4}(?![0-9A-Za-z])" },
    # materialKeys 槽位已于 2026-11-25 推迟（见 11.3）：用户原始需求 1-4 未含按物料过滤；
    # 增加一个静默不过滤的 UI 控件比不加更糟。工具侧亦未实现该过滤。
    { "key": "orderKeys", "type": "entityMulti", "label": "订单",
      "conflictKeywords": ["订单", "订单号"] }
  ],
  "question_template": "分析订单到货情况",
  "chips": [
    { "label": "近 12 个月到货概览", "monthsBack": 12 },
    { "label": "近 3 个月到货概览", "monthsBack": 3 },
    { "label": "本期到货概览（24 日周期）", "anchorDay": 24, "periodIndex": 0 },
    { "label": "上期到货概览（24 日周期）", "anchorDay": 24, "periodIndex": -1 }
  ],
  "result_renderer": "delivery_tracking",
  "required_role": null,
  "sort_order": 30,
  "enabled": true
}
```

**无 `datasourceId` 槽位**（THBI 硬规则 ⇒ 数据源由本体绑定唯一确定，不给用户切源）。

**`anchorDay` / `periodIndex` 的槽位类型**：现有 `SLOT_TYPE_WHITELIST` 无「周期」类型。
两者均用 `number` 类型承载，后端在渲染时做语义转换（见 5.6），
故**无需扩展白名单**、**无需前端新增控件**（InputNumber 已支持 number）。

### 5.6 周期参数在渲染层的展开

槽位值 `anchorDay=24, periodIndex=0` 无法直接作为工具参数（工具要的是日期区间）。
展开发生在**渲染器**（`copilot_scenario_render.py`）：渲染时若检测到 `anchorDay`，
按 4.3 规则算出 `dateFrom/dateTo`，并以显式区间形式进入参数尾注：

```
分析订单到货情况（时间窗=近12个月）。请调用场景绑定工具完成取数与分析
monthsBack=12；dateFrom=2026-10-24；dateTo=2026-11-24。
```

冲突让位规则：若用户文字已指明周期（「上期」「本月」），`periodIndex` 槽位让位，
不重复进尾注（由 `conflictKeywords` 命中判定）。

### 5.7 供应商检索

复用 `Supplier360Service._resolveSupplier` 阶梯（编码 → 企业键 → 全名精确 → 全名模糊），
解析出 `enterpriseCode` 后作为 `<SUPPLIER_CODE> IN (...)` 过滤值。

**多命中消歧**：全名模糊命中多个供应商时抛 `NotFoundError` 并列候选
（与上一轮「供应商风险速览」一致），由规划器/用户消歧，**绝不静默取第一个**。

注意：ERP 侧供应商名称与 MDM `name` 可能不同，展示以 MDM `name` 为准，回退 ERP 名。

### 5.8 本体对接的落地机制（复用既有绑定设施）

工具不直接写物理表名，取数前先做一次**绑定编译**，把语义名换成物理标识：

```
输入：类名 ODS_ERP_PORDERQ + 数据源 id + 需要的属性名列表
  ↓ semantic_compiler_bindings.compileSourceBindings()
  ├─ 读 procurement_source_binding（valid_to IS NULL 且 is_candidate=false）
  │     → datasource_id + schema_name + source_table
  ├─ 读 procurement_property_binding
  │     → 属性名 → source_column
  └─ 缺失校验：必需属性齐全？物理列在该数据源真实存在？
  ↓
输出：{ datasourceId: 1, table: "THBI.ODS_ERP_PORDERQ",
        columns: { PO_QTY: "PO_QTY", RCV_QTY_STU: "RCV_QTY_STU", ... } }
  ↓
SQL 模板按解析结果渲染（表名/列名均为绑定解析产物，非代码常量）
```

**实现要点**：

| 要点 | 做法 |
|---|---|
| 解析时机 | 工具执行时从 DB 读，**不进代码常量** —— 绑定是可治理资产 |
| 生效过滤 | `valid_to IS NULL`（软版本化：旧绑定置 `valid_to` 即失效） |
| 候选隔离 | `is_candidate=true` 的绑定不进读路径（DB CHECK 已钉候选↔无确认人） |
| 类墓碑 | 类 `valid_to` 非空 ⇒ fail loud，不静默换类 |
| 属性缺失 | `ValidationError` 并列出缺失属性名（不静默用空列） |
| 物理列漂移 | 校验物理列在该数据源存在；不存在 ⇒ `ValidationError` 提示漂移 |
| SQL 注入 | 解析出的表名/列名仍走既有标识符白名单校验（`procurement_binding_validation`） |
| 缓存 | 可按「类 + 数据源 + 属性集」缓存；本体/绑定变更时须失效 |

**为什么不用 NL2SQL 那条路**：NL2SQL 走 LLM 生成 SQL，适合开放式问句；
本场景是**固定口径的指标查询**（到货率三态），需要确定性 ⇒ 模板 SQL + 绑定编译，
不让 LLM 参与取数（与既有 `pending_delivery_query` / `purchase_history_query` 同一思路）。

## 6. 前端结果卡

```
┌────────────────────────────────────────────────────────────┐
│ 到货达成概览                              [数仓层 ODS_ERP]  │
│  订单到货率 82.29%   ← 主指标                              │
│  [全到货 3,986] [部分到货 448] [未到货 410]                 │
│  参考：量级加权到货率 40.51%（受超大额未到货行影响）        │
│  脏数据剔除 6 行                                            │
├────────────────────────────────────────────────────────────┤
│ 供应商到货情况（表格，可排序）                              │
│  供应商 | 名称 | 订单数 | 全到 | 部分 | 未到 | 到货率 | 欠交量│
├────────────────────────────────────────────────────────────┤
│ 订单明细  [未到货订单] [已到货订单]                          │
│  订单号 | 供应商 | 状态 | 已到/总行数 | 下钻 → 物料行明细    │
└────────────────────────────────────────────────────────────┘
```

- **主指标**：订单到货率（大号数字）；三态计数用带色 Tag（绿/橙/红）。
- **量级率**：次要行展示，附口径说明（tooltip），避免用户误读为"只有 40% 到货"。
- **双 Tab**：未到货订单（部分 + 未到货，含部分到货行）在前，已到货订单在后 —— 业务先看问题。
- 状态色：全到货绿 / 部分到货橙 / 未到货红。
- 数据层标注：卡片角标显示「数仓层 ODS_ERP」，避免与待到货场景（源层）误读。
- 明细下钻：订单行展开显示物料行（编码 / 描述 / 订购 / 已收 / 欠交 / 预期到货日 / 最后到货日）。

## 7. 错误处理

| 场景 | 行为 |
|---|---|
| **本体类不存在 / 已墓碑** | `NotFoundError`（提示「本体类未建或已删除，请先建类」）—— 不回退硬编码表 |
| **本体必需属性缺失** | `ValidationError` 并列出缺失属性名 |
| **物理列在数据源不存在**（本体与数据源漂移） | `ValidationError`（提示漂移），**不静默改列** |
| **类未绑定数据源**（`source_table` 为空） | `ValidationError` |
| 供应商多命中 | `NotFoundError` 列候选（「请用编码或企业键指定」） |
| 供应商不存在 | `NotFoundError` |
| 窗口内无数据 | 正常返回，summary 全 0 + 说明性 answer（「该期间无下单记录」） |
| 非法 `anchorDay`（<1 或 >28） | `ValidationError`（fail-closed，不静默纠正） |
| 非法日期格式 | `ValidationError`（沿用 `PendingDeliveryFilters.__post_init__`） |
| 数据源不存在/停用 | `ConflictError`（沿用 `_loadActiveDatasource`） |
| 切了数据源但该源内无对应类/表 | `ValidationError`（不静默落默认源） |
| 脏数据行 | 剔除并在 `summary.excludedRows` 显式回报 |

## 8. 测试计划

### 8.1 后端单测

- **本体解析**（新增组件，重点）：
  - 类存在 → 解析出 `source_table`；类墓碑（`valid_to` 非空）⇒ fail loud；
  - 属性名 → 物理列映射正确；必需属性缺失 ⇒ `ValidationError` 且列出缺失名；
  - 物理列不存在于目标数据源 ⇒ `ValidationError`（提示漂移）；
  - 解析结果缓存命中与失效；
- SQL 模板生成：窗口谓词三形态（`monthsBack` / `dateFrom+dateTo` / 24 日锚点），
  断言边界为左闭右开 `>= AND <`；
- 24 日锚点日期计算：跨月（11-24 → 12-24）、跨年（12-24 → 次年 1-24）、2 月（28 日上限）；
- 订单三态判定：全到 / 部分 / 未到（含「部分到货后全部到齐」归入全到货）；
- 供应商码解析：编码 / 企业键 / 全名 / 简称 / 多命中消歧；
- 方言分叉：Oracle 与 MySQL 两种 `tail` 语法（沿用 `_PendingDialect`）。

### 8.2 后端集成测试（真实 PostgreSQL + 真实 API 链路）

- **本体建类门禁**（对照 `procurement_class_source_table_realdata.py` 的形态）：
  干净库跑迁移链 ⇒ 类存在、`source_table` 指向真实存在的表、
  10 个属性的 `source_column` 全部存在、`seed_ontology.seed()` 不撞唯一约束；
- 场景配置 CRUD 与白名单校验（`result_renderer=delivery_tracking` 须在白名单内）；
- 场景列表返回新场景，`required_role` 过滤生效；
- 渲染器：槽位 → 提问文本，24 日锚点展开为显式区间，冲突让位生效。

### 8.3 口径校验测试（防回归，最重要）

用**固定探针基线**锁定口径，防止后续改动悄悄改变率值：

| 校验项 | 期望（本期窗口 2026-10-24~11-24 实测） |
|---|---|
| 订单总数 | 4,844 |
| 全到货 / 部分 / 未到 | 3,986 / 448 / 410 |
| 订单到货率 | 82.29% |
| 总行数 | 19,910 |
| 量级率（参考值） | 40.51%（**允许漂移，但须复核**） |
| 脏数据行 | 6 |

量级率对数据变化敏感，**不作为硬断言**；但订单三态与订单率必须精确匹配基线。
**基线随本体绑定走**：换数据源/换类后基线须重算并显式更新（不静默漂移）。

### 8.4 前端测试

- `parseDeliveryTrackingStep`：解析工具 observation；坏 JSON / 非本工具 ⇒ `null`；
- 结果卡：主指标（订单到货率）为大号数字、量级率为次要行且带口径说明、
  三态计数、双 Tab 切换、物料行明细渲染。

### 8.5 实测验收（本地栈）

- 用真实 Oracle 数据跑通：默认 12 月窗口 → 概览 + 供应商表 + 订单明细；
- 24 日锚点本期窗口 → 4,844 张订单，与 8.3 基线逐项对齐；
- 多供应商筛选（B125 / D1 / C1）；
- 供应商简称/名称检索（走解析阶梯）；
- 明细行的物料名非空（缺失率应 ≈ 0）。

## 9. 不做（YAGNI）

- 不做双层数据源切换参数（当前只有数仓层一个真实需求）；
- 不做 `generic_table` 通用渲染器（本场景专用卡即可，避免过度抽象）；
- 不做跨 schema JOIN；
- 不做订单头状态过滤（行级量判定已足够，且避开闭单假绿）；
- 不做「一个 Agent 跨多个数据源」（用户原则：跨源应换绑定，不在工具里拼源）；
- 不为 `ODS_ERP_BPSUPPLIER` / `ODS_ERP_ITMMASTER` 建类（名称走 MDM，见 3.5）；
- 不做物化视图 / 预聚合表（当前 192k 行规模，实时聚合足够）。

## 10. 风险

| 风险 | 缓解 |
|---|---|
| **建类引入本体绑定漂移**（2026-11-18 已发生过一次） | 绑定走既有治理流程 + 真实数据门禁脚本；类/属性/绑定同步登记 |
| 本体类被治理侧删除/改绑，工具随之失效 | 解析 fail-closed + 明确报错（不静默换表） |
| **场景数据与 KPI 数据暂时不同源** | 本轮推迟删 `DWD_PURCHASE_ORDER_LINE` 的直接后果：场景走新鲜批次（至 11-24），6 个 KPI 仍走旧批次（至 08-08）。**界面上不得把两者并列比较**；后续换绑立项时一并收敛（3.2.1） |
| 待到货迁移后**现有实测结果变化** | 迁移前后跑同一组基线并显式对比；口径基线随迁移更新（不静默漂移） |
| 两个场景若不同源，用户发现数字对不上 | 方案 A 统一到数仓层（推荐）；卡片标注数据层 |
| 数仓层供应商码与 MDM 码域未来漂移 | 解析统一走 MDM；过滤前校验码在 MDM 域内 |
| 订单明细行数膨胀 | `limit` 上限 50（沿用 `_MAX_LIMIT`），概览层不受影响 |
| 24 日锚点与「自然月」两套窗口并存易混 | 卡上显示实际 `window.dateFrom/dateTo`，不只显示口径名 |
| 量级率被超大额行主导，用户误读 | 主指标用订单率；量级率降为参考并附口径说明 |

## 11. 决策台账

### 11.1 已由用户拍板（2026-11-25）

| # | 决定 | 影响 |
|---|---|---|
| ✅ 1 | **一切数据对象出自 THBI 数据源**（硬规则） | 3.0.1；待到货必须迁；取消 `datasourceId` 槽位 |
| ✅ 2 | **本体对接，不硬编码表名列名** | 3.0.2，复用既有绑定设施（`procurement_source_binding` / `procurement_property_binding` / `semantic_compiler_bindings`） |
| ✅ 3 | **待到货场景一并迁到本体** | 3.8，切 THBI 数仓层 |
| ✅ 4 | **`DWD_PURCHASE_ORDER_DTL` 是订单行表**（对应 PORDERQ 订单行） | 修正了「订单头」的误判；到货率改走「订单行 ⋈ 收货单」 |
| ✅ 5 | **`FACILITY` 基地名在系统里已有对应** | `DIM_FACILITY` 类已存在（117 属性）⇒ 只需补绑定，不新建类 |
| ✅ 6 | **BO 用选项 A：给既有 `POL` 换绑到 `DWD_PURCHASE_ORDER_DTL`** | 3.2.1；**本轮推迟执行**（连带迁 6 个 KPI 口径源 + `purchase_history_query` + 图谱边） |
| ✅ 7 | **`DWD_PURCHASE_ORDER_LINE` 本轮保留，删类推迟** | 3.2.1 依赖链；用户决定「暂不动 `purchase_history_query`，推迟删类」 |
| ✅ 8 | **金额改从发票关联取**（设计决定） | 3.8；⚠️ 实测 `DWD_PURCHASE_INVOICE_LINE` 不可行（`PO_NO` 多空 + 数据旧 4 个月 + 无绑定），`DWD_PURCHASE_INVOICE_DTL` 有覆盖率与性能硬伤 ⇒ 本轮金额口径暂不变 |

### 11.3 执行期调整（2026-11-25，实现阶段发现）

| # | 调整 | 理由 |
|---|---|---|
| 1 | **移除 `materialKeys` 槽位（本场景）** | 用户原始需求 1-4 未包含按物料过滤；加一个静默不过滤的 UI 控件比不加更糟；实现它要改三个 SQL builder 并改变率值语义 ⇒ YAGNI。工具侧 `delivery_tracking_query` 亦不接受该参数 |
| 2 | **`byOrder` 订单级派生值剔除脏行** | 与 `summary`/`bySupplier` 口径对齐：`lines[]` 保留脏行供核查，但订单级 `status`/行数/数量只用非脏行；新增行级 `isDirty` 与订单级 `hasDirtyLines` 供前端提示 |
| 3 | **`NOT_ARRIVED` 谓词补 `OPEN_LINES > 0`** | 原谓词会让「订购量 ≤ 0 且未收货」的订单**同时**计入全到货与未到货 ⇒ 到货率可 > 100%。装配层另加 `DeliveryTrackingInvariantError` 兜底 |
| 4 | **供应商名 ERP 回退落地** | spec 5.7 原本只写了「以 MDM 为准，回退 ERP 名」；实现确认 `SUPPLIER_NAME` 在订单行/收货两类均已绑定，作为**可选**属性使用（缺绑定不 fail-closed） |

### 11.2 按建议定稿（2026-11-25 用户「没有异议」）

| # | 议题 | 定稿 | 依据 |
|---|---|---|---|
| ✅ 9 | 字典来源 | **统一走 MDM `entity_mapping`** | 数仓 `DIM_SUPPLIER` 覆盖率 99.97%、`DIM_IMATERIAL` 90.6%；MDM 是主数据 SSOT，免建类、跨场景一致 |
| ✅ 10 | 到货跟踪的基地维度 | **本期不加** | 需求只提时间/供应商/订单/物料；YAGNI，可后补 |
| ✅ 11 | 到货明细 `ODS_ERP_YPRECEIPTD` | **本期不接** | `BPSNUM_0` 全空、覆盖仅 41% 订单；本体收货类已够用；需批次追溯时再立项 |
| ✅ 12 | 待到货迁移的口径变化告知 | **卡片标注数据层 + 迁移说明；口径基线显式更新** | 近 12 月行数 84,049 → 192,467（2.3 倍），数据截止 10-23 → 11-24 |
| ✅ 13 | 订单到货率口径 | **收货单口径（82.29%）** | 更贴近业务事实（收货单已开但 ERP 累计未回写的 2 单），且数据新鲜 |
| ✅ 14 | 场景与 KPI 暂时不同源 | **卡片显式标注各自数据层与截止日；不在同一张卡并列两者** | 本轮推迟删类的直接后果（见 3.2.1 口径割裂表） |

**本需求的最终范围**：

| 做 | 不做（独立后续立项） |
|---|---|
| 到货跟踪场景（本体订单行 ⋈ 收货单） | `purchase_history_query` 改指 + 金额口径改造 |
| 待到货场景迁到本体 + THBI | 删 `DWD_PURCHASE_ORDER_LINE` + `POL` BO 换绑 + 6 个 KPI 换指 + 图谱边换指 |
| `DIM_FACILITY` 补源绑定（唯一新增本体资产） | 到货跟踪的基地维度 |
| 24 日锚点周期 + 双指标结果卡 | `ODS_ERP_YPRECEIPTD` 到货明细接入 |

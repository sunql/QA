# 到货跟踪场景（P5）设计文档

日期：2026-11-25
分支：`feat/ontology-evolution`
状态：待评审

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
| `DWD_PURCHASE_ORDER_DTL` | `PO` 采购订单 | ✅ `THBI.DWD_PURCHASE_ORDER_DTL` | 30（已确认） | 1,036,069 | 2026-11-24 | ❌ **无 `RECEIVED_QTY`**（只有 `PO_QTY`）、无 `MATERIAL_CODE` |
| `DWD_PURCHASE_ORDER_LINE` | `POL` 采购订单行 | ✅ `THBI.DWD_PURCHASE_ORDER_LINE` | 29（已确认） | 927,648 | **2026-08-08** | ✅ 全部齐（含 `ORDER_QTY`/`RECEIVED_QTY`/`LAST_RECEIPT_DATE`） |
| `ODS_ERP_PORDERQ` | — | ❌ **未建类** | — | 1,036,089 | 2026-11-24 | 物理列齐（150 列，10 个目标列非空率 100%） |

**核心矛盾（本体侧的结构性缺口）**：

> **「有到货量的行表是旧的，有新数据的表缺到货量」—— 正好差 `RECEIVED_QTY` 这一列。**

- `DWD_PURCHASE_ORDER_DTL`（新鲜，1,036,069 行 / 227,907 订单 / 至 2026-11-24）
  物理列 35 个，含 `PO_QTY`（订购量）但**没有 `RECEIVED_QTY`** ⇒ **算不出到货率**；
- `DWD_PURCHASE_ORDER_LINE`（属性最全，29 个已确认绑定，含 `RECEIVED_QTY`）
  数据停在 **2026-08-08** ⇒ 本期窗口 **0 单**（而需求 3 的本期有 4,844 单）；
- `ODS_ERP_PORDERQ`（新鲜全量 ERP 行表，1,036,089 行）**本体未建类** ——
  按 `backend/app/domain/procurement_object_catalog.py:413` 的既有登记，
  `ODS_ERP_*` 属「其它采购相关源表（未建类）」，
  `carriedBy = "单独立项（按需建类，不在 M1-1.4 范围）"`。

**交叉验证（两表同源不同批次）**：

- `DWD_PURCHASE_ORDER_LINE` 的 200,561 张订单 **99.98%** 落在 `ODS_ERP_PORDERQ` 内；
- 反向：`ODS_ERP_PORDERQ` 的 227,909 张订单中 **87.99%** 在 `DWD_PURCHASE_ORDER_LINE` 有行明细；
- `DWD_PURCHASE_ORDER_DTL` 与 `ODS_ERP_PORDERQ` 行数几乎一致
  （1,036,069 vs 1,036,089），是同批数据的两个切面。

**决策**：本需求触发 `ODS_ERP_*` 建类立项 —— **先建本体类 + 绑定，再做场景**。

### 3.2 建本体类 + 绑定（复用既有设施）

**类定义**：

| 项 | 值 |
|---|---|
| `class_name` | `ODS_ERP_PORDERQ` |
| `class_alias` | ERP 采购订单明细 |
| `object_type` | `Transaction` |
| 归属域 | 采购域（`PROCUREMENT`） |

**源绑定行**（`procurement_source_binding`）：

| 列 | 值 |
|---|---|
| `class_id` | 新建类的 id |
| `datasource_id` | 1（THBI Oracle） |
| `schema_name` | `THBI` |
| `source_table` | `ODS_ERP_PORDERQ` |
| `binding_role` | `BASE` |
| `is_candidate` | `false`（**必须已确认**，候选不得进读路径） |
| `confirmed_by` | 确认人（与既有 9 条一致用 `m1-b-confirm` 或本次确认人） |
| `valid_from` | 建类时刻；`valid_to` = NULL（生效中） |

**属性 + 属性绑定**（`ontology_property` + `procurement_property_binding`）：

| 本体属性名 | 物理列 | 类型 | key_ordinal | 用途 |
|---|---|---|---|---|
| `PO_NO` | `POHNUM_0` | STRING | 1 | 订单号（主键 1/2） |
| `PO_LINE_NO` | `POPLIN_0` | INT | 2 | 订单行号（主键 2/2） |
| `ORDER_DATE` | `ORDDAT_0` | DATETIME | | 下单日期（周期锚定） |
| `SUPPLIER_CODE` | `BPSNUM_0` | STRING | | 供应商编码（过滤 + 关联字典） |
| `MATERIAL_CODE` | `ITMREF_0` | STRING | | 物料编码（关联字典） |
| `ORDER_QTY` | `QTYSTU_0` | DECIMAL | | 订购量（库存单位） |
| `RECEIVED_QTY` | `RCPQTYSTU_0` | DECIMAL | | 已收量（库存单位） |
| `PROMISED_RECEIPT_DATE` | `EXTRCPDAT_0` | DATETIME | | 预期到货日期 |
| `LAST_RECEIPT_DATE` | `LASRCPDAT_0` | DATETIME | | 最后到货日期 |
| `RETURNED_QTY` | `RETQTYSTU_0` | DECIMAL | | 退货量 |

**建类 + 建绑定约束（防重演 2026-11-18 事故）**：

1. 类、属性、源绑定、属性绑定**在同一次 seed 中同步登记**（不留半截状态）；
2. 绑定行必须 `is_candidate=false` 且有 `confirmed_by`/`confirmed_time`
   （DB CHECK 已钉：`候选 ↔ 无确认人`）；
3. 建类后跑**真实数据门禁**（对照
   `procurement_class_source_table_realdata.py` 的形态）：干净库跑迁移链 ⇒
   类存在、绑定指向真实存在的表、10 个属性的物理列全部存在、`seed_ontology.seed()` 不撞唯一约束；
4. 探针已验证 `THBI.ODS_ERP_PORDERQ` 150 列，10 个目标列全部存在且非空率 100%
   （`ORDER_DATE` 空 0、`QTYSTU_0`/`RCPQTYSTU_0`/`BPSNUM_0`/`ITMREF_0` 全非空）。

### 3.2.1 新增 Business Object（用户 2026-11-25 决定：要增加）

新建 BO 承载该类：

| 项 | 值 |
|---|---|
| `code` | `POL_ERP`（既有 `POL` 已被 `DWD_PURCHASE_ORDER_LINE` 占用，见下） |
| `name` | ERP 采购订单行 |
| `header_class_id` | 新建的 `ODS_ERP_PORDERQ` 类 id |
| `primary_domain_id` | `PROCUREMENT` |
| `graph_label` | `ODS_ERP_PORDERQ` |

**为什么是新 BO 而不是改指既有 `POL`**：

- 既有 `POL`（采购订单行）的 `header_class_id` 指向 `DWD_PURCHASE_ORDER_LINE`
  —— 属性齐全但数据停在 2026-08-08；
- 新 BO 指向新鲜全量类，语义是「ERP 抽取的采购订单行」；
- `uq_business_object_header_class` 唯一索引保证**一个类只作一个 BO 的头类**，
  新建 BO 不影响既有 `POL`；
- 两个 BO 并存会造成同名不同数据 ⇒ **名称必须区分**（`POL_ERP` vs `POL`），
  且卡片/答案里须标明数据层。

**⚠ 待业务确认**：BO `code` 上限 `String(20)`，且 `ck_business_object_code_upper` 要求大写。
`POL_ERP` 合规，但**该 BO 面向业务用户是否需要独立编码**，或应直接给既有 `POL`
换绑到新类（会让旧数据的引用失效）—— 属业务口径，需业务方拍板（见第 11 节待确认项）。

**为什么不复用 `DWD_PURCHASE_ORDER_LINE` 的属性定义**：该类的语义名与物理列同名
（数仓 DWD 表已语义化），而 `ODS_ERP_PORDERQ` 的物理列是 ERP 原始名
（`QTYSTU_0` 等）。**建新类时把语义名与原始列显式绑定**，正是本体层吸收跨源差异的机制。

### 3.3 工具如何用本体取数

工具 SQL 模板**只写属性名**，运行时经**绑定编译**解析为物理表列：

```
SELECT
  <PO_NO>, <PO_LINE_NO>, <SUPPLIER_CODE>, <MATERIAL_CODE>,
  <ORDER_QTY>, <RECEIVED_QTY>, <PROMISED_RECEIPT_DATE>, <LAST_RECEIPT_DATE>
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

用户原则：「每个智能体都确定唯一的一个数据源就好了」。

- `DELIVERY_TRACKING_AGENT` 绑定的数据源 = 源绑定行的 `datasource_id`
  （当前为 `1` THBI Oracle + `schema_name=THBI`）；
- 工具执行时按绑定解析出的 `datasource_id`/`schema_name`/`source_table` 取数，
  **不在代码里写 `ZJTH.` / `THBI.` 前缀**；
- 场景的 `datasourceId` 槽位（admin 可切）**保留**，但切换后必须校验
  该数据源内存在该类的**生效绑定** —— 不存在则 fail loud（`ValidationError`），
  **不静默回退到默认源**。

**跨数据源的行为**（回应用户「没有同一智能体跨数据源的能力，除非都是配置化的」）：

- 本方案**不**支持一个 Agent 同时跨多个数据源取数；
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

**决策**：到货判定基于订单行级量（`ORDER_QTY` / `RECEIVED_QTY`），不依赖到货明细表；
到货明细仅用于展示实际到货日期（`LAST_RECEIPT_DATE`，实测已收行 100% 非空）。

### 3.8 待到货场景一并迁到本体（用户 2026-11-25 决定）

`pending_delivery_query` 目前**硬编码** `ZJTH.PORDERQ` / `ZJTH.ITMMASTER` /
`ZJTH.FACILITY` / `ZJTH.BPSUPPLIER` 与 ERP 原始列名（`QTYSTU_0` / `RCPQTYSTU_0` /
`EXTRCPDAT_0` / `BPSNUM_0` / `ITMREF_0` / `POHNUM_0` / `POPLIN_0` / `QTYUOM_0` …），
且查的是 **ERP 源层 `ZJTH`**（近 12 月 84,049 行），与到货跟踪的数仓层（192,467 行）不是同一批数据。

**迁移前必须先确认的事实缺口**（这是本节要向你确认的第一件事）：

| 待到货需要的对象 | dev 本体（Lineage B） | 测试库（Lineage A） | 现状 |
|---|---|---|---|
| 采购订单行 `ZJTH.PORDERQ` | ❌ 无该类 | ✅ `PurchaseOrderDetail` → `PORDERQ`（**但属性 0 条**） | **两套血统都不可直接用** |
| 物料 `ZJTH.ITMMASTER` | ✅ `DIM_IMATERIAL`（数仓层） | ✅ `ItemMaster` → `ITMMASTER`（属性 0 条） | dev 可用但**不同层** |
| 供应商 `ZJTH.BPSUPPLIER` | ✅ `DIM_SUPPLIER`（数仓层） | ✅ `Supplier` → `BPSUPPLIER`（属性 0 条） | dev 可用但**不同层** |
| 基地 `ZJTH.FACILITY` | ❌ 无该类 | ❌ 无 | **两套都缺** |

**关键结论**：待到货要迁到本体，**必须先补齐本体类**——
`ZJTH` 源层的采购订单行 / 物料 / 供应商 / 基地 四个对象，在两套本体血统里都不完整。

**迁移方案（两条路，需你选）**：

**方案 A：待到货也切到数仓层（与到货跟踪同源）**
- 复用本次新建的 `ODS_ERP_PORDERQ` 类 + `DIM_IMATERIAL` / `DIM_SUPPLIER`；
- 需补一个数仓层基地类（`THBI.DIM_FACILITY` 实测存在，本体未建类）；
- **优点**：两个场景同源同口径，数据覆盖面一致（192k vs 84k 行），
  用户在两个场景看到的数据能对得上；工具实现可共用绑定解析件；
- **代价**：待到货的现有实测结果会变（数据更多、结果更全），
  需重跑本地验收与口径基线。

**方案 B：保留 ERP 源层，为 `ZJTH` 源层补建本体类**
- 新建 `ZJTH.PORDERQ` / `ZJTH.ITMMASTER` / `ZJTH.BPSUPPLIER` / `ZJTH.FACILITY` 四个类
  + 绑定（`datasource_id=1`, `schema_name=ZJTH`）；
- **优点**：待到货行为不变，回归面最小；
- **代价**：与到货跟踪**跨源不同口径**，两个场景数字对不上；
  且要建 4 个类（属性可从 Lineage A 的 `seed_ontology.py` 拿到，
  但 Lineage A 的属性在测试库里是 0 条，需从仓内 `seed_ontology.py` 重新落库）。

**我的建议**：**方案 A**。理由是待到货与到货跟踪本来就是同一业务域的姊妹场景，
用户会在同一个页面来回看；若一个查数仓层、一个查源层，数字不一致会直接引发
「到底是哪个对」的信任危机。且方案 A 只需多建 1 个基地类，方案 B 要建 4 个。

**⚠ 需你确认**：方案 A / B 选哪个（见第 11 节）。
另需注意：两个场景若都走本体，**它们的「物料/供应商」字典来源要统一**
（本次决策：名称走 MDM `entity_mapping`）。

## 4. 口径定义（探针实测结论）

### 4.1 到货判定：行级量，不看订单状态

```
行级欠交量  OPEN_QTY = <ORDER_QTY> - <RECEIVED_QTY>     -- 同为库存单位
订单三态：
  全到货    SUM(OPEN_QTY) = 0
  部分到货  有欠交 且 有已收（<RECEIVED_QTY> > 0）
  未到货    所有行 <RECEIVED_QTY> = 0
```

（`<属性名>` = 本体属性，由本体解析为物理列，见 3.3。）

**为什么不用订单头 `RCPLINNBR_0 >= LINNBR_0`**：头行两种判定在近 12 月仅差 4 单、本期差 0 单，
但头表另有 11,817 张「已关闭（CLEFLG_0=2）却仍有欠交」的订单，且其欠交总量级异常
（4.5e18），把闭单当已完结会**假绿**。行级量判定与既有 `pending_delivery_query` 同源，
两个场景可对照。

### 4.2 核心指标：订单到货率为主，量级率为参考

| 指标 | 角色 | 定义 | 本期实测 |
|---|---|---|---|
| **订单到货率** | **主指标** | 全到货订单数 / 总订单数 | **82.25%**（3,984 / 4,844） |
| 量级加权到货率 | 参考 | `SUM(<RECEIVED_QTY>) / SUM(<ORDER_QTY>)` | 40.51% |
| 订单三态 | 辅指标 | 全到货 / 部分到货 / 未到货 订单计数 | 3,984 / 448 / 412 |

**为什么量级率不能当主指标（探针实证，重要）**：

本期 19,910 行中，「完全未到货」行（`<RECEIVED_QTY> = 0`）有 2,251 行，但它们贡献了
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
剔除大额行后期率收敛到 **80.04%**（阈 1 万），与订单到货率 82.25% 仅差 2.2pp
⇒ 两者本应一致，差异纯粹来自少数超大额未到货行的**权重放大**。

**决策**：
- 主指标用**订单到货率**（订单个数口径，不受大单权重影响）；
- 量级率作为**参考值展示，并附口径说明**（「量级加权，受超大额未到货行影响」）；
- **不做**「剔除大额行」的处理 —— 那会删掉真实业务大单，分子分母都不含它们，
  业务不会接受；
- 脏数据（欠交 ≥ 1e6）仍按既有机制剔除并回报 `excludedRows`（本期 6 行），
  但**须说明**：剔除量级行不改变率值结论（剔前后订单率 82.25% vs 82.28%）。

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

**注意**：剔除欠交脏行**不改变**到货率结论（本期订单率 82.25% → 剔后 82.28%），
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
| `ODS_ERP_PORDERQ` 本体类 + 绑定 | 本体资产 | 新增 seed（类 + 属性 + `procurement_source_binding` + `procurement_property_binding`） | 行级到货数据的本体登记；工具取数的唯一锚点 |
| `POL_ERP` Business Object | 本体资产 | 同上 | 承载新类的 BO（用户 2026-11-25 决定：要增加） |
| 绑定解析 | 服务（**复用**） | `backend/app/services/semantic_compiler_bindings.py` | 类+数据源 → 物理表/列；候选隔离；fail-closed |
| `delivery_tracking_query` | 工具 | `backend/app/services/agent_tools_procurement.py` + `seed_agent_tool_configs.py` | 概览 + 双层明细取数（**经绑定解析物理表列**） |
| `DELIVERY_TRACKING_AGENT` | Agent | `backend/scripts/seed_agents.py` + `seed_procurement_agent_profiles.py` | 编排与输出纪律；**绑定唯一数据源** |
| `delivery_tracking` | 场景配置 | `backend/scripts/seed_copilot_scenarios.py` | 卡片墙 / 槽位 / 模板 / 渲染器绑定 |
| `DeliveryTrackingResultCard` | 前端组件 | `frontend/src/components/copilot/` | 结果卡（概览 + 双 Tab） |
| `pending_delivery` 渲染器扩展 | 前端 | `PendingDeliveryResultTabs.tsx` | 分发到新卡 |
| **待到货迁移** | 改造 | `agent_tools_procurement.py`（`pending_delivery_query`） | 去硬编码，改走本体绑定（方案待定，见 3.8） |
| 真实数据门禁 | 脚本 | `backend/scripts/procurement_class_source_table_realdata.py` 同款 | 建类后证明绑定生效（防重演 2026-11-18 事故） |

### 5.2 为什么不复用 `pending_delivery_query`

两者语义正交：

- 待到货：**欠交视角**（只关心没到的），输出催货建议；
- 到货跟踪：**到货视角**（关心整体到货率与已到/未到分布），输出到货达成情况。

共用部分通过抽公共件实现，不共用 SQL 模板：
`_PendingDialect`（方言碎片）、`PendingDeliveryFilters`（过滤/窗口）的思路可提取，
但窗口语义不同（到货跟踪要 24 日锚点），故新写。

**⚠ 数据源不一致须显式说明**：既有 `pending_delivery_query` 走 ERP 源层
（`ZJTH.PORDERQ`，源表名硬编码），本场景走数仓层本体类。
两者数据覆盖面不同（源层近 12 月 84,049 行 vs 数仓层 192,467 行），
界面上须标注数据层以免误读。**是否把待到货也迁到本体/数仓层**不在本需求范围，
但应作为后续独立立项（本需求不擅自改动已上线场景）。

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
  datasourceId: int?    # 数据源（admin 槽位）
  limit: int            # 订单明细上限，默认 50
)
```

**返回结构**（camelCase，与 `PendingDeliveryResult` 风格一致）。
下列数值为 2026-11-25 探针在**本期窗口（2026-10-24 ~ 2026-11-24）**的实测值：

```jsonc
{
  "via": "business_db",
  "datasourceId": 1,
  "dataLayer": "ods_erp",
  "window": { "dateFrom": "2026-10-24", "dateTo": "2026-11-24", "mode": "anchorDay" },
  "summary": {
    "totalLines": 19910,
    "totalOrders": 4844,
    "orderedQty": 98495267.255,
    "receivedQty": 39901874.255,
    "orderArrivalRate": 82.25,    // 主指标：全到货订单 / 总订单 = 3984/4844
    "qtyArrivalRate": 40.51,      // 参考：量级加权 = 39901874/98495267（受超大额未到货行影响）
    "allArrivedOrders": 3984,
    "partialArrivedOrders": 448,
    "notArrivedOrders": 412,
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
**行级到货率只有 40.5%**，远低于订单到货率 82.25%。原因：单行订购量存在百万级脏值
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
    { "key": "materialKeys", "type": "entityMulti", "label": "物料",
      "conflictKeywords": ["物料", "材料"] },
    { "key": "orderKeys", "type": "entityMulti", "label": "订单",
      "conflictKeywords": ["订单", "订单号"] },
    { "key": "datasourceId", "type": "number", "label": "数据源",
      "conflictKeywords": ["数据源"], "hidden": true }
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
        columns: { ORDER_QTY: "QTYSTU_0", RECEIVED_QTY: "RCPQTYSTU_0", ... } }
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
│  订单到货率 82.25%   ← 主指标                              │
│  [全到货 3,984] [部分到货 448] [未到货 412]                 │
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
| 全到货 / 部分 / 未到 | 3,984 / 448 / 412 |
| 订单到货率 | 82.25% |
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
| **建类引入本体绑定漂移**（2026-11-18 已发生过一次） | 建类走既有治理流程 + 真实数据门禁脚本；类/属性/绑定同步登记 |
| 本体类被治理侧删除/改绑，工具随之失效 | 解析 fail-closed + 明确报错（不静默换表） |
| 待到货迁移后**现有实测结果变化** | 迁移前后跑同一组基线并显式对比；口径基线随迁移更新（不静默漂移） |
| 两个场景若不同源，用户发现数字对不上 | 方案 A 统一到数仓层（推荐）；卡片标注数据层 |
| 数仓层供应商码与 MDM 码域未来漂移 | 解析统一走 MDM；过滤前校验码在 MDM 域内 |
| 订单明细行数膨胀 | `limit` 上限 50（沿用 `_MAX_LIMIT`），概览层不受影响 |
| 24 日锚点与「自然月」两套窗口并存易混 | 卡上显示实际 `window.dateFrom/dateTo`，不只显示口径名 |
| 量级率被超大额行主导，用户误读 | 主指标用订单率；量级率降为参考并附口径说明 |

## 11. 待业务确认项

| # | 问题 | 背景 | 我的建议 |
|---|---|---|---|
| 1 | **待到货迁移方案 A（切数仓层）还是 B（保留源层、补建 4 个类）？** | 见 3.8。方案 A 需多建 1 个基地类但两场景同源；方案 B 需建 4 个类且两场景数字对不上 | **方案 A** |
| 2 | 新 BO 编码用 `POL_ERP` 可以吗？ | 既有 `POL` 已被 `DWD_PURCHASE_ORDER_LINE` 占用；新 BO 指向新鲜类，名称须区分 | `POL_ERP`（或业务给定的其它码） |
| 3 | 两个场景的字典来源统一为 MDM `entity_mapping` 可以吗？ | 数仓 `ODS_ERP_BPSUPPLIER` 覆盖率 100% 但要额外建类；`DIM_SUPPLIER` 覆盖率 99.97% | 用 MDM（免建类、跨场景一致） |
| 4 | **数仓层基地类 `THBI.DIM_FACILITY` 需要建类吗？** | 待到货的 `siteKeys` 过滤用到基地字典；方案 A 需要它 | 方案 A 下需建（实测 `THBI.DIM_FACILITY` 存在） |
| 5 | 「到货跟踪」是否也需要基地维度？ | 需求只提时间/供应商/订单/物料 | 本期不加（YAGNI），可后补 |
| 6 | 到货明细（`ODS_ERP_YPRECEIPTD`）要不要接入？ | 它有到货单号与批次，但 `BPSNUM_0` 全空、覆盖仅 41% 订单 | 本期不接（够用即可）；需要批次追溯时再立项 |

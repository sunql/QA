# 源侧能力实测（SRM + THBI）

> 日期：2026-11-18 ｜ 分支 `feat/procurement-m0` ｜ 未部署
> 证据：[`../evidence/source-capability-scan.txt`](../evidence/source-capability-scan.txt)（296 行，只读实测快照）
> 复跑：`cd backend && uv run python scripts/probe_procurement_source_capability.py --out ../docs/update/1118/M0/evidence/source-capability-scan.txt`
> 纪律：全部语句是 `SELECT` 聚合；走 `BusinessDbAdapter.execute_read_only`（SQL Guard 只读路径）；口令由系统 Fernet 解密，不打印、不落盘、不进 argv。

---

## 0. 这份文件为什么存在

问卷回答是**业务口径的权威来源**，但它描述的是**业务看到的现象**，不是**数据里的事实**。
两者不一致时，以实测为准，并把差异登记到 [`conflicts-and-degradations.md`](./conflicts-and-degradations.md)。

本轮实测推翻或修正了问卷中 **6 处**判断（§4），其中 2 处直接改变了 KPI 的可算性结论。
另发现 **2 处本体侧结构性事实**（§1.3 两套命名族、§1.4 关联表覆盖率），
它们不改问卷答案，而是改**绑定清单的范围** —— 影响面比 6 处更大。

---

## 1. THBI Oracle（datasource 1，schema `THBI`，库 `X3V71ORA`）

### 1.1 行数与关键列填充率

| 物理表 | 行数 | 关键列实测 |
|---|---:|---|
| `DWD_PURCHASE_ORDER_LINE` | 927,648 | `PROMISED_RECEIPT_DATE` 填 **927,646（99.9998%）**；`REQUESTED_RECEIPT_DATE` 填 **0**；`ORDER_QTY` / `RECEIVED_QTY` / `UNIT_PRICE` / `LINE_AMOUNT_EXCL_TAX` 在 2025 起 196,753 行上 100% 填充 |
| `DWD_PURCHASE_ORDER`（头） | — | 18 列，**无 `REQUISITION_NO`**；有 `VALIDATION_STATUS` / `TOTAL_AMOUNT_EXCL_TAX` |
| `DWD_GOODS_RECEIPT_LINE` | 3,558,008 | 收货日期 2016-06-30 → 2026-08-08；2025 起 1,189,303 行，其中 **955,035（80.3%）** 有 `PO_NO`+`PO_LINE_NO`；`REJECTED_QTY` 100% 填充但**合计恒为 0**；`RETURNED_QTY` 合计 5,458,549.792（**带小数**） |
| `DWD_ARRIVAL_ORDER_DTL` | 81,754 | `ARR_QTY` / `QUALIFIED_QTY` / `INSPECT_DATE` / `ARR_DATE` 100% 填充；不良行（`QUALIFIED_QTY < ARR_QTY`）**1,300**；去重 PO 行键 **33,840**（2025 起：18,342 行 / 7,848 键） |
| `DWD_PURCHASE_INVOICE_LINE` | 2,486,311 | `RECEIPT_NO` 填 **2,474,258（99.55%）**、`RECEIPT_LINE_NO` / `RECEIPT_NO` 齐备 ⇒ 三单匹配接缝存在；**无 `INVOICE_DATE`**（ORA-00904，该列在发票**头**表） |
| `DWD_PURCHASE_INVOICE`（头） | — | 有 `INVOICE_DATE` / `AMOUNT_EXCL_TAX` / `STATUS` / `SUPPLIER_NAME` |
| `DWD_SUPPLIER_PAYMENT` | 33,576 | `DUE_DATE` / `VALUE_DATE` / `BILL_DATE` / `CREATION_DATE` 均 100% 填充；**`BANK_DATE` 填充 0**；`DUE_DATE − BILL_DATE ∈ [−146, +7]` 天；`VALUE_DATE > BILL_DATE` 仅 **6** 笔 |
| `DWD_PURCHASE_REQUISITION_LINE` | 181,201 | `CREATION_DATE` / `LINE_AMOUNT_EXCL_TAX` 100% 填充；有 `REQUISITION_NO` / `ORDERED_FLAG` / `EXPECTED_ORDER_DATE`；**无任何 PO 关联列** |
| `DWD_SUPPLIER_PRICE_LIST` | 126,508 | **`MATERIAL_CODE` 填充 0（全部 NULL）**；`UNIT_PRICE` / `VALID_FROM` / `VALID_TO` 100% 填充；仅 5 个 `PRICE_LIST_CODE` |
| `DWD_PURCHASE_QUOTATION_YEARLY_DTL` | 167,909 | `ITEM_CODE` 21,679 个 / `SUPPLIER_CODE` 570 个 / `PRICE_LIST_CODE` 4 个；`SEG_YEAR` 覆盖 2015–2027；`UNIT_PRICE_EX_TAX`（不含税）/ `SEG_START_DATE` / `SEG_END_DATE` 在 2025–2026 的 73,191 行上 100% 填充 |
| `DWD_QUOTATION` / `DWD_QUOTATION_LINE` | 0 / 0 | **空表**（⇒ PO↔请购 无法经报价单中转） |
| `DWD_PURCHASE_CONTRACT` | — | **不存在（ORA-00942）** |
| `DWD_RECONCILIATION_BILL_DTL` | 24,868 | 对账明细（D1.6 明确排除，范围外对象） |
| `DWD_QUOTATION_LINE` | 0 | 空表 |
| `DIM_SUPPLIER` | 3,500 | 列名全是 SAP 混淆码（`BPSNAM_0` / `ABCCLS_0` / …），**没有任何供应商状态/停用列** |

### 1.2 口径可算性实测

**① A1.1「订单行完成度」三分档可直接算**（2025 起 196,753 订单行）

| 档 | 行数 | 占比 |
|---|---:|---:|
| 全完成（`RECEIVED_QTY >= ORDER_QTY`） | 177,137 | 90.0% |
| 部分完成 | 9,244 | 4.7% |
| 未完成（`RECEIVED_QTY` 为 0） | 10,372 | 5.3% |

⇒ 问卷 A1.1 描述的「部分完成 / 全部未完成均为未完成」在数据上是一个**真实存在的三档分布**，
不是理论情形。两种读法（订单级完成 vs 订单行级完成）都**技术可算** ⇒ 该歧义不构成降级风险，只需回问确认。

**② 基准价覆盖率（成本节约 / 价格偏差的致命数字）**

| 关联键 | 2025 起订单行命中 | 覆盖率 |
|---|---:|---:|
| `SUPPLIER_CODE` + `MATERIAL_CODE`（→ 年度报价 `ITEM_CODE`） | 81,020 / 196,753 | **41.2%** |
| 仅 `MATERIAL_CODE` | 91,485 / 196,753 | 46.5% |

⇒ **近六成订单行没有基准价**。配合 A7.5「基准价缺失的行按 0」，
成本节约额会被**系统性低估约 59%**。这是必须由业务拍板的口径风险（见冲突登记 C-06）。

**③ 到货检验支持「首次检验」判定**

`DWD_ARRIVAL_ORDER_DTL` 2025 起 18,342 行对应 7,848 个去重 PO 行键 ⇒ **平均 2.34 次检验/PO 行** ⇒
A10.1「首次检验即合格」可用「同一 `PO_NO#PO_LINE_NO` 下 `ARR_DATE` 最早的一条」判定（候选实现已具备数据基础）。

**④ 付款日期列的现实**

| 候选列 | 填充率 | 说明 |
|---|---:|---|
| `BANK_DATE` | **0%** | 银行日期列存在但从未落数 |
| `VALUE_DATE` | 100% | 付款日 |
| `BILL_DATE` | 100% | 单据日；与 `VALUE_DATE` 相差 > 0 的仅 6 笔 |
| `CREATION_DATE` | 100% | 付款单创建日 |

⇒ **「付款申请日」（A8.1）在数仓里没有独立列**。最贴近的代理是 `BILL_DATE`（单据日）。
且 `DUE_DATE − BILL_DATE ∈ [−146, +7]`，说明到期日是**账期**而非 A8.2 所说的「收货后固定天数」。

---

## 1.3 两套命名族：34 个候选 ≠ KPI 读路径（本轮最重要的发现）

本体里同时存在**两套并行的采购对象建模**，它们指向**不同的物理表**：

| 指标公式实际引用的类（**7 个墓碑类**） | 物理表 | 行数 | 物理列数 = 本体属性数 | 关键列 |
|---|---|---:|:-:|---|
| `DWD_PURCHASE_ORDER_LINE`（id20，被 **6** 条指标引用） | `DWD_PURCHASE_ORDER_LINE` | 927,648 | 29 = 29 ✅ | `PROMISED_RECEIPT_DATE` ✅ `ORDER_QTY` ✅ `LINE_AMOUNT_EXCL_TAX` ✅ `MATERIAL_CODE` ✅ |
| `DWD_GOODS_RECEIPT_LINE`（id14，4 条） | `DWD_GOODS_RECEIPT_LINE` | 3,558,008 | 26 = 26 ✅ | `RECEIPT_DATE` ✅ `REJECTED_QTY` ✅ `RECEIPT_NO` ✅ |
| `DWD_PURCHASE_INVOICE_LINE`（id17，1 条） | `DWD_PURCHASE_INVOICE_LINE` | 2,486,311 | 23 = 23 ✅ | `RECEIPT_NO` ✅ `UNIT_PRICE_NET` ✅ |
| `DWD_PURCHASE_INVOICE`（id16，1 条） | `DWD_PURCHASE_INVOICE` | — | 17 = 17 ✅ | `INVOICE_DATE` ✅ |
| `DWD_SUPPLIER_PAYMENT`（id27，1 条） | `DWD_SUPPLIER_PAYMENT` | 33,576 | 17 = 17 ✅ | `BILL_DATE` ✅ `VALUE_DATE` ✅ `DUE_DATE` ✅ `BANK_DATE` ✅（填充 0） |
| `DWD_PURCHASE_REQUISITION_LINE`（id22，1 条） | `DWD_PURCHASE_REQUISITION_LINE` | 181,201 | 23 = 23 ✅（missing=0） | `REQUISITION_NO` ✅ `CREATION_DATE` ✅ `QUOTATION_NO` **100% NULL** |
| `DWD_REQUISITION_ORDER_LINK`（id25，1 条） | `DWD_REQUISITION_ORDER_LINK` | **138,118** | 6 = 6 ✅（missing=0） | `PO_NO`/`PO_LINE_NO`/`REQUISITION_NO`/`REQUISITION_LINE_NO` **100% 填充** |

对照：**A8 的 34 个候选**用的是另一套 `_DTL` 命名：

| A8 候选类 | 物理表 | 行数 | 关键列是否齐 |
|---|---|---:|---|
| `DWD_PURCHASE_ORDER_DTL` | `DWD_PURCHASE_ORDER_DTL` | **1,023,442** | ❌ 无 `PROMISED_RECEIPT_DATE` / `ORDER_QTY` / `LINE_AMOUNT_EXCL_TAX` / `MATERIAL_CODE` |
| `DWD_GOODS_RECEIPT_DTL` | `DWD_GOODS_RECEIPT_DTL` | **3,977,714** | ❌ 无 `RECEIPT_DATE` / `REJECTED_QTY` / `RECEIPT_NO` |
| `DWD_AP_INVOICE_DTL` | `DWD_AP_INVOICE_DTL` | **46** | ❌ 无 `PO_NO` / `RECEIPT_NO`（且几乎是空表） |
| `DWD_ARRIVAL_ORDER_DTL` | `DWD_ARRIVAL_ORDER_DTL` | 81,754 | ✅ `ARR_QTY` / `QUALIFIED_QTY` / `ARR_DATE` / `INSPECT_DATE`（**与墓碑族同表**） |

**结论（有重大影响）**：

1. **34 个候选单独不足以支撑 12 条 KPI** —— 它们缺少 OTD / 三单匹配 / 付款 / 发票所需的关键列。
   绑定清单必须是 **34 候选 + 7 个复活墓碑类 = 41 个类**。
2. **7 个墓碑类的物理表与本体属性列完全对得上**（列数 = 属性数，抽样 `missing = 0`）⇒ 复活**没有列级缺口**，
   A8 §3.1 担心的「复活成本高」不成立。
3. `_DTL` 族与墓碑族来自两条不同的落地链路（行数也不同：1,023,442 vs 927,648 / 3,977,714 vs 3,558,008），
   **不能互相替代**。A8 §3.2「一个源表挂两个类」只覆盖了 `DWD_SUPPLIER_PRICE_LIST` 一处，
   实际上**至少 3 处**（`_DTL` 与 `_LINE` 同名不同表）。
4. ODS → DWD 的对应关系可确认：`ODS_PORDERQ` = **927,648** = `DWD_PURCHASE_ORDER_LINE`；
   `ODS_PINVOICED` = **2,486,311** = `DWD_PURCHASE_INVOICE_LINE` ⇒ 墓碑族是 ODS 的正常 DWD 落地层。

### 1.4 PO ↔ 请购：有精确关联表，但只覆盖 13.8%

`DWD_REQUISITION_ORDER_LINK`（138,118 行，6 个键列 100% 填充）**就是** PO↔请购的关联表：

| 指标 | 实测 |
|---|---:|
| 关联表行数 | 138,118 |
| 关联表覆盖的 PO 号（2025+ 订单中的） | 8,096 / 54,554（**14.8%**） |
| 2025+ 订单行能精确接上请购单的行数 | **27,159 / 196,753（13.8%）** |
| 经 `QUOTATION_NO` 中转的匹配 | **0**（请购行 `QUOTATION_NO` 100% NULL；`DWD_QUOTATION`/`_LINE` 是空表） |

⇒ **A3.1「起点=采购申请」不是不可算**：起点可以**精确**算（请购创建日 → 收货日），
但**覆盖率只有 13.8%** ⇒ 首期口径应是「**只统计可精确匹配的行，并同屏输出覆盖率**」，
而不是问卷原设想，也不是「按 (供应商, 物料, 时间窗) 近似」（近似匹配反而更不可信）。

---

## 2. SRM-TH-01（datasource 10，MySQL，库 `srm_company_taihong`）

| 表 | 行数 | 关键事实 |
|---|---:|---|
| `supply_access`（供应商准入） | 436 | `access_state` 值域 {51004: 394, 51003: 18, 51001: 15, 51002: 9}；`supply_state` {55002: 417, 55001: 19} ⇒ **码值含义未确认**，不能直接当「黑名单」 |
| `supply_access_qualification`（资质） | 6 | 过期 1 条；30 天内到期 **0** 条 |
| `supply_access_bank`（银行信息） | 437 | `bank_name` 为空 **323** 条 |
| `supply_access_class`（品类准入） | **0** | 空表 |
| `main_data_supplier`（供应商主数据） | 470 | `state` 全部恒为 `100001` ⇒ **没有「停用」标志** |
| `supply_inspection` / `supply_inspection_factors`（来料检验） | **0 / 0** | 空表 |
| `order_purchase_order_info`（采购订单行） | 261,194 | `required_delivery_date` 填 **1,688（0.65%）** |
| `pricing_quote_form_info`（报价明细） | 1,352 | `unit_price` 填 **1** 条 ⇒ 不能当基准价源 |
| `order_warehousing_order_info`（入库单行） | 33,419 | `plan_qty` 100% 填充 |
| `order_contract`（合同） | **0** | 空表 |
| `performance_assessment`（绩效） | 1,131 | 有数据 |
| `supply_return_order`（退货） | **0** | 空表 |

---

## 3. 可算性裁决表（12 条 KPI）

| KPI | 结论 | 决定性依据 |
|---|---|---|
| `KPI_SUPPLIER_OTD` | ✅ 可算 | `PROMISED_RECEIPT_DATE` 99.9998% 填充；完成度三档可判 |
| `KPI_SUPPLIER_OVERDUE_RATIO` | ✅ 可算 | 同上（与 OTD 同分母，A2.3） |
| `KPI_SUPPLIER_DELAY_DAYS` | ⚠ 可算但口径待定 | A4.1 / A4.2 / A4.3 三问互斥（见 C-03） |
| `KPI_PURCHASE_CYCLE_TIME` | ⚠ **起点可精确算，但只覆盖 13.8%** | 关联表 `DWD_REQUISITION_ORDER_LINK` 存在（138,118 行）⇒ 起点可精确；27,159 / 196,753 行可接上请购单（经报价单中转则 0 匹配） |
| `KPI_PURCHASE_PRICE_VARIANCE` | ⚠ **基准价覆盖 41%** | 基准价源须改用 `DWD_PURCHASE_QUOTATION_YEARLY_DTL`；`DWD_SUPPLIER_PRICE_LIST.MATERIAL_CODE` 全空 |
| `KPI_COST_SAVING` | ⚠ 同上 | 同 A5/A7，另加 A7.5「缺失按 0」放大低估风险 |
| `KPI_INVOICE_MATCH_RATE` | ✅ 可算 | 发票行 `RECEIPT_NO`/`RECEIPT_LINE_NO` 99.55% 齐备；`INVOICE_DATE` 在发票头表（需头行 JOIN） |
| `KPI_PAYMENT_ON_TIME` | ⚠ 判定日无独立列 | `BANK_DATE` 0% ⇒ 用 `BILL_DATE` 代理；A8.2「收货后固定天数」与实测账期不符 |
| `KPI_SUPPLIER_DEFECT_RATE` | ✅ **原判「不可算」推翻** | `DWD_ARRIVAL_ORDER_DTL` 81,754 行，`ARR_QTY`/`QUALIFIED_QTY` 100% 填充 ⇒ 分子分母都有列 |
| `KPI_SUPPLIER_FPY` | ⚠ 可判但单位待定 | 多次检验存在（2.34 次/PO 行）⇒「首次」可判；但 A10.2 行数 vs A10.3 补集单位不一致 |
| `KPI_SUPPLIER_MAINTENANCE_RATIO` | ⚠ 判定项缺列 | 品类准入表为空；「未停用」列不存在；`access_state` 码值未确认 |
| `KPI_SUPPLIER_NCR_RATE` | ❌ 摘除首期 | A11.1–A11.4「质量数据未在系统」；SRM 无 NCR 表（`supply_return_order` 亦为 0） |

---

## 4. 问卷断言的实测修正（6 处）

| # | 问卷断言 | 实测 | 处置 |
|---|---|---|---|
| 1 | A2.1「承诺交货日期的值目前为空」 | **对 THBI 不成立**：`PROMISED_RECEIPT_DATE` 填充 99.9998%。成立的是 **SRM**：`required_delivery_date` 仅 0.65% | 业务描述的是 SRM 侧或另一视图。THBI 侧 OTD/逾期率/延误天数**可算**，不必降级；改为**回问「您指的是哪张表/哪个报表」** |
| 2 | A7 §4.5/§4.6「基准价 = 价目表 `DWD_SUPPLIER_PRICE_LIST`」 | 该表 **`MATERIAL_CODE` 100% NULL**，无法按物料关联 | 基准价源改为 `DWD_PURCHASE_QUOTATION_YEARLY_DTL`（`ITEM_CODE` + `UNIT_PRICE_EX_TAX` + 生效区间，字段完整） |
| 3 | C4.3「来料检验 IQC：没有」 | **只对 SRM 空表成立**；THBI `DWD_ARRIVAL_ORDER_DTL` 有 81,754 行真实检验数据 | A9/A10 从「源类不存在」改判为「源类已存在，需确认字段语义」 |
| 4 | C4.4「NCR：没有」 | 成立（SRM 无 NCR 表；`supply_return_order` 0 行） | A11 维持摘除首期 |
| 5 | A6/A7 §4.9「本体没有来料检验类」 | 本体确实无该类，但**物理表存在且有数据** | 不必新建虚构类，按 A8 流程为 `DWD_ARRIVAL_ORDER_DTL` 建类并绑定（它已在 A8 的 34 个候选内） |
| 6 | C4.1「采购合同：没有」 | 成立，且 `DWD_PURCHASE_CONTRACT` 在 THBI **不存在**（ORA-00942）⇒ 治理层引用的是一个不存在的表 | 治理层字段须改写（属 A6 已判定的「治理层需改」） |

### 附：与 A8 候选表的一致性

A8 的 34 个候选全部由 `ontology_class.source_table ⨝ schema_cache` 解析得出，`notFound = 0`，
本轮实测**未发现候选表不存在**。注意 `DWD_SUPPLIER_PRICE_LIST_DEL` 是**本体类名**，
其物理表是 `DWD_SUPPLIER_PRICE_LIST`（A8 §4 已注明「物理表名可 ≠ class_name」）——不是幻觉引用，
但它正是修正 #2 里那个「物料编码全空」的表。

---

## 5. 给 M1（源绑定）的直接结论

1. **绑定清单 = 34 候选 + 7 个复活墓碑类 = 41 个类**，不是 34 个。
   34 候选里的 `_DTL` 族**缺 OTD / 三单 / 付款 / 发票所需的关键列**（§1.3），
   单绑 34 个会让 9 条 KPI 的读路径断在库级。
2. **34 个候选全部可绑定**（`notFound = 0`），无需因「表不存在」剔除任何候选 ——
   但**也不能只绑这 34 个**。
3. **7 个墓碑类的复活没有列级缺口**（物理列数 = 属性数，抽样 `missing = 0`）⇒ A8 §3.1 的
   「复活成本高」顾虑不成立；复活是**必需项**，不是可选项。
4. **必须新增的类**：目前只有 `DWD_ARRIVAL_ORDER_DTL` 已登记（在 34 个候选内）⇒ A9/A10 的源类**不必新建**，
   但 A9/A10 需要的 `ARR_QTY` / `QUALIFIED_QTY` / `INSPECT_DATE` / `DOC_STATUS_CODE` 属性要补进本体。
5. **SRM 侧 6 张空表**（`supply_access_class` / `supply_inspection` / `supply_inspection_factors` /
   `order_contract` / `supply_return_order` / `supply_access_qualification` 仅 6 行）⇒
   「表存在」不等于「有数据」。绑定可以建，但**必须标注数据规模**，否则系统会查到一张空表
   （这正是 C3.1 问卷要问、而本次实测可以直接回答的问题）。
6. **`DOC_STATUS_CODE` 码值未确认**（{3: 81,487 / 1: 228 / 2: 39}）⇒ 在 ERP 管理员确认码义前，
   到货检验相关绑定**不得启用状态过滤**，否则会把 267 行（0.33%）漏掉或错筛。
7. **采购周期起点有精确路径**（`DWD_REQUISITION_ORDER_LINK`），但只覆盖 13.8% 的订单行（§1.4）
   ⇒ 首期「只统计可精确匹配的行 + 输出覆盖率」，**不做近似匹配**。

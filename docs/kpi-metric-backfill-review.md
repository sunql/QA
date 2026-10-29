# KPI 指标技术形态 —— 业务方审核清单

回填脚本产出的是**候选技术形态**，不是结论。
每条指标都是 `DRAFT`；发布闸门要求 `metric.status = PUBLISHED` ⇒
**业务方核对本清单后，在本体指标页手工发布**，KPI 才能发布。

## 脚本不做的事（必须人工补）

- **不绑定数据源**：`kpi_catalog.datasource_id` 保持原值。
- **不改 KPI 发布状态**：脚本只写 `metric_id` 与结构化列。
- **不代业务确认口径**：下方每条的「需确认的偏差」逐条核对。
- **不改治理列**：`num/denominator_columns` 写的是**业务原声明**。
  层 2 对账因此判 **mismatch**，这是**预期**（防「脚本自己给自己签字」）。

## 落库后的连带影响（上线前必读）

回填不只是写数据，它会**改变 NL2SQL 的运行时行为**：

- 脚本会把 `kpi_catalog.metric_id` 挂到本体指标上。
- Tier B 契约注入（`backend/app/services/kpi_contract_builder.py`）要求 KPI
  挂到本体指标才会注入契约块 ⇒ **`metric_id` 一落库，契约注入就有了注入对象**。
- 而 `KPI_CONTRACT_INJECTION_ENABLED` 开关在本分支**默认 true**
  （`backend/app/services/kpi_metric_switches.py`；`system_config` 无该行时
  `readKpiMetricSwitch(session, SWITCH_CONTRACT_INJECTION, True)` 返回 `True`）
  ⇒ **回填落库后，契约注入默认生效，不需要额外翻开关**。
- 对比：`KPI_METRIC_GATE_ENABLED`（发布闸门）默认 **false** ⇒ 闸门「先观察再阻断」；
  注入则是「默认开」。**两者灰度口径不一致，上线前要单独决策。**

**因此业务方/运维在 `--apply` 之前必须二选一（写进清单，不要口头传）：**

1. **先关后开**：先把 `KPI_CONTRACT_INJECTION_ENABLED` 显式置为 `false`，
   回填落库后观察一轮，再按需置 `true`；
2. **接受默认生效**：直接回填，契约注入随之启用（NL2SQL plan prompt 会多出
   「【KPI 口径契约】」块）。

> 把注入显式置 false 的写法（**控制器上线决策，不由脚本默认执行**）：
> 在 `system_config` 写 `config_key='KPI_CONTRACT_INJECTION_ENABLED'`、
> `config_value='false'`（按 `seed_system_config.py` 的 upsert 口径）。

## `--overwrite` 的破坏性语义（默认关闭，FIX-I1）

- **默认（不带 `--overwrite`）**：对已存在的指标 / 来源 / KPI 治理列**只填空值**，
  已有非空值一律不覆盖；也**不删除**任何 source 行 ⇒ 人工修订过的公式、owner、
  治理列不会被脚本回退。
- **`--overwrite`**：全量重写 + 删除 plan 之外的 source 行。
  **若目标指标当前是 `PUBLISHED`，写完后会被回退为 `DRAFT`，必须重新发布并
  重新过发布闸门**（否则契约注入口径会与指标本体不一致）。

## 正式库备份（T15-B，不可跳过）

`--apply` 到 `qa_metadata`（prod 同源）时脚本会**自己先跑 pg_dump**：
`/tmp/<table>_<YYYYMMDD>_<alembic_revision>.sql`（`ontology_metric` / `ontology_metric_source` / `kpi_catalog`），口令走 `PGPASSWORD` 环境变量、不进 argv。
备份先写 `.tmp` 再原子替换；产物不存在或为空 ⇒ 脚本**拒绝写入**。

## 逐条核对

### KPI_SUPPLIER_OTD —— KPI_SUPPLIER_OTD 供应商准时交付率
- 档位：A
- 落库动作：新建指标
- 技术粒度键：['SUPPLIER_CODE', 'FACILITY_CODE']
- 治理粒度（业务原声明）：['供应商', '工厂', '月']
- 治理分子列（业务原声明）：['ACTUAL_RECEIPT_DATE']
- 治理分母列（业务原声明）：['PROMISED_DATE']
- 结构化分子（物理列）：{'agg': 'COUNT', 'expr': 'RECEIPT_DATE', 'filter': 'RECEIPT_DATE <= PROMISED_RECEIPT_DATE', 'class_alias': 'DWD_GOODS_RECEIPT_LINE'}
- 结构化分母（物理列）：{'agg': 'COUNT', 'expr': 'PROMISED_RECEIPT_DATE', 'class_alias': 'DWD_PURCHASE_ORDER_LINE'}
- 可执行形态 / 口径声明：`SELECT COUNT(CASE WHEN R.RECEIPT_DATE <= O.PROMISED_RECEIPT_DATE THEN 1 END) / NULLIF(COUNT(O.PROMISED_RECEIPT_DATE), 0) FROM THBI.DWD_PURCHASE_ORDER_LINE O JOIN THBI.DWD_GOODS_RECEIPT_LINE R ON O.PO_NO = R.PO_NO AND O.PO_LINE_NO = R.PO_LINE_NO WHERE O.PROMISED_RECEIPT_DATE IS NOT NULL`
- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）
- **需确认的偏差（治理列 ↔ 物理列）**：
  - 治理列 ACTUAL_RECEIPT_DATE ↔ 物理列 THBI.DWD_GOODS_RECEIPT_LINE.RECEIPT_DATE
  - 治理列 PROMISED_DATE ↔ 物理列 THBI.DWD_PURCHASE_ORDER_LINE.PROMISED_RECEIPT_DATE
  - 口径粒度「月」未落成列（SQL 侧按日期列截断到月）
  - 分子条件同时比较承诺日（PROMISED_RECEIPT_DATE 属订单行表，已落在分母度量）

### KPI_SUPPLIER_OVERDUE_RATIO —— KPI_SUPPLIER_OVERDUE_RATIO 供应商逾期率
- 档位：A
- 落库动作：新建指标
- 技术粒度键：['SUPPLIER_CODE', 'FACILITY_CODE']
- 治理粒度（业务原声明）：['供应商', '工厂', '月']
- 治理分子列（业务原声明）：['ACTUAL_RECEIPT_DATE']
- 治理分母列（业务原声明）：['PROMISED_DATE']
- 结构化分子（物理列）：{'agg': 'COUNT', 'expr': 'RECEIPT_DATE', 'filter': 'RECEIPT_DATE > PROMISED_RECEIPT_DATE', 'class_alias': 'DWD_GOODS_RECEIPT_LINE'}
- 结构化分母（物理列）：{'agg': 'COUNT', 'expr': 'PROMISED_RECEIPT_DATE', 'class_alias': 'DWD_PURCHASE_ORDER_LINE'}
- 可执行形态 / 口径声明：`SELECT COUNT(CASE WHEN R.RECEIPT_DATE > O.PROMISED_RECEIPT_DATE THEN 1 END) / NULLIF(COUNT(O.PROMISED_RECEIPT_DATE), 0) FROM THBI.DWD_PURCHASE_ORDER_LINE O JOIN THBI.DWD_GOODS_RECEIPT_LINE R ON O.PO_NO = R.PO_NO AND O.PO_LINE_NO = R.PO_LINE_NO WHERE O.PROMISED_RECEIPT_DATE IS NOT NULL`
- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）
- **需确认的偏差（治理列 ↔ 物理列）**：
  - 治理列 ACTUAL_RECEIPT_DATE ↔ 物理列 THBI.DWD_GOODS_RECEIPT_LINE.RECEIPT_DATE
  - 治理列 PROMISED_DATE ↔ 物理列 THBI.DWD_PURCHASE_ORDER_LINE.PROMISED_RECEIPT_DATE
  - 口径粒度「月」未落成列（SQL 侧按日期列截断到月）

### KPI_PAYMENT_ON_TIME —— KPI_PAYMENT_ON_TIME 付款及时率
- 档位：A
- 落库动作：新建指标
- 技术粒度键：['SUPPLIER_CODE']
- 治理粒度（业务原声明）：['供应商', '月']
- 治理分子列（业务原声明）：['PAYMENT_DATE']
- 治理分母列（业务原声明）：['DUE_DATE']
- 结构化分子（物理列）：{'agg': 'COUNT', 'expr': 'VALUE_DATE', 'filter': 'VALUE_DATE <= DUE_DATE', 'class_alias': 'DWD_SUPPLIER_PAYMENT'}
- 结构化分母（物理列）：{'agg': 'COUNT', 'expr': 'DUE_DATE', 'class_alias': 'DWD_SUPPLIER_PAYMENT'}
- 可执行形态 / 口径声明：`SELECT COUNT(CASE WHEN VALUE_DATE <= DUE_DATE THEN 1 END) / NULLIF(COUNT(DUE_DATE), 0) FROM THBI.DWD_SUPPLIER_PAYMENT`
- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）
- **需确认的偏差（治理列 ↔ 物理列）**：
  - 治理列 PAYMENT_DATE ↔ 物理列 THBI.DWD_SUPPLIER_PAYMENT.VALUE_DATE（银行付款日）
  - KPI 的 data_source 写 DWD_PAYMENT，物理表实际是 DWD_SUPPLIER_PAYMENT
  - 口径粒度「月」未落成列（SQL 侧按日期列截断到月）

### KPI_INVOICE_MATCH_RATE —— KPI_INVOICE_MATCH_RATE 发票三单匹配率
- 档位：A
- 落库动作：新建指标
- 技术粒度键：['SUPPLIER_CODE', 'INVOICE_DATE']
- 治理粒度（业务原声明）：['供应商', '月']
- 治理分子列（业务原声明）：['PO', 'QTY', 'PRICE']
- 治理分母列（业务原声明）：[]
- 结构化分子（物理列）：{'agg': 'SUM', 'expr': 'LINE_AMOUNT_EXCL_TAX', 'filter': 'PO_NO = O.PO_NO AND PO_LINE_NO = O.PO_LINE_NO AND QUANTITY = O.ORDER_QTY AND UNIT_PRICE_NET = O.UNIT_PRICE', 'class_alias': 'DWD_PURCHASE_INVOICE_LINE'}
- 结构化分母（物理列）：{'agg': 'SUM', 'expr': 'AMOUNT_EXCL_TAX', 'class_alias': 'DWD_PURCHASE_INVOICE'}
- 可执行形态 / 口径声明：`SELECT SUM(CASE WHEN IL.PO_NO = O.PO_NO AND IL.PO_LINE_NO = O.PO_LINE_NO AND IL.QUANTITY = O.ORDER_QTY AND IL.UNIT_PRICE_NET = O.UNIT_PRICE THEN IL.LINE_AMOUNT_EXCL_TAX ELSE 0 END) / NULLIF(SUM(I.AMOUNT_EXCL_TAX), 0) FROM THBI.DWD_PURCHASE_INVOICE I JOIN THBI.DWD_PURCHASE_INVOICE_LINE IL ON IL.INVOICE_TYPE = I.INVOICE_TYPE AND IL.INVOICE_NO = I.INVOICE_NO JOIN THBI.DWD_PURCHASE_ORDER_LINE O ON O.PO_NO = IL.PO_NO AND O.PO_LINE_NO = IL.PO_LINE_NO`
- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）
- **需确认的偏差（治理列 ↔ 物理列）**：
  - 治理列 PO ↔ 物理列 THBI.DWD_PURCHASE_INVOICE_LINE.PO_NO
  - 治理列 QTY ↔ 物理列 THBI.DWD_PURCHASE_INVOICE_LINE.QUANTITY
  - 治理列 PRICE ↔ 物理列 THBI.DWD_PURCHASE_INVOICE_LINE.UNIT_PRICE_NET
  - 治理分子「发票金额」未点名列 ↔ 物理列 THBI.DWD_PURCHASE_INVOICE_LINE.LINE_AMOUNT_EXCL_TAX
  - 治理分母「总发票金额」未点名列 ↔ 物理列 THBI.DWD_PURCHASE_INVOICE.AMOUNT_EXCL_TAX
  - 「三单」按 PO 号 + 行号 + 数量 + 单价 四要素实现（口径未定义「单」的确切含义）

### KPI_PURCHASE_CYCLE_TIME —— KPI_PURCHASE_CYCLE_TIME 采购周期
- 档位：B
- 落库动作：新建指标
- 技术粒度键：['SUPPLIER_CODE', 'FACILITY_CODE']
- 治理粒度（业务原声明）：['供应商', '工厂', '月']
- 治理分子列（业务原声明）：['GOODS_RECEIPT_DATE', 'PR_CREATED_DATE']
- 治理分母列（业务原声明）：[]
- 结构化分子（物理列）：（无结构化度量）
- 结构化分母（物理列）：（无结构化度量）
- 可执行形态 / 口径声明：`SELECT AVG(TRUNC(R.RECEIPT_DATE) - TRUNC(L.CREATION_DATE)) FROM THBI.DWD_REQUISITION_ORDER_LINK K JOIN THBI.DWD_PURCHASE_REQUISITION_LINE L ON K.REQUISITION_NO = L.REQUISITION_NO AND K.REQUISITION_LINE_NO = L.REQUISITION_LINE_NO JOIN THBI.DWD_GOODS_RECEIPT_LINE R ON K.PO_NO = R.PO_NO AND K.PO_LINE_NO = R.PO_LINE_NO`
- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）
- **需确认的偏差（治理列 ↔ 物理列）**：
  - 口径是差值（收货日 - 请购单创建日），结构化词表表达不了 ⇒ 不给结构化度量
  - 治理列 GOODS_RECEIPT_DATE ↔ 物理列 THBI.DWD_GOODS_RECEIPT_LINE.RECEIPT_DATE
  - 治理列 PR_CREATED_DATE ↔ 物理列 THBI.DWD_PURCHASE_REQUISITION_LINE.CREATION_DATE
  - 口径表名 DWD_PURCHASE_REQUISITION 物理上拆成 _LINE + DWD_REQUISITION_ORDER_LINK

### KPI_SUPPLIER_DELAY_DAYS —— KPI_SUPPLIER_DELAY_DAYS 供应商平均延误天数
- 档位：B
- 落库动作：新建指标
- 技术粒度键：['SUPPLIER_CODE', 'FACILITY_CODE']
- 治理粒度（业务原声明）：['供应商', '工厂', '月']
- 治理分子列（业务原声明）：['ACTUAL_RECEIPT_DATE', 'PROMISED_DATE']
- 治理分母列（业务原声明）：[]
- 结构化分子（物理列）：（无结构化度量）
- 结构化分母（物理列）：（无结构化度量）
- 可执行形态 / 口径声明：`SELECT AVG(TRUNC(R.RECEIPT_DATE) - TRUNC(O.PROMISED_RECEIPT_DATE)) FROM THBI.DWD_PURCHASE_ORDER_LINE O JOIN THBI.DWD_GOODS_RECEIPT_LINE R ON O.PO_NO = R.PO_NO AND O.PO_LINE_NO = R.PO_LINE_NO WHERE R.RECEIPT_DATE > O.PROMISED_RECEIPT_DATE`
- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）
- **需确认的偏差（治理列 ↔ 物理列）**：
  - 口径是差值且带「仅逾期」过滤 ⇒ 结构化词表表达不了
  - 治理列 ACTUAL_RECEIPT_DATE ↔ 物理列 THBI.DWD_GOODS_RECEIPT_LINE.RECEIPT_DATE
  - 治理列 PROMISED_DATE ↔ 物理列 THBI.DWD_PURCHASE_ORDER_LINE.PROMISED_RECEIPT_DATE

### KPI_COST_SAVING —— KPI_COST_SAVING 采购成本节约
- 档位：B
- 落库动作：新建指标
- 技术粒度键：['SUPPLIER_CODE', 'MATERIAL_CODE']
- 治理粒度（业务原声明）：['供应商', '物料', '月']
- 治理分子列（业务原声明）：['STANDARD_PRICE', 'ACTUAL_PRICE']
- 治理分母列（业务原声明）：[]
- 结构化分子（物理列）：（无结构化度量）
- 结构化分母（物理列）：（无结构化度量）
- 可执行形态 / 口径声明：`SELECT SUM((P.UNIT_PRICE - O.UNIT_PRICE) * O.ORDER_QTY) FROM THBI.DWD_PURCHASE_ORDER_LINE O JOIN THBI.DWD_SUPPLIER_PRICE_LIST P ON P.MATERIAL_CODE = O.SUPPLIER_MATERIAL_CODE AND P.VALID_FROM <= O.ORDER_DATE AND (P.VALID_TO IS NULL OR P.VALID_TO >= O.ORDER_DATE)`
- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）
- **需确认的偏差（治理列 ↔ 物理列）**：
  - 口径写 STANDARD_PRICE / ACTUAL_PRICE，物理列是 THBI.DWD_SUPPLIER_PRICE_LIST.UNIT_PRICE / THBI.DWD_PURCHASE_ORDER_LINE.UNIT_PRICE
  - 口径写 QTY，物理列是 THBI.DWD_PURCHASE_ORDER_LINE.ORDER_QTY
  - 标准价与实际价的关联口径（按物料 + 有效期 + 价目表优先级）需业务确认

### KPI_PURCHASE_PRICE_VARIANCE —— KPI_PURCHASE_PRICE_VARIANCE 采购价格偏差率
- 档位：B
- 落库动作：新建指标
- 技术粒度键：['MATERIAL_CODE', 'FACILITY_CODE']
- 治理粒度（业务原声明）：['物料', '工厂', '月']
- 治理分子列（业务原声明）：['ACTUAL_PRICE', 'STANDARD_PRICE']
- 治理分母列（业务原声明）：['STANDARD_PRICE']
- 结构化分子（物理列）：（无结构化度量）
- 结构化分母（物理列）：（无结构化度量）
- 可执行形态 / 口径声明：`SELECT AVG((O.UNIT_PRICE - P.UNIT_PRICE) / NULLIF(P.UNIT_PRICE, 0)) FROM THBI.DWD_PURCHASE_ORDER_LINE O JOIN THBI.DWD_SUPPLIER_PRICE_LIST P ON P.MATERIAL_CODE = O.SUPPLIER_MATERIAL_CODE AND P.VALID_FROM <= O.ORDER_DATE AND (P.VALID_TO IS NULL OR P.VALID_TO >= O.ORDER_DATE)`
- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）
- **需确认的偏差（治理列 ↔ 物理列）**：
  - 口径是比值算术式 ⇒ 结构化词表表达不了
  - 聚合阶取 AVG_OF_RATIOS（先算单行比值再平均）；若业务要「总额比」需改为 WEIGHTED_RATIO

### KPI_SUPPLIER_MAINTENANCE_RATIO —— KPI_SUPPLIER_MAINTENANCE_RATIO 供应商资质合规率
- 档位：B
- 落库动作：新建指标
- 技术粒度键：['SUPPLIER_CODE']
- 治理粒度（业务原声明）：['供应商（快照）']
- 治理分子列（业务原声明）：[]
- 治理分母列（业务原声明）：[]
- 结构化分子（物理列）：（无结构化度量）
- 结构化分母（物理列）：（无结构化度量）
- 可执行形态 / 口径声明：`口径声明：有效期内资质证书齐全的供应商数 / 活跃供应商总数。技术形态待业务补：资质表 supply_quality_certificate 在 srm 数据源，与 THBI 跨库，无法写成单条可执行 SQL ⇒ execution_source = NONE。`
- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）
- **需确认的偏差（治理列 ↔ 物理列）**：
  - 资质表是 SRM 侧 supply_quality_certificate，**不在本体也不在 KPI 的 data_source 里**
  - SUPPLY_ID ↔ THBI.DIM_SUPPLIER.BPSNUM_0 的关联键未确认
  - THBI.DIM_SUPPLIER 的属性是 ERP 原生名（*_0 后缀），与业务名列不一致
  - 跨数据源（THBI Oracle + srm）⇒ 无法写成单条可执行 SQL ⇒ execution_source = NONE

### KPI_SUPPLIER_DEFECT_RATE —— KPI_SUPPLIER_DEFECT_RATE 供应商来料不良率
- 档位：C
- 落库动作：新建指标
- 技术粒度键：['SUPPLIER_CODE', 'MATERIAL_CODE', 'FACILITY_CODE']
- 治理粒度（业务原声明）：['供应商', '物料', '工厂', '月']
- 治理分子列（业务原声明）：['IQCRESULT']
- 治理分母列（业务原声明）：[]
- 结构化分子（物理列）：（无结构化度量）
- 结构化分母（物理列）：（无结构化度量）
- 可执行形态 / 口径声明：`口径声明：检验结果为 FAIL 的检验数量 / 检验总数量。技术形态待业务补：物理源 DWD_INCOMING_INSPECTION 在 schema_cache 里不存在 ⇒ execution_source = NONE。`
- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）
- **需确认的偏差（治理列 ↔ 物理列）**：
  - **物理源缺失**：DWD_INCOMING_INSPECTION 在 schema_cache 里不存在
  - 口径写 IQCRESULT，全库 0 命中；最接近的代理列是 THBI.DWD_GOODS_RECEIPT_LINE.REJECTED_QTY（代理口径需业务确认）

### KPI_SUPPLIER_FPY —— KPI_SUPPLIER_FPY 供应商一次合格率
- 档位：C
- 落库动作：新建指标
- 技术粒度键：['SUPPLIER_CODE', 'MATERIAL_CODE', 'FACILITY_CODE']
- 治理粒度（业务原声明）：['供应商', '物料', '工厂', '月']
- 治理分子列（业务原声明）：['INSPECTION_RETRY_COUNT', 'IQCRESULT']
- 治理分母列（业务原声明）：[]
- 结构化分子（物理列）：（无结构化度量）
- 结构化分母（物理列）：（无结构化度量）
- 可执行形态 / 口径声明：`口径声明：首次检验即通过且无复检的批数 / 检验总批数。技术形态待业务补：物理源 DWD_INCOMING_INSPECTION 不存在 ⇒ execution_source = NONE。`
- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）
- **需确认的偏差（治理列 ↔ 物理列）**：
  - **物理源缺失**：DWD_INCOMING_INSPECTION 不存在
  - 口径写 IQCRESULT / INSPECTION_RETRY_COUNT，全库 0 命中

### KPI_SUPPLIER_NCR_RATE —— KPI_SUPPLIER_NCR_RATE 供应商 NCR 率
- 档位：C
- 落库动作：新建指标
- 技术粒度键：['SUPPLIER_CODE', 'MATERIAL_CODE', 'FACILITY_CODE']
- 治理粒度（业务原声明）：['供应商', '物料', '工厂', '月']
- 治理分子列（业务原声明）：[]
- 治理分母列（业务原声明）：[]
- 结构化分子（物理列）：（无结构化度量）
- 结构化分母（物理列）：（无结构化度量）
- 可执行形态 / 口径声明：`口径声明：触发 NCR 的检验批数 / 检验总批数。技术形态待业务补：物理源 DWD_INCOMING_INSPECTION 与 DWD_NCR 都不存在 ⇒ execution_source = NONE。`
- [ ] 公式已按 **Oracle 方言**人工复核（`TRUNC(a)-TRUNC(b)`、`THBI.` 前缀、双键 join）
- **需确认的偏差（治理列 ↔ 物理列）**：
  - **物理源缺失**：DWD_INCOMING_INSPECTION 与 DWD_NCR 都不存在
  - 「NCR 数 / 检验批数」的去重口径需业务确认

## 汇总

- A 档（4 条）
- B 档（5 条）
- C 档（3 条）
- 新建本体指标：12 条
- 匹配到已有指标：0 条

# 变更：feat-dw-layering-thbi

- **日期**：2026-08-30
- **Phase**：数仓分层（docs/data-knowledge P0-3 补齐）
- **状态**：✅ implemented (2026-08-30，建仓 + 校验全过)

## 建仓执行记录（2026-08-30）

全量执行约 5 分钟（ODS 3 分钟 + DWD 1.5 分钟 + DIM/DWS/ADS 0.5 分钟）。

**执行中的修正**（已回写 SQL 脚本）：

| 问题 | 修正 |
|---|---|
| ORA-12899：中文列实际字节数超 VARCHAR2 预估（如 BPSNAM_0 实测 72 字节 > 预估 60） | name/desc 类列按 LENGTHB 实测 + 余量放宽（supplier_name 90、description_1 130 等） |
| ORA-00932：ITMFACILIT.LOCNUM_0 实为 NUMBER 非 VARCHAR2 | stock_location 类型改 NUMBER，去掉 NULLIF |
| ORA-01722：PAYMENTD.VCRTYP_0 是 VARCHAR2（'CNGEN'/'CNPIH' 等凭证类型代码）非 NUMBER | voucher_type 改 VARCHAR2(10) |
| ORA-01408：(po_no, po_line_no) 已是复合主键，重复索引报错 | 删除 IX_DWD_POL_PO / IX_DWD_GRL_RECEIPT |

**校验结果**（run_build.py --verify + 手工抽查，全部通过）：

1. ✅ ODS 27 表行数对账：全部与 ZJTH 源表一致
2. ✅ DWD 清洗校验：1599 日期残留 0、空串 PO 引用 0
3. ✅ DWS 对账：DELIVERY 明细 2,542,243 = 汇总一致；PURCHASE 927,646 = 汇总一致
4. ✅ OTD 链路：收货行 JOIN PO 行可正确判定 ON_TIME / LATE + delay_days
5. ✅ ADS_SUPPLIER_360 可查：如 C005 台州形美电泳--近 12 月 OTD 77.05%、平均逾期 2.5 天、订单额 133.8 万、13,415 张 PO（2016-2026 十年合作）

**数据观察**（对 AI 应用有意义的初步发现）：

- 近 12 月 OTD 分布：大供应商 60%-78% 居多，个别异常低（Q630 浙江椋誉 2.37%）——已诊断为**系统性数据缺陷**（预期收货日未维护，见 §9），item 3c DQ 规则按用户决策整体延期
- 拒收数量（RRRQTYPUU）稀疏，reject_rate 普遍为 0——供应商质量指标弱，需 IQC 数据源接入
- ORDER_DETAIL 宽表视图可直接回答"某供应商某批逾期几天"类问题

### 补充区分维度 + 金额清洗（2026-08-30 二次执行）

**背景**：本体重建前置要求——本体需区分「物料分类」与「零库存供应商」，DWD/DIM/DWS/ADS 缺该属性。

**改动**（已回写 SQL 脚本）：

| 层 | 表 | 新增字段 | 源 |
|---|---|---|---|
| DWD | DWD_SUPPLIER | `zero_stock_flag NUMBER` | BPSUPPLIER.YPTHFLGM_0（2=零库存，1=非零库存） |
| DWD | DWD_MATERIAL | `material_category VARCHAR2(20)` | ITMMASTER.TCLCOD_0（产品大类，TH1/TJ1/A06E…） |
| DIM | DIM_SUPPLIER / DIM_MATERIAL | 同上透传 | 自 DWD |
| DWS | 4 张供应商月度汇总 | `zero_stock_flag`（LEFT JOIN DWD_SUPPLIER） | — |
| DWS | DWS_MATERIAL_PRICE_MONTHLY | `material_category`（LEFT JOIN DWD_MATERIAL） | — |
| ADS | ADS_SUPPLIER_360 / ADS_SUPPLIER_ORDER_DETAIL | 同上透出 | 自 DIM |

**金额清洗规则**（DWD_PURCHASE_ORDER_LINE）：X3 源 PORDERQ 存在 **19 行 order_qty > 1e9 的污染数据**（如 PO C12009POH0182 qty 达 4.1e17、金额 2.4e18，单价 ~5.8 正常 → qty 被异常放大）；GR 侧全库 MAX 仅 120 万，1e9 为天然分界 → **该行 order_qty / line_amount_excl_tax / line_amount_incl_tax 置 NULL**（unit_price 保留）。

**验证**：
1. ✅ zero_stock_flag：DWD 与 DIM 均为 1→3326 / 2→174，与 ZJTH 源完全一致
2. ✅ material_category：DWD/DIM 落值（TH1 138,754 / TJ1 132,504 / A06E 20,441…）
3. ✅ 5 张 DWS 全部透传，分组可用
4. ✅ 清洗后残留 qty>1e9 = 0；DWS 采购金额恢复正常（全库 624 亿 / 1,191 家供应商）
5. ✅ ADS 两视图可查（零库存供应商 360：如 A119 苏州利来 近12月采购 2.75 亿、OTD 1.63%）

**新业务观察（强区分度，值得 AI 应用利用）**：

- **零库存供应商 OTD 极低**：月均值 3.98% vs 非零库存 50.23%（未按量加权）。零库存（JIT 直送）供应商承诺日期口径与普通供应商不同，或存在系统性延迟——后续可作「按零库存类型分开看 OTD」的语义，否则会误判供应商表现
- 物料类别采购额 Top：A01（86 亿）、C02（18.9 亿）、A06E（15.1 亿）——类别维度可直接支撑品类分析

### MERGE 增量同步（2026-08-30 第三次执行）

**背景**：全量快照的增量补充（§9 已知限制闭环）——按 `UPDDATTIM_0` 水位 MERGE，ODS/DWD 增量、DIM/DWS/ADS 全量重建。

**改动**（已回写代码）：

- 新增 `backend/scripts/dwd_merge.py`：纯函数 MERGE 生成器（解析 dw/*.sql 的 INSERT SELECT → 按水位 MERGE；解析 PK / 源表 / WHERE / 双源 JOIN）
- `dw/run_build.py` 增加 `--incremental` 模式 + `THBI.ETL_WATERMARK` 水位控制表（target_table PK, last_watermark TIMESTAMP(3)）
- 首次增量从 1900-01-01 起全量 MERGE 后推进到源 `MAX(UPDDATTIM_0)`；MERGE 键 = ZJTH 唯一索引（ITMMASTER 无唯一索引 → 手动 `ITMREF_0`）
- DWD 24 张 PK 表 MERGE；3 张无 PK 表（BOM/BOM_DETAIL/ROUTING_OPERATION）reload（DELETE+INSERT）
- 双源 JOIN 表（INVOICE_LINE / SUPPLIER_PAYMENT_LINE）水位 = `GREATEST(d.UPDDATTIM_0, h.UPDDATTIM_0)`（头行变更也触发行重灌）

**验证**（真实 THBI，全部通过）：

1. ✅ ODS 27 表 merged 行数 == ZJTH 源（如 PRECEIPTD 3,558,008、PINVOICED 2,486,311、PORDERQ 927,648）
2. ✅ DWD 27 表计数与全量基线一致（PO_LINE 927,648、GR_LINE 3,558,008、INV_LINE 2,486,311、PAY_LINE 44,493）
3. ✅ 校验和一致：DWD_PURCHASE_ORDER_LINE `SUM(ORDER_QTY)=4,373,106,735.863` == 全量基线
4. ✅ 增量语义：回拨水位 2026-01-01 → 重跑只 merged 89,307 行（2026-01 后变更行），水位自动推进回 2026-08-10；幂等（追赶重灌后计数/校验和不变）
5. ✅ `--verify`：ODS 对账 27/27 OK、清洗校验 0 残留、OTD 抽样正常
6. ✅ 单测 14/14 GREEN（`backend/app/tests/unit/test_dwd_merge.py`）

**code-review 修复**（code-reviewer 发现后已修 + 回归测试）：

- **HIGH**：`buildWatermarkQuery` 丢 WHERE → 5 张业务过滤表（PRICE_LIST_HEADER / PURCHASE_INVOICE[+LINE] / SUPPLIER_PAYMENT[+LINE]）水位被不合格行抬高，后续增量会静默漏数。已修：水位查询与 MERGE 同谓词（保留 `info.where`）。
- **MEDIUM**：双源 `GREATEST(d.UPDDATTIM_0, h.UPDDATTIM_0)` 遇 NULL 返回 NULL → 一侧 UPDDATTIM_0 为 NULL 的行被水位过滤掉。已修：`COALESCE(..., TIMESTAMP '1900-01-01 00:00:00')`。修复后实测 DWD_PURCHASE_INVOICE_LINE 收回 6 行（MATCHED 更新，计数/校验和不变）。

**已知限制**：MERGE 只处理插入/更新，不处理源表物理删除的行（X3 多以状态字段软删，影响可控）；DWD 无 PK 3 表仍全量 reload（表小）；水位在 MERGE 后采集（标准高水位，源同步窗口内并发写有竞争，手工批量可接受）。

## 1. 需求

把 ZJTH（Sage X3 ERP 业务库，1371 张表）中**本体管理已映射的 27 张表**（采购域业务数据 + 主数据，共 1016 万行）复制抽取到新建 Oracle 用户 **THBI** 下，按数仓分层架构组织：

```
ZJTH 原始表 --> ODS --> DWD --> DIM --> DWS --> ADS
```

**验收标准**：

1. THBI 下建成 ODS（27 表全字段贴源）+ DWD（25 表精选核心字段，英文 snake_case）+ DIM（5 张维度）+ DWS（5 张月度汇总）+ ADS（Supplier 360° 视图）
2. 从 DWD 开始按英文命名定义数据标准（ODS 保留 X3 原列名贴源）
3. 数据清洗：X3 空日期占位符 `1599-12-31` -> NULL、空串 `' '` -> NULL
4. OTD（准时交付率）指标数据链路可直接计算：承诺交期（PORDERQ.EXTRCPDAT）+ 实际收货日期（PRECEIPTD.RCPDAT）
5. 建仓后：qa-system 注册 THBI 为数据源 + 重建本体指向 DWD/DIM 表（**后续独立 change，本 change 只做数据**）

## 2. 设计评审

| 方案 | 优点 | 缺点 |
|---|---|---|
| **A. THBI 独立用户 + CTAS 一次性建仓**（采用） | 与源库物理隔离；THBI 有 DBA 角色可直接读 ZJTH；CTAS 在 Oracle 服务端执行，1000 万行速度快 | 首版为全量快照，无增量 CDC |
| B. 在 ZJTH 内建视图分层 | 零数据复制 | 违反「AI 禁访业务源表」的访问策略；无法独立授权 |
| C. 抽取到 PostgreSQL（qa_metadata 同实例） | 与 qa-system 元数据库同构 | 失去与 X3 源同库的便利；数据搬迁双跳 |

**用户决策记录**（2026-08-30）：
- DWD 字段策略：**精选核心字段**（每表 15-30 列，键/日期/数量/金额/状态），符合采购域.md §4.3 规范
- 建设范围：**核心链到 ADS**——27 表全部 ODS+DWD；主数据建 DIM；核心采购链（供应商/物料/PO/收货/发票/付款）建 DWS+ADS Supplier 360°；BOM/工艺路线/报价等止步 DWD

## 3. 数据模型变更（THBI schema 内）

### 3.1 ODS 层（27 表，`ODS_<原表名>`）

全字段贴源 CTAS 复制 + `ETL_LOAD_TS`（TIMESTAMP），保留 X3 原列名（含 `_0` 后缀）。

### 3.2 DWD 层（25 表，`DWD_<实体>`，英文 snake_case）

| 源表 | DWD 表 | 行数 | 核心字段（示例） |
|---|---|---|---|
| BPSUPPLIER | DWD_SUPPLIER | 3,500 | supplier_code / supplier_name / payment_code / **zero_stock_flag**（YPTHFLGM_0，2=零库存） |
| BPARTNER | DWD_BUSINESS_PARTNER | 5,923 | partner_code / partner_name / vat_number |
| BPCUSTOMER | DWD_CUSTOMER | 708 | customer_code / customer_name / status |
| BPCARRIER | DWD_CARRIER | 0 | carrier_code / carrier_name |
| ITMMASTER | DWD_MATERIAL | 350,922 | material_code / description_1 / status / item_category / **material_category**（TCLCOD_0，产品大类） |
| FACILITY | DWD_FACILITY | 41 | facility_code / facility_name |
| ITMFACILIT | DWD_ITEM_FACILITY | 818,347 | material_code / facility_code / lot_qty |
| BOM / BOMD | DWD_BOM / DWD_BOM_DETAIL | 1.95万 / 5.17万 | material_code / component_code / component_qty |
| ROUOPE | DWD_ROUTING_OPERATION | 50,869 | material_code / operation_no / subcontractor |
| PQUOTAT / PQUOTATD | DWD_QUOTATION / DWD_QUOTATION_LINE | 0 / 0 | quotation_no / material_code / qty |
| PPRICLIST | DWD_SUPPLIER_PRICE_LIST | 126,508 | material_code / unit_price / valid_from/to |
| PORDER | DWD_PURCHASE_ORDER | 200,562 | po_no / supplier_code / order_date / total_amount |
| PORDERQ | DWD_PURCHASE_ORDER_LINE | 927,648 | po_no+line / **promised_receipt_date** / order_qty / line_amount |
| PREQUISD | DWD_PURCHASE_REQUISITION_LINE | 181,201 | requisition_no / material_code / qty / ordered_flag |
| PREQUISO | DWD_REQUISITION_ORDER_LINK | 138,118 | requisition_no -> po_no / qty |
| YPRECEIPT / YPRECEIPTD | DWD_ARRIVAL_NOTICE / _LINE | 2.7万 / 7.6万 | arrival_notice_no / po_no / planned_qty |
| PRECEIPT | DWD_GOODS_RECEIPT | 970,304 | receipt_no / supplier_code / receipt_date |
| PRECEIPTD | DWD_GOODS_RECEIPT_LINE | 3,558,008 | receipt_no+line / **po_no / po_line_no** / received_qty / **rejected_qty** |
| PINVOICE | DWD_PURCHASE_INVOICE | 38,323 | invoice_no / supplier_code / amount_incl_tax / status |
| PINVOICED | DWD_PURCHASE_INVOICE_LINE | 2,486,311 | invoice_no+line / po_no / receipt_no / material_code |
| PAYMENTH | DWD_SUPPLIER_PAYMENT | 45,891 | payment_no / payment_type / payment_amount / value_date |
| PAYMENTD | DWD_SUPPLIER_PAYMENT_LINE | 68,750 | payment_no+line / voucher_no / line_amount |

**留 ODS 不建 DWD**（2 表）：PPRICCONF / PPRICFICH——价格条件配置表，其价格表头主键关联语义需业务确认后补建。

### 3.3 DIM 层（5 表）

| 维度 | 来源 | 键 |
|---|---|---|
| DIM_DATE | 日历生成（2012-01-01 ~ 2027-12-31，覆盖数据实际范围 2012-12-25 ~ 2026-08-10） | date_key (YYYYMMDD) |
| DIM_SUPPLIER | DWD_SUPPLIER | supplier_key（代理键，序列） |
| DIM_MATERIAL | DWD_MATERIAL | material_key |
| DIM_FACILITY | DWD_FACILITY | facility_key |
| DIM_CURRENCY | 事实表 distinct 货币码 | currency_code |

### 3.4 DWS 层（5 表，粒度 = supplier + facility + year_month）

| 表 | 来源 | 度量 |
|---|---|---|
| DWS_SUPPLIER_PURCHASE_MONTHLY | DWD_PURCHASE_ORDER_LINE | 订单数/行数/数量/金额（不含税） |
| DWS_SUPPLIER_DELIVERY_MONTHLY | GR_LINE JOIN PO_LINE（按 po_no+po_line_no） | 收货行数 / 准时行数 / 逾期行数 / **OTD 率** / 平均逾期天数 |
| DWS_SUPPLIER_QUALITY_MONTHLY | DWD_GOODS_RECEIPT_LINE | 收货数量 / 拒收数量 / 拒收率（QTYPUU 口径） |
| DWS_SUPPLIER_PAYMENT_MONTHLY | DWD_SUPPLIER_PAYMENT | 付款笔数 / 付款金额（按 value_date） |
| DWS_MATERIAL_PRICE_MONTHLY | DWD_GOODS_RECEIPT_LINE | 物料+供应商月均净单价 / 行数（价格趋势） |

### 3.5 ADS 层（1 视图）

`ADS_SUPPLIER_360`（视图）：每供应商一行——近 12 月 OTD 率 / 近 12 月拒收率 / 近 12 月订单金额 / 累计订单行数 / 首末订单日期 / 交易物料数 / 近 12 月收票金额 / 近 12 月付款金额。

## 4. 接口契约变更

无 API 变更（本 change 只动 Oracle THBI，不动 qa-system 代码）。qa-system 接入是后续独立 change（注册 THBI 数据源 + 本体重建）。

## 5. 实现要点

| 文件 | 内容 |
|---|---|
| `dw/01_ods.sql` | 27 条 CTAS（`CREATE TABLE THBI.ODS_x AS SELECT t.*, SYSTIMESTAMP ETL_LOAD_TS FROM ZJTH.x t`） |
| `dw/02_dwd_master.sql` | 12 张主数据/制造 DWD（CREATE + INSERT SELECT 清洗） |
| `dw/03_dwd_facts.sql` | 13 张采购事实 DWD |
| `dw/04_dim.sql` | 5 张维度（DIM_DATE 用 CONNECT BY 生成） |
| `dw/05_dws.sql` | 5 张月度汇总 |
| `dw/06_ads.sql` | ADS_SUPPLIER_360 视图 |
| `dw/run_build.py` | 执行器：连 THBI、按阶段执行 SQL、自动 drop-if-exists、逐语句提交、行数校验日志 |

**清洗规则**（DWD INSERT SELECT 内联）：

```sql
-- X3 空日期占位符 -> NULL
CASE WHEN EXTRCPDAT_0 > DATE '1900-01-01' THEN EXTRCPDAT_0 END
-- 空串 -> NULL（外键引用列）
NULLIF(POHNUM_0, ' ')
```

**OTD 口径**（写入 DWS_SUPPLIER_DELIVERY_MONTHLY）：

```sql
-- 准时 = receipt_date <= promised_receipt_date（收货行关联 PO 行）
-- 粒度 = supplier + facility + year_month（按收货日期归期）
```

**关键语义映射**（X3 -> 标准）：

| 标准 | X3 字段 | 说明 |
|---|---|---|
| po_no / po_line_no | POHNUM_0 / POPLIN_0 | |
| supplier_code | BPSNUM_0（收货/订单）；BPR_0（发票/付款） | 同一编码体系 |
| material_code | ITMREF_0 | |
| company_code / facility_code | CPY_0 / POHFCY_0、PRHFCY_0 | |
| order_date | ORDDAT_0 | |
| promised_receipt_date | PORDERQ.EXTRCPDAT_0（预期收货日） | DEMRCPDAT_0 全为 1599 占位，不可用 |
| requested_receipt_date | PORDERQ.DEMRCPDAT_0（清洗后大多 NULL） | 保留字段但数据质量差 |
| receipt_date | PRECEIPTD.RCPDAT_0 | |
| rejected_qty | PRECEIPTD.RRRQTYPUU_0（拒收数量） | 仅 50 行非零，质量指标数据稀疏 |
| order_qty | QTYUOM_0（订单 UOM） | 注意 received/rejected 为 QTYPUU（价格 UOM），UOM 混用见 §9 |

## 6. 测试

数据工程（非应用代码），验证以「真实数据校验」替代单测（`dw/run_build.py --verify` + 手工抽查 SQL）：

1. **行数对账**：每张 ODS/DWD 与 ZJTH 源表 COUNT 相等（CTAS 无 WHERE 即全量）
2. **清洗校验**：DWD 中不存在 1599 日期、`' '` 空串键
3. **OTD 链路校验**：抽样 10 条 GR_LINE JOIN PO_LINE，人工核对 receipt_date vs promised_date
4. **DWS 汇总校验**：DWS_SUPPLIER_PURCHASE_MONTHLY 汇总额 = DWD_PURCHASE_ORDER_LINE 直查聚合
5. **ADS 视图校验**：抽 3 家供应商，比对 360 值与明细直查结果

TDD/80% 覆盖率门槛不适用于纯 SQL 数据管道（qa-system 代码零改动）；本 change 的质量门禁 = 上述 5 项校验全过。

## 7. 安全审查

- **权限隔离**：THBI 建为独立用户（DBA 角色 + UNLIMITED TABLESPACE），qa-system 后续以 THBI 只读连接 DWD/DIM，不触 ZJTH 源
- **密码管理**：THBI 密码从环境变量 `THBI_PASSWORD` 读取，不写入任何脚本/文档
- **无代码改动**：本 change 不动 qa-system 后端/前端，无攻击面变化
- SQL 均为静态 DDL/DML（无字符串拼接用户输入），无注入面

## 8. 部署验证

```bash
cd dw
THBI_PASSWORD=*** ./.venv/bin/python run_build.py --phase all        # 全量建仓（约 5-15 分钟）
THBI_PASSWORD=*** ./.venv/bin/python run_build.py --phase all --verify  # 校验行数对账

# 抽查（SQL*Plus / sqlcl，以 THBI 登录）
SELECT COUNT(*) FROM ODS_PORDERQ;                 -- 应 = 927,648
SELECT COUNT(*) FROM DWD_PURCHASE_ORDER_LINE
  WHERE promised_receipt_date = DATE '1599-12-31'; -- 应 = 0
SELECT supplier_code, ROUND(on_time_rate,3) avg_otd
  FROM DWS_SUPPLIER_DELIVERY_MONTHLY GROUP BY supplier_code FETCH FIRST 10 ROWS ONLY;
SELECT * FROM ADS_SUPPLIER_360 FETCH FIRST 5 ROWS ONLY;
```

## 9. 已知限制与后续工作

| 事项 | 说明 | 处理 |
|---|---|---|
| 全量快照，无增量 | CTAS 一次性抽取；X3 无 CDC | ✅ 已实现 MERGE 增量（按 UPDDATTIM_0 水位，见顶部执行记录）；scheduled rebuild 待调度器 |
| UOM 混用 | order_qty 为 QTYUOM（订单 UOM），received/rejected 为 QTYPUU（价格 UOM） | DWS 质量率用同表 QTYPUU 口径内部一致；跨表数量对比需注意 |
| 拒收数据稀疏 | RRRQTYPUU 仅 50 行非零（无独立 IQC 表） | 供应商质量指标弱；IQC 系统接入是远期 |
| 价格表头关联缺失 | PPRICFICH/PPRICCONF 与 PPRICLIST 的关联键语义待业务确认 | 暂留 ODS；确认后补 DWD |
| DEMRCPDAT 不可用 | 需求日期全为 1599 占位 | REQUEST_DATE 用 PORDER 行级 EXTRCPDAT；GR 承诺日期用 PO 行级 |
| PORDERQ 数量/金额脏数据 | 19 行 order_qty > 1e9（PO C12009POH0182 等，qty 达 4.1e17），污染金额聚合 | 已加 DWD 清洗规则：qty>1e9 置 NULL；其余 >1亿 金额（≤300亿，qty 正常）按真实大额保留 |
| 零库存供应商 OTD 极低 | 零库存（YPTHFLGM_0=2）月均 OTD 3.98% vs 非零 50.23% | 疑似零库存 JIT 承诺日期口径不同或系统性延迟；AI 语义层需按 zero_stock_flag 分口径，勿混比 |
| 预期收货日未维护 → OTD 失真（Q630 等） | 全局 97.7% PO 行（192,238/196,753）`EXTRCPDAT_0 == ORDDAT_0`（承诺日=下单日，提前期 0）；13.4% 供应商月（4,400/32,815）`received_line_count>=30 AND on_time_rate<0.10`；Q630 浙江椋誉 2.37% 即此系统性缺陷的表象，非单供应商业务异常 | 数据侧不建 DQ 规则（item 3c **整体延期**，2026-08-30 用户决策）；属 ERP 预期收货日管理缺失，待业务侧字段管理上线后再建「OTD 异常」症状级 DQ 规则 |
| DIM 无 SCD2 | 首版 SCD1（全量重建） | 主数据变更频率低，SCD2 待真实需求 |
| qa-system 未接入 | THBI 建仓后需注册数据源 + 本体重建 | ✅ 已完成：feat-dw-ontology-rebind（THBI 默认数据源 + 27 类 rebind DWD + NL2SQL 验证通过） |

## 10. 关联

- 前置：`docs/data-knowledge/采购域.md`（§4.3 DWD 样例 / §13 核心先行建议）、`系统差距评估报告.md`（P0-3）
- 后续：✅ `Harness/changes/feat-dw-ontology-rebind/summary.md`（THBI 注册 + 本体重建 + NL2SQL 切换 DWD，2026-08-30 完成）
- 关联：`Harness/changes/feat-data-lineage-model`（数仓分层落地后血缘表可补 SYSTEM->ODS->DWD->DWS->ADS 全链边）
- 增量：本 change 顶部「MERGE 增量同步」执行记录（`backend/scripts/dwd_merge.py` + `dw/run_build.py --incremental` + `THBI.ETL_WATERMARK`）

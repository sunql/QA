# 业务域

## Demo 数据源

Demo 业务库使用 RuoYi WMS MySQL schema（`/Users/sunql/Prejectcode/Rouyi/sql/`），含仓储管理域表：

| 实体 | 表 |
|------|----|
| 仓库 | `wms_warehouse` |
| 库区 | `wms_zone` |
| 库位 | `wms_location` |
| 物料 | `wms_material` |
| 批次 | `wms_batch` |
| 供应商 | `wms_supplier` |
| 客户 | `wms_customer` |
| 库存 | `wms_inventory` |
| 库存流水 | `wms_inventory_log` |
| 入库单 | `wms_inbound_order` / `_detail` |
| 出库单 | `wms_outbound_order` / `_detail` |
| 分配(FIFO) | `wms_allocation_record` |
| 盘点 | `wms_check_order` / `_detail` |
| 采购申请 | `wms_purchase_requisition` / `_detail` |
| 采购订单 | `wms_purchase_order` / `_detail` |

## 本体映射示例

Phase 2 预置本体：
- 类 `库存` -> `wms_inventory`，属性：仓库、物料、数量。
- 类 `入库单` -> `wms_inbound_order`。
- 指标 `库存总量` = `SUM(wms_inventory.quantity)`。
- 指标 `入库金额` = `SUM(wms_inbound_order_detail.total_amount)`。

## 扩展数据源

支持动态注册（`POST /api/v1/datasource`），可接入 SRM（供应商/采购订单）等其它业务库。

## 采购域业务对象目录（Phase 3.3）

`seed_ontology.py` 现有 27 个类，覆盖采购域核心单据与主数据。Phase 3.3 补齐 3 个缺失业务对象（6 个类），源表与关键属性基于 ZJTH 真实库（`all_tab_columns` 实证）：

| 对象 | 类 | source_table | 关键属性 |
|---|---|---|---|
| QUOT（报价/询价） | `Quotation` / `QuotationDetail` | `PQUOTAT` / `PQUOTATD` | 报价单号、报价日期、响应期限、受邀/响应供应商数；明细：物料、数量、提前期、来源请购行 |
| INV（采购发票） | `PurchaseInvoice` / `PurchaseInvoiceDetail` | `PINVOICE` / `PINVOICED` | 发票号、供应商、含税/不含税金额、到期日、状态；明细：物料、数量、金额、三向匹配关联 |
| PAY（付款） | `Payment` / `PaymentDetail` | `PAYMENTH` / `PAYMENTD` | 付款单号、付款类型、付款金额、付款/到期日期、状态；明细：科目、供应商、被支付凭证 |

### 关键映射与业务流转

- **QUOT 映射修正**：采购域「报价」对应 Sage X3 **采购报价 `PQUOTAT`**（`SQUOTE` 是销售报价——含销售员 `REP_0`、销售订单 `SOHNUM_0`，不属采购域）。`PQUOTAT` 一张单据覆盖「询价 → 报价」两端：表头 `BPSNBR_0`（受邀供应商数）/`RSPNBR_0`（响应供应商数），明细 `PSHNUM_0`/`PSDLIN_0` 引用来源请购单（`PREQUISD`）。当前表内 0 行（结构性建模，供后续数据加载）。
- **INV 三向匹配**：`PINVOICED` 明细经 `POHNUM_0`+`POPLIN_0` → `PORDERQ`（订单）、`PTHNUM_0`+`PTDLIN_0` → `PRECEIPTD`（收货）、`PNHNUM_0`+`PNDLIN_0` → `PAYMENTD`（付款），支撑「订单/收货/发票/付款」四单对账。数据规模：发票 38K 行 / 明细 2.4M 行。
- **PAY 与发票核销**：`PAYMENTD` 的 `VCRNUM_0`/`VCRTYP_0` 为被支付凭证（发票）；发票明细的 `PNHNUM_0`/`PNDLIN_0` 反指付款行。
- **新 KPI**：`KPI_INVOICE_AMT`（发票金额）、`KPI_PAYMENT_AMT`（付款金额）、`KPI_QUOTATION_QTY`（询价数量）。

### 已覆盖 / 已知缺口

| 目录对象 | 状态 | 说明 |
|---|---|---|
| ASN（发运通知） | ✅ 已建模 | = 到货单 `ArrivalNotice`（`YPRECEIPT`），`XSRMID_0` 即 ASN 号 |
| DELIVERY（交付） | ✅ 已覆盖 | 采购交付无独立表，由到货单 `YPRECEIPT`/收货单 `PRECEIPT` 全流程覆盖（`SDELIVERY` 为销售发货，不属采购域） |
| RFQ（询价） | ✅ 并入 QUOT | `PQUOTAT` 即询价/报价单据（与 QUOT 同表两端） |
| CONTRACT（采购合同） | ⚠️ 已知缺口 | ZJTH 无合同主数据表；合同条款可存于 `DocumentCatalog`（Phase 5） |
| NCR（不合格处理） | ⚠️ 已知缺口 | 属 QMS 域，当前业务库无 NCR 表 |
| SUP_PERF（供应商绩效） | ⚠️ 已知缺口 | 为派生指标域（OTD/质量/价格），由 Phase 4 Feature/KPI 计算，无源表 |

NL2SQL 引用新类经 `_validateOntologyAgainstSchema` 校验：`PQUOTAT`/`PINVOICE`/`PAYMENTH` 等表均存在于 ZJTH schema 缓存，无漂移告警。见 [[Supplier 收货模式 YPTHFLGM_0]]、[[nl2sql-engine]]。

## 供应商域（Supplier）

业务表：`BPSUPPLIER`（供应商主档）→ `PORDER`/`PORDERQ`（采购订单及明细）→ `YPRECEIPT`/`YPRECEIPTD`（到货单及明细）→ `PRECEIPT`/`PRECEIPTD`（收货单/入库单及明细）。

### 关键分类字段：`BPSUPPLIER.YPTHFLGM_0`（收货模式）

`BPSUPPLIER.YPTHFLGM_0` 决定整条 SRM → WMS 收料链路的形态，**订单完成率必须按此字段拆分两组口径统计**，混算会同时虚增 / 虚减两侧：

| 取值 | 语义 | 典型代表 |
|------|------|----------|
| `1` | **非零库存供应商** | 通用外购物料 / 长周期供应商 |
| `2` | **零库存供应商** | JIT 直送车间 / 寄售 / VMI 等无独立仓库环节 |

业务别名（`business_aliases`）：「零库存标志」、「到货模式」、「收货管理方式」。见 [[Supplier 收货模式 YPTHFLGM_0]]。

### 两种模式的业务流程

#### 非零库存供应商（`YPTHFLGM_0 = 1`）：到货 → 质检 → 收货

```
采购订单 PORDER/PORDERQ
    │
    ▼ 创建到货单（草稿，供应商发货时填写）
YPRECEIPT / YPRECEIPTD        [状态：草稿 draft]
    │   货物到达工厂，改为「到货」
    ▼
YPRECEIPT                     [状态：到货 arrived]
    │   推质检（QC）
    ▼
    │   质检完毕，输入合格数量 → 单据变为「质检完毕」
YPRECEIPT                     [状态：质检完毕 qualified]
    │   以合格数量为依据，创建收货单
    ▼
PRECEIPT / PRECEIPTD          [状态：收货 receipt]
    │   收货数量 = 合格数量；不合格品另走退货流程
    ▼
入库到仓库（库存 +1）
```

#### 零库存供应商（`YPTHFLGM_0 = 2`）：采购单直通收货，自动出入库

```
采购订单 PORDER/PORDERQ
    │   无独立到货 / 质检环节，订单直接驱动收货单
    ▼
PRECEIPT / PRECEIPTD          [状态：收货 receipt]
    │
    ▼ 推到车间后，由生产领用系统自动执行「入库 → 出库」
    │   物资先虚拟入线边仓（即时生成入库记录），再按生产工单领料出库
    ▼
车间消耗，整单无独立仓库库存积压
```

### NL2SQL 路由要点

涉及「订单完成率 / 收货完成率 / 到货及时率」等指标时，Prompt 须注入 **BPSUPPLIER.YPTHFLGM_0** 维度并显式要求按 `1` / `2` 分组；面向零库存供应商的查询应略过 `YPRECEIPT`，直接走 `PRECEIPT`。

详见 [[供应商收货流程（零库存 vs 非零库存）]]。

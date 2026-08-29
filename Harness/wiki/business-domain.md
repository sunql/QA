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

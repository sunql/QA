# 批量关系引擎 — 数据模板（业务层 39 类全量，含数据）

> 面向「通用批量关系引擎」的关系清单**全量预填**。类名、连接列、单据编号列均取自
> qa-system 本体库真实元数据（`qa_metadata`，核实 2026-09-09）。

## 覆盖范围

**DIM(5) + DWD(27) + DWS(5) + ADS(2) = 39 个业务类**。ODS(27 原始贴源)与 ETL 工具表
**不建语义关系**（只是 DWD 的镜像拷贝，仅在 ETL 血缘维度有意义），故排除。

| 文件 | 行数 | 性质 |
|---|---|---|
| `joins.csv` | **279** | **物理关联全量**：库里现有真实 join 完整快照（所有业务表间共享列关联）。重放=全 skip（幂等），用于**还原/审阅/一致性比对**，**无需执行** |
| `to-apply/relations.csv` | **45** | **语义关系（待建，唯一待执行清单）**：按单据结构/流转/层间同体/主数据归属推导，覆盖 37/39 类 |
| `to-apply/manifest.example.json` | 45 | 同 45 条，JSON（真实 id），弹窗粘贴用 |
| `to-apply/run-sheet.md` | — | **执行步骤**：引擎弹窗里怎么喂、预期结果、API 直连 |
| `README.md` | — | 本说明 |

**▶ 真正要执行的是 `to-apply/`，怎么跑见 `to-apply/run-sheet.md`。**

## 两层关系不要混

- **物理 join（279 已存在）= 共享列事实**：任何两个类只要含同名列（`SUPPLIER_CODE`、
  `MATERIAL_CODE`、`FACILITY_CODE`、`CURRENCY_CODE`、`YEAR_MONTH`、单据号…）就有一条。
  它**含机械噪音**（每个带供应商列的单据都连到全部 DWS 月度表）——是推断的固有形态，不是语义判断。
- **语义关系（45 待建）= 业务意图**：只记录有意义的结构/流转/归属，**刻意不复刻噪音**。

## CSV 表头契约（与服务端一致）

- **relations.csv**：`sourceClassName,targetClassName,relationType,description`
- **joins.csv**：`sourceClassName,sourceColumns,targetClassName,targetColumns,joinType,relationType,description`
  - 多列连接在 `sourceColumns`/`targetColumns` 内用 `;` 分隔，源/目标列数必须一致；`joinType` 缺省 `INNER`，`relationType` 缺省 `foreign_key`。
- 类名按类名或 `source_table` 尾段匹配；重复/缺失 → 该行记 `BatchRowError`，其余继续。

## 语义关系枚举（`ClassRelationType`，仅此 6 值）

`SUPPLIES` · `CONTAINS` · `GENERATES` · `INSPECTED_BY` · `GENERATED` · `RELATED_TO`

方向 = `源类 -(type)-> 目标类`。统一读法（每条 description 写明证据列）：

| 类型 | 读法 | 数量 |
|---|---|---|
| `CONTAINS` | 单据头含行 / 主体含子记录（键=单据号/主料号/表号） | 10 |
| `GENERATES` | 上游单据驱动下游产生（溯源列=单据号） | 9 |
| `GENERATED` | 源=汇总/派生，由目标明细聚合生成 | 12 |
| `SUPPLIES` | 供应商供应物料 | 1 |
| `INSPECTED_BY` | 主体被其质量档案评估 | 1 |
| `RELATED_TO` | 层间同体 / 主数据归属 / 关联实体 | 12 |

## 语义集合怎么来的（推导规则，可审计）

1. **单据结构 CONTAINS**：header→line 家族，键=真实单据号列（PO_NO/RECEIPT_NO/ARRIVAL_NOTICE_NO/QUOTATION_NO/INVOICE_NO/PAYMENT_NO、BOM.MATERIAL_CODE、PRICE_LIST_CODE）。
2. **采购价值链 GENERATES**（各跳都有真实溯源列）：
   `请购行 →(经 DWD_REQUISITION_ORDER_LINK)→ 采购行`；`报价 → 采购单`；`采购单 → 到货通知`；`到货通知 → 收货单`；`收货行 →(三单匹配)→ 发票行`；`发票 → 付款`。
3. **汇总 GENERATED**：各 DWS 月度 ← 最贴近明细（质量←收货行 REJECTED_QTY、价格←采购实际价、付款←付款单…）；ADS_360 ← 4 个 DWS 月度 + 供应商；ADS 订单明细 ← 采购行 + 收货行。
4. **主数据 RELATED_TO**：DIM↔DWD 同体；通用伙伴↔供应商/客户/承运商；物料↔工厂视图/工艺路线/BOM；客户↔兼供货供应商。
5. **仅 2 类无语义边**：`DIM_DATE`、`DIM_CURRENCY` —— 叶子维度，只被物理 join 引用，无业务语义边。

> 语义是**提案**，方向/依据都在 description。执行前先用 `/ontology/batch/preview` dry-run 看计数，
> 任何一条不合业务就改 CSV 那行再跑。冲突策略与幂等见 `to-apply/run-sheet.md`。

## 关联

- 引擎实现与变更 SSOT：`Harness/changes/feat-batch-relation-engine/summary.md`
- 图库现状（2026-09-09）：Class 67 + Property 3629；边 HAS_PROPERTY 3629 / JOIN 200 / SUPPLIES 1

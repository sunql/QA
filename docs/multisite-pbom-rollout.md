---
created: 2026-08-20
updated: 2026-08-20
sources:
  - /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/docs/pBOM_multisite.xlsx
  - /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/docs/multisite-pbom-model.md
tags: [qa-system, pBOM, 多工厂, 接口契约, 落地路径, PLM-ERP-MES-WMS]
---

# 多工厂 PBOM 落地路径

一句话：把 [[多工厂 PBOM 建模指南]] 的数据模型接到 PLM / ERP-MES / WMS-TMS 上跑通，让"主厂做哪几步、外协做哪几步、最后谁发给客户"在系统间自动流转。

## 详细说明

### 一、端到端系统架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          多工厂 PBOM 数据流                              │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  PLM (Sage X3 EBOM)  ─IF-01/02/03──▶  MDM  ─IF-04──▶  ERP (PBOM)        │
│       │ 工程源头                 主数据中台              │              │
│       │ 工艺路线源头                                 按基地拆 BOM        │
│       │ ECN 源头                                       │              │
│                                                          ▼              │
│                                       ┌─────────────────────────────┐   │
│                                       │  MES (多工厂)              │   │
│                                       │  PLANT-A / PLANT-B / 外协  │   │
│                                       │  按工艺路线派工 / 报工     │   │
│                                       └────────┬────────────────────┘   │
│                                                │ 跨厂完工 + 发运指令    │
│                                                ▼                         │
│                                       ┌─────────────────────────────┐   │
│                                       │  WMS (按工厂独立仓库)      │   │
│                                       │  PLANT-A 总仓              │   │
│                                       │  PLANT-B 协作仓            │   │
│                                       │  外协厂 VMI 仓             │   │
│                                       └────────┬────────────────────┘   │
│                                                │ 出库 / 在途             │
│                                                ▼                         │
│                                       ┌─────────────────────────────┐   │
│                                       │  TMS 物流调度              │   │
│                                       │  跨厂段 / 客户段           │   │
│                                       └─────────────────────────────┘   │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 二、PBOM 数据源头与责任边界

| 数据 | 源头系统 | 责任角色 | 推到哪 |
|---|---|---|---|
| EBOM 物料结构 | PLM | 研发 BOM 工程师 | MDM → ERP |
| 工艺路线（工序/工厂/外协） | PLM | 工艺工程师 | MDM → ERP → MES |
| PBOM 按基地拆分 | MDM/ERP | 主数据治理小组 | ERP 各工厂视图 |
| 跨厂物流细则（12_LOGISTICS） | ERP/MDM | 物流计划员 | TMS |
| 主厂组装 BOM | ERP（PLANT-A 视图） | 生产计划 | MES |
| 包装 BOM | ERP/MDM | 包装工艺工程师 | MES/WMS |
| 物料主数据 | MDM | 主数据治理 | ERP/MES/WMS 全局 |
| 工厂主数据（00_PLANT_MASTER） | MDM | 主数据治理 | ERP/MES/WMS/TMS 全局 |

> **核心原则**：每类数据**只有一处源头**，变更必须在源头系统执行；下游订阅而非重复维护。

### 三、接口契约清单（按边界划分）

#### 3.1 PLM → MDM（工程源头）

| ID | 名称 | 触发时机 | 关键字段 | 频次 |
|---|---|---|---|---|
| IF-PL-01 | 物料基本信息推送 | 物料审核通过 | `mat_code / mat_name / unit / spec / drawing_rev` | 实时 |
| IF-PL-02 | EBOM 全树推送 | BOM 审核通过 | `parent / child / qty / reference_type / borrowed_from` | 实时 |
| IF-PL-03 | 工艺路线推送 | 工艺审核通过 | `routing_no / op_seq / op_name / plant_code / outsource_type / cross_plant` | 实时 |
| IF-PL-04 | ECN 变更推送 | ECN 审核通过 | `ecn_no / change_type / before / after / affected_items` | 实时 |
| IF-PL-21 | 引擎回流物料（客供件） | 销售订单释放 | `mat_code / supplier / spec` | 日批次 |
| IF-PL-22 | 跟升 ECN 通知 | 借用件原项目升级 | `borrowed_from / new_rev / affected_projects` | 实时 |

#### 3.2 MDM → ERP（按基地拆 BOM）

| ID | 名称 | 触发时机 | 关键字段 | 频次 |
|---|---|---|---|---|
| IF-MD-01 | 物料分发 | MDM 审核通过 | `mat_code / plant_visible[]` | 实时 |
| IF-MD-02 | BOM 按基地生成 | 工艺路线下发后 | `pbom_code / plant_code / base_ebom_rev / alt_bom_flag` | 实时 |
| IF-MD-03 | 工艺路线下发 | 工艺审核通过 | `routing_no / plant_code / op_list[]` | 实时 |
| IF-MD-04 | 工厂主数据同步 | 工厂新增/变更 | `plant_code / address / tax_no / plant_type` | 实时 |
| IF-MD-05 | 外协供应商主数据 | 供应商资质审核 | `supplier_code / plant_code / outsource_type / capability` | 实时 |

#### 3.3 ERP → MES（生产执行）

| ID | 名称 | 触发时机 | 关键字段 | 频次 |
|---|---|---|---|---|
| IF-EM-01 | 生产订单下发 | MRP 运算后 | `mo_no / mat_code / plant_code / qty / start_date / end_date` | 日批次 + 紧急实时 |
| IF-EM-02 | 工艺路线下发 | PBOM 变更后 | `routing_no / plant_code / ops[] / equipment[] / tooling[]` | 实时 |
| IF-EM-03 | 跨厂物料需求（PR） | 子件工厂≠主厂时 | `parent_mo / child_mat_code / source_plant / need_qty / need_date` | 实时 |
| IF-EM-04 | 委外工序单 | 外协工序触发 | `op_id / supplier_code / supplied_mat / process_fee` | 实时 |
| IF-EM-05 | 直发客户订单 | 销售订单释放 | `so_no / final_plant / ship_to / direct_ship_flag` | 实时 |

#### 3.4 MES → WMS（仓储执行）

| ID | 名称 | 触发时机 | 关键字段 | 频次 |
|---|---|---|---|---|
| IF-MW-01 | 入库指令（完工） | 工序报工完成 | `mat_code / plant_code / warehouse / qty / batch / inspection_required` | 实时 |
| IF-MW-02 | 出库指令（领料） | 工单开工 | `mo_no / mat_code / plant_code / qty / lot` | 实时 |
| IF-MW-03 | 跨厂调拨出库 | 跨厂完工触发 | `transfer_no / from_plant / to_plant / mat_code / qty / eta` | 实时 |
| IF-MW-04 | VMI 收发货 | 外协 VMI 仓 | `supplier / vmi_warehouse / mat_code / delta / owner` | 实时 |
| IF-MW-05 | 直发客户出库 | 销售订单触发 | `so_no / ship_from / ship_to / carrier / tracking_no` | 实时 |

#### 3.5 WMS → TMS / 物流执行

| ID | 名称 | 触发时机 | 关键字段 | 频次 |
|---|---|---|---|---|
| IF-WT-01 | 跨厂运输任务 | 跨厂调拨出库后 | `transfer_no / from / to / weight / volume / distance / transit_days` | 实时 |
| IF-WT-02 | 直发客户运单 | 直发出库后 | `so_no / carrier / ship_to / weight / service_level` | 实时 |
| IF-WT-03 | 在途签收回传 | 收货方扫码 | `transfer_no / received_qty / inspection_result / exception` | 实时 |
| IF-WT-04 | 运费结算 | 月底对账 | `carrier / period / freight_total / weight_total / exceptions` | 月批次 |

### 四、关键控制点（必须落到数据 + 系统）

#### 4.1 跨厂在途控制点

```
工序完工 (MES) 
  └─▶ IF-MW-03 跨厂调拨出库 (WMS)
        └─▶ IF-WT-01 跨厂运输任务 (TMS)
              └─▶ 在途天数 = 12_LOGISTICS.TransitDays (PBOM 数据)
                    └─▶ IF-WT-03 签收回传
                          └─▶ IF-MW-01 收货入库 (WMS)
                                └─▶ 收货方质检 (OP991 虚拟工序, MES)
                                      └─▶ 合格：MRP 推进 / 不合格：触发退货 IF-WT-05
```

**关键数据**：在途天数、运输方式、收货方质检规则必须从 12_CROSS_PLANT_LOGISTICS 取，不允许在 MES/WMS 重新配置。

#### 4.2 外协工序控制点

```
PLANT-A 主厂 OP020 完工
  └─▶ IF-EM-04 委外工序单（MES → 外协厂）
        └─▶ 外协厂接收（外协厂 MES / 邮件 / 看板）
              └─▶ 外协厂生产（外协厂 MES 或手工报工）
                    └─▶ 外协厂完工发货
                          └─▶ IF-MW-03 主厂收货（VMI 仓或直送产线）
                                └─▶ 收货方质检
                                      └─▶ 合格入库 / 不合格退货
```

**关键控制**：
- 主厂供料到外协厂的物料（WMS 出库 + 外协 VMI 入库）必须先于委外工序单释放（避免外协厂开工无料）
- 外协厂必须返回实际工序报工数据（用于核算加工费 + 工序成本归集）

#### 4.3 直发客户控制点

```
N001-OP999 直发客户虚拟工序触发
  └─▶ IF-EM-05 直发客户订单（MES → 销售）
        └─▶ IF-MW-05 直发出库（WMS → 仓库拣货 + 装车）
              └─▶ IF-WT-02 客户运单（TMS → 第三方物流）
                    └─▶ 客户签收（TMS 回传 → 销售订单关闭）
```

**关键控制**：
- 直发客户订单的"最终发运工厂"必须等于 N001-OP010 主厂组装工厂，否则触发例外
- 直发环节需要同时检查：客户要货日期 - 直发在途天数 - 工厂生产周期 ≥ 今天

### 五、关键不变量（系统开发期 + 运维期都要守护）

| 不变量 | 含义 | 守护手段 |
|---|---|---|
| INV-01 | 物料编码全局唯一 | MDM 主键约束 |
| INV-02 | 工厂代码必须在 00_PLANT_MASTER | MDM 外键约束 |
| INV-03 | 03_OPERATION.plant 必须在工厂主数据中存在 | 入库校验 |
| INV-04 | 11_PLANT_FLOW 中"跨厂边界=是"必须有对应 12_LOGISTICS 行 | 入库校验 |
| INV-05 | 12_LOGISTICS 中发货工厂 + 收货工厂组合必须唯一 | 唯一键 |
| INV-06 | N001 主产品的最终发运工厂 = N001-OP010 主厂 | 变更校验 |
| INV-07 | 所有跨厂工序的"在途天数"必须 ≥ 1 | 数据约束 |
| INV-08 | 外协厂接单必须先有 VMI 仓（或主厂供料） | 业务流程前置 |
| INV-09 | 直发客户订单的"最终发运工厂"必须等于 N001-OP010 工厂 | 订单校验 |
| INV-10 | OP999 直发虚拟工序的"运输方式"必须从 12_LOGISTICS 派生 | 取数规则 |

### 六、实施步骤（4 阶段 12 个月）

#### Phase 1：基础建设（M1-M3，单工厂跑通）

- [ ] M1：工厂主数据 00_PLANT_MASTER 落 MDM；PLANT-A 一个工厂跑通完整 PBOM → ERP → MES → WMS 数据流
- [ ] M2：03_OPERATION 加加工厂字段（仅 PLANT-A）；11_PBOM_PLANT_FLOW 出厂；单工厂 PBOM 自动派工验证
- [ ] M3：直发客户 N001-OP999 流程跑通（含 TMS）；SLA 监控上线

**验收**：单工厂 PBOM 全流程端到端打通，物料从 EBOM → PBOM → MES → WMS → 客户 7 天内闭环

#### Phase 2：协作厂接入（M4-M6，集团内多厂）

- [ ] M4：PLANT-B 工厂主数据落 MDM；注塑件（N002/N006/N007）按工厂分配规则分到 PLANT-B
- [ ] M5：12_CROSS_PLANT_LOGISTICS 落 PLANT-A↔PLANT-B 段（180km 1d 汽运）；IF-MW-03 / IF-WT-01 / IF-WT-03 跑通
- [ ] M6：跨厂在途算入 MRP 提前期；PLANT-B 注塑件完工触发 PLANT-A 收货 + OP991 质检

**验收**：跨厂物料从 PLANT-B 完工 → 在途 → PLANT-A 收货 → 投料组装全程可追溯，在途数据 100% 与 12_LOGISTICS 一致

#### Phase 3：外协厂接入（M7-M9，包工包料 + 工序外协）

- [ ] M7：SUPPLIER-S1/S2/S3 + SUPPLIER-OUTSIDE 主数据落 MDM；外购件按物料号 hash 分配供应商
- [ ] M8：IF-EM-04 委外工序单跑通（VMI 仓收发 + 主厂供料触发）；外协厂报工数据回传
- [ ] M9：委外加工费核算 + 外协厂考核 KPI（OTD / 合格率 / 加工费偏差）上线

**验收**：外协厂全流程数据闭环；主厂能实时看到外协厂生产进度、在途、质检结果

#### Phase 4：直发客户 + 优化（M10-M12）

- [ ] M10：客户直发场景验证（多客户多目的地）；N001-OP999 直发客户工序在多客户下都能正确派单
- [ ] M11：TMS 多承运商对接；运费自动结算
- [ ] M12：所有 KPI / 不变量监控上线；端到端 SLA 报告

**验收**：PBOM-工厂-物流-财务四流合一，跨厂订单交付准确率 ≥ 98%

### 七、与现有系统的对接现状（THWL 项目）

> 这部分随实际系统演进持续更新。

| 系统 | 当前状态 | 落地节奏 |
|---|---|---|
| PLM (Sage X3) | EBOM 源头已建；工艺路线源头待建 | M1 同步工艺路线源头 |
| MDM | 主数据中台已有；工厂主数据待建 | M1 工厂主数据 |
| ERP (Sage X3) | PBOM 按物料维护；未按基地拆分 | M1 起按基地拆 BOM |
| MES | 单工厂派工已有；多工厂 + 跨厂待建 | M4-M6 跨厂 + M7-M9 外协 |
| WMS | 单工厂仓库；多工厂仓 + VMI 待建 | M4 多工厂仓 + M7 VMI |
| TMS | 已有第三方物流；多承运商调度待建 | M11 |

### 八、关键风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 工厂分配规则硬编码在脚本 | 规则变更需改代码 | 抽到工厂分配策略表（M1） |
| 跨厂在途算入 MRP 后提前期变长 | 客户要货日期可能延后 | M5 与销售沟通调提前期；引入并行调度 |
| 外协厂报工数据不准 | 加工费核算 + 工序成本归集错 | M8 强制外协厂系统对接；初期可手工补但需考核 |
| 直发客户工厂≠主厂组装工厂 | 例外频发 | M10 与销售统一规则；客户直发订单审核节点 |
| 多工厂库存数据分散 | 跨厂调拨可执行性差 | M4 上集团库存可视化；按需调拨 |
| ECN 跨工厂级联失效 | 部分工厂没更新 | INV-04 守护；M12 上 ECN 变更追踪看板 |

### 九、量化 KPI

| 指标 | 目标 | 度量来源 |
|---|---|---|
| PBOM 跨厂自动派工率 | ≥ 95% | MES 派工日志 |
| 跨厂在途数据与 12_LOGISTICS 一致率 | 100% | 对账日报 |
| 跨厂 MRP 提前期准确度 | ±0.5 天 | MRP 模拟 vs 实际 |
| 外协厂 OTD | ≥ 95% | 外协工序单考核 |
| 外协厂来料合格率 | ≥ 99% | 收货方质检 |
| 直发客户订单准时率 | ≥ 98% | TMS 签收 |
| 跨厂订单交付准确率 | ≥ 98% | 端到端对账 |
| 跨厂物流运费占成本比 | ≤ 3% | 财务核算 |

### 十、可复用条目

- [[多工厂 PBOM 建模指南]]：数据模型（字段/sheet/虚拟工序）
- [[qa-system 离线迁移方案]]：相关基础设施（如果跨厂系统也要搬）
- THWL_MDM [[08-PLM-ERP集成技术要求_v2.1]]：单工厂接口契约（多工厂版需升级）
- THWL_MDM [[12-生产型物料及BOM管理方案_v2.2]]：BOM 数据标准（多工厂版需补充工厂字段）

## 相关条目

- [[多工厂 PBOM 建模指南]]（数据模型）
- [[多工厂 PBOM 落地路径]]（本文）
- [[qa-system 离线迁移方案]]（基础设施迁移参考）
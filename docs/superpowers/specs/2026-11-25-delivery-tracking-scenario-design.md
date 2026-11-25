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

**工具 SQL 只写本体属性名，物理表名与物理列名由本体解析**。
这样做的收益（对应用户的三点顾虑）：

1. **不硬编码**：换数据源时改本体绑定即可，工具 SQL 不动；
2. **一个 Agent 一个数据源**：本体类的 `source_table`（可带 `schema.` 前缀）
   就是该 Agent 的数据源锚点，跨源时对象名/属性名变化由本体映射吸收；
3. **可配置化**：跨源差异最终收敛到本体配置行，不需要每个工具各写一套。

**前车之鉴（不可忽视）**：`backend/scripts/procurement_class_source_table_realdata.py`
记录了 2026-11-18 的真实事故 —— 迁移把采购域类的 `source_table` 绑到 DWD 词表，
而本体 SSOT 绑的是 ERP 词表 ⇒ NL2SQL 指向**不存在的表**，且 `seed_ontology.seed()`
在干净库上**直接崩**（`source_table` 查重与 `class_name` 唯一约束冲突）。
⇒ 本方案必须遵守「类名与 `source_table` 绑定同步」+ 建类后跑真实数据门禁。

### 3.1 本体现状盘点（2026-11-25 只读探针 + PG 实查）

采购订单相关的本体类（68 个活类中）：

| 类名 | `source_table` | 属性数 | 数据行数 | 数据截止 | 状态 |
|---|---|---|---|---|---|
| `DWD_PURCHASE_ORDER_DTL`（BO=PO 采购订单） | 同名 | 30 | 1,036,069 | 2026-11-24 | ✅ 新鲜，但是**订单头**（无数量/物料列） |
| `DWD_PURCHASE_ORDER_LINE`（BO=POL 采购订单行） | 同名 | 29 | 927,648 | 2026-08-08 | ⚠️ 属性齐全但**数据旧**，本期窗口为空 |
| `ODS_PORDERQ` | 同名 | 151 | 927,648 | 2026-08-10 | ⚠️ 同批旧快照 |
| `ODS_ERP_PORDERQ` | — | — | 1,036,089 | 2026-11-24 | ❌ **本体未建类** |

**关键事实**：

- `DWD_PURCHASE_ORDER_LINE` 的本体属性**已经包含**到货跟踪所需的全部语义：
  `ORDER_QTY` / `RECEIVED_QTY` / `ORDER_DATE` / `PROMISED_RECEIPT_DATE` /
  `LAST_RECEIPT_DATE` / `SUPPLIER_CODE` / `MATERIAL_CODE` / `PO_NO` / `PO_LINE_NO` / `LINE_STATUS`
  —— **语义名与物理列同名**（数仓层 DWD 表已做语义化）。
- 但该表指向 **2026-08-08 的抽取批次**（近 12 月 19,422 单，**本期 0 单**），
  而需求 3 的「本期」在数仓层新快照里有 **4,844 张订单**。
- 数仓层新快照的订单头表 `DWD_PURCHASE_ORDER_DTL` 与 `ODS_ERP_PORDERQ` 行数几乎一致
  （1,036,069 vs 1,036,089），二者是同批数据的头/行两面。
- 订单覆盖交叉验证：`DWD_PURCHASE_ORDER_LINE` 的 200,561 张订单 **99.98%** 落在
  `ODS_ERP_PORDERQ` 内；反向 227,909 张订单中 87.99% 有行级明细 ⇒ 两表是同源不同批次。

**缺口定位**：本体已有「行级语义属性」（在旧批次表上），但**行级新鲜数据所在的
`ODS_ERP_PORDERQ` 未建类**。按 `backend/app/domain/procurement_object_catalog.py:413`
的既有登记，`ODS_ERP_*` 属于「其它采购相关源表（未建类）」，
`carriedBy = "单独立项（按需建类，不在 M1-1.4 范围）"`。

**决策**：本需求触发该立项 —— **先建本体类，再做场景**。

### 3.2 新建本体类

| 项 | 值 |
|---|---|
| `class_name` | `ODS_ERP_PORDERQ` |
| `source_table` | `ODS_ERP_PORDERQ`（数仓层 `THBI` schema；`source_table` 支持 `schema.` 前缀） |
| `object_type` | `Transaction` |
| 归属 | 采购域（是否新增 BO 待议，见 3.5） |

**属性定义**（语义名 → 物理列，全部来自探针实测的 `THBI.ODS_ERP_PORDERQ` 列）：

| 本体属性名 | 物理列 | 类型 | 用途 |
|---|---|---|---|
| `PO_NO` | `POHNUM_0` | STRING | 订单号 |
| `PO_LINE_NO` | `POPLIN_0` | INT | 订单行号 |
| `ORDER_DATE` | `ORDDAT_0` | DATETIME | 下单日期（周期锚定） |
| `SUPPLIER_CODE` | `BPSNUM_0` | STRING | 供应商编码（过滤 + 关联 `DIM_SUPPLIER`） |
| `MATERIAL_CODE` | `ITMREF_0` | STRING | 物料编码（关联 `DIM_IMATERIAL`） |
| `ORDER_QTY` | `QTYSTU_0` | DECIMAL | 订购量（库存单位） |
| `RECEIVED_QTY` | `RCPQTYSTU_0` | DECIMAL | 已收量（库存单位） |
| `PROMISED_RECEIPT_DATE` | `EXTRCPDAT_0` | DATETIME | 预期到货日期 |
| `LAST_RECEIPT_DATE` | `LASRCPDAT_0` | DATETIME | 最后到货日期 |
| `RETURNED_QTY` | `RETQTYSTU_0` | DECIMAL | 退货量 |

**建类约束（防重演 2026-11-18 事故）**：

1. `class_name` 与 `source_table` 同步登记（同一份 seed）；
2. 建类后跑**真实数据门禁**（对照
   `procurement_class_source_table_realdata.py` 的形态）：干净库跑迁移链 ⇒
   类存在、`source_table` 指向真实存在的表、属性列全部存在、`seed_ontology.seed()` 不撞唯一约束；
3. 属性名与 `source_column` 一一对应且物理列在目标数据源真实存在（探针已验证
   `THBI.ODS_ERP_PORDERQ` 150 列，10 个目标列全部存在且非空率 100%）。

**为什么不复用 `DWD_PURCHASE_ORDER_LINE` 的属性定义**：该类的语义名与物理列同名
（数仓 DWD 表已语义化），而 `ODS_ERP_PORDERQ` 的物理列是 ERP 原始名
（`QTYSTU_0` 等）。**建新类时把语义名与原始列显式绑定**，正是本体层吸收跨源差异的机制。

### 3.3 工具如何用本体取数

工具 SQL 模板**只写属性名**，运行时经本体解析为物理列：

```
SELECT
  <PO_NO>, <PO_LINE_NO>, <SUPPLIER_CODE>, <MATERIAL_CODE>,
  <ORDER_QTY>, <RECEIVED_QTY>, <PROMISED_RECEIPT_DATE>, <LAST_RECEIPT_DATE>
FROM <源表>   -- 由本体类的 source_table 解析（含 schema）
WHERE <ORDER_DATE> >= DATE :from AND <ORDER_DATE> < DATE :to
```

解析实现复用本体既有设施：

- `ontology_class.source_table` → 物理表（支持 `schema.` 前缀，
  见 `ontology_service.py:1753` 的「去 schema 前缀」索引逻辑）；
- `ontology_property.source_column` → 物理列（NL2SQL 与数据质量规则同款用法）；
- 类→属性解析须在工具执行时从 DB 读取（本体是可治理资产，不能进代码常量）。

**解析失败处理（fail-closed）**：

| 情形 | 行为 |
|---|---|
| 类不存在 / 已墓碑（`valid_to` 非空） | `NotFoundError`（提示先建类） |
| 必需属性缺失或 `source_column` 为空 | `ValidationError`（列出缺失属性名） |
| 物理列在目标数据源不存在 | `ValidationError`（提示本体与数据源漂移） |
| 类未绑定数据源 | `ValidationError` |

### 3.4 数据源绑定：每个 Agent 一个

用户原则：「每个智能体都确定唯一的一个数据源就好了」。

- `DELIVERY_TRACKING_AGENT` 绑定的数据源 = 本体类 `ODS_ERP_PORDERQ` 所属的数据源
  （`THBI` schema 所在的业务库，即当前 `data_source.id=1` THBI Oracle）；
- 工具执行时按本体类解析出的 `source_table` 解析 schema，不跨源拼接；
- 场景的 `datasourceId` 槽位（admin 可切）**保留**，但切换后必须校验
  该数据源内存在同名类/表 —— 不存在则 fail loud（`ValidationError`），
  **不静默回退到默认源**。

**跨数据源的行为**（回应用户「没有同一智能体跨数据源的能力，除非都是配置化的」）：

- 本方案**不**支持一个 Agent 同时跨多个数据源取数；
- 若业务要求换源，正确做法是**换一个本体类**（新 `source_table` 绑定 + 新属性映射），
  而不是让工具里写死 `ZJTH.` / `THBI.` 前缀；
- 对象的**业务名**（订单号、供应商、订购量…）在本体层稳定，物理表列名随源变化 ——
  这正是本体层存在的意义。

### 3.5 字典表关联（同样走本体）

供应商名 / 物料名通过本体类 `DIM_SUPPLIER` / `DIM_IMATERIAL` 关联
（两者本体均已登记，`source_table` 同名）：

- `DIM_SUPPLIER`（3,500 行，键唯一）：`BPSNUM_0` → `BPSNAM_0`
  —— 与 `ODS_ERP_PORDERQ` 的供应商码域 **100% 匹配**（1,191/1,191）；
- `DIM_IMATERIAL`（350,922 行，键唯一）：`ITMREF_0` → `ITMDES1_0`
  —— 与 `ODS_ERP_PORDERQ` 的物料码域 **100% 匹配**（306,939/306,939）。

**⚠ 字典覆盖率坑（探针实测）**：本体词表里的 `DIM_*` 与 `ODS_ERP_*` 是**两批不同抽取**，
`DIM_*` 的名称列覆盖率明显低于 `ODS_ERP_*`：

| 字典 | 行数 | 本期取名缺失 |
|---|---|---|
| `THBI.ODS_ERP_ITMMASTER` | 366,611 | **2 行** |
| `THBI.DIM_IMATERIAL` | 350,922 | 1,883 行（9.4%） |
| `THBI.ODS_ERP_BPSUPPLIER` | 3,812 | **0 行** |
| `THBI.DIM_SUPPLIER` | 3,500 | 110 行 |

⇒ **名称一律 JOIN `ODS_ERP_ITMMASTER` / `ODS_ERP_BPSUPPLIER`**，`DIM_*` 不参与取数。
（`ODS_ERP_ITMMASTER` / `ODS_ERP_BPSUPPLIER` 本体**未建类**，
若走本体对接需一并建类；否则名称从 MDM `entity_mapping` 取，不依赖数仓字典。）

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
| `ODS_ERP_PORDERQ` 本体类 | 本体资产 | `backend/seed_ontology.py`（类 + 属性 + `source_table` 绑定） | 行级到货数据的本体登记；工具取数的唯一锚点 |
| `delivery_tracking_query` | 工具 | `backend/app/services/agent_tools_procurement.py` + `seed_agent_tool_configs.py` | 概览 + 双层明细取数（**经本体解析物理表列**） |
| 本体解析件 | 服务 | `backend/app/services/agent_tools_procurement.py`（或新建 `ontology_binding_service.py`） | 类名 → 物理表 + 属性名 → 物理列；fail-closed |
| `DELIVERY_TRACKING_AGENT` | Agent | `backend/scripts/seed_agents.py` + `seed_procurement_agent_profiles.py` | 编排与输出纪律；**绑定唯一数据源** |
| `delivery_tracking` | 场景配置 | `backend/scripts/seed_copilot_scenarios.py` | 卡片墙 / 槽位 / 模板 / 渲染器绑定 |
| `DeliveryTrackingResultCard` | 前端组件 | `frontend/src/components/copilot/` | 结果卡（概览 + 双 Tab） |
| `pending_delivery` 渲染器扩展 | 前端 | `PendingDeliveryResultTabs.tsx` | 分发到新卡 |
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

### 5.8 本体对接的落地机制（新增组件）

工具不直接写物理表名，取数前先做一次「本体解析」，把语义名换成物理标识：

```
输入：类名 ODS_ERP_PORDERQ + 需要的属性名列表
  ↓ OntologyService（读 ontology_class / ontology_property）
  ├─ 类：查 class_name='ODS_ERP_PORDERQ' 且 valid_to IS NULL
  │     → source_table（物理表，可带 schema. 前缀）
  ├─ 属性：按 property_name 查该类的 ontology_property
  │     → source_column（物理列）
  └─ 缺失校验：必需属性齐全？物理列在该数据源真实存在？
  ↓
输出：{ table: "THBI.ODS_ERP_PORDERQ", columns: { ORDER_QTY: "QTYSTU_0", ... } }
  ↓
SQL 模板按解析结果渲染（列名与表名均为解析产物，非代码常量）
```

**实现要点**：

| 要点 | 做法 |
|---|---|
| 解析时机 | 工具执行时（每次或按本体版本缓存），**不进代码常量** —— 本体是可治理资产 |
| 类查询 | 只取未软删类（`valid_to IS NULL`），墓碑类 fail loud |
| 属性缺失 | `ValidationError` 并列出缺失属性名（不静默用空列） |
| 物理列漂移 | 校验物理列在该数据源存在；不存在 ⇒ `ValidationError` 提示本体与数据源漂移 |
| SQL 注入 | 解析出的表名/列名仍走既有标识符白名单校验（与 `schema_introspection_service` 同款） |
| 缓存 | 可按「本体类 + 属性集」缓存解析结果；本体变更时须失效（本体有治理事件，可挂失效钩子） |

**为什么不用 NL2SQL 那条路**：NL2SQL 走 LLM 生成 SQL，适合开放式问句；
本场景是**固定口径的指标查询**（到货率三态），需要确定性 ⇒ 模板 SQL + 本体解析，
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

- 不改既有 `pending_delivery_query` 的表层（保持源层），避免回归既有已验收场景；
  （迁移到本体/数仓层作为后续独立立项）
- 不做双层数据源切换参数（当前只有数仓层一个真实需求）；
- 不做 `generic_table` 通用渲染器（本场景专用卡即可，避免过度抽象）；
- 不做跨 schema JOIN（数仓层字典已齐）；
- 不做订单头状态过滤（行级量判定已足够，且避开闭单假绿）；
- 不做「一个 Agent 跨多个数据源」（用户原则：跨源应换本体类，不在工具里拼源）。

## 10. 风险

| 风险 | 缓解 |
|---|---|
| **建类引入本体绑定漂移**（2026-11-18 已发生过一次） | 建类走既有治理流程 + 真实数据门禁脚本；类名与 `source_table` 同步登记 |
| 本体类被治理侧删除/改绑，工具随之失效 | 解析 fail-closed + 明确报错（不静默换表） |
| 与「待到货」场景表层不同，用户可能混淆 | 卡片标注数据层；两场景文案区分（到货达成 vs 欠交催货） |
| 数仓层供应商码与 MDM 码域未来漂移 | 解析统一走 MDM；过滤前校验码在 MDM 域内 |
| 订单明细行数膨胀 | `limit` 上限 50（沿用 `_MAX_LIMIT`），概览层不受影响 |
| 24 日锚点与「自然月」两套窗口并存易混 | 卡上显示实际 `window.dateFrom/dateTo`，不只显示口径名 |
| 量级率被超大额行主导，用户误读 | 主指标用订单率；量级率降为参考并附口径说明 |

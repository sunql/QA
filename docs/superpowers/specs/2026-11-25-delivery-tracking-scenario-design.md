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

**核心交付物**：一个专用只读工具 + 一个结果渲染器 + 一个场景配置行。

## 2. 关键结论：需要编码，不能纯模板配置

P4 已把「场景外壳」（卡片墙 / 槽位表单 / 提问模板 / 冲突让位 / 结果渲染器分发）全部做成
DB 配置。新场景的**外壳**因此可以零代码，但**取数层必须写代码**，三处硬缺口：

| 缺口 | 说明 |
|---|---|
| ① 现有 `pending_delivery_query` 谓词是「只看欠交」 | 三段 SQL 全部带 `QTYSTU_0 - RCPQTYSTU_0 > 0`，只返回未到货行，**没有分母**，取不到「已到货」物料 |
| ② 24 日周期不是静态日期 | `copilot_scenario.chips` 是 DB 静态 JSON，写死的 `dateFrom/dateTo` 不随日期滚动；`monthsBack` 是自然月，非 24 日锚点 |
| ③ 无对应结果渲染器 | `RESULT_RENDERER_WHITELIST` 中 `generic_table` 是空壳（前端未实现），到货跟踪需要「概览 + 双 Tab 明细」两层结构 |

## 3. 取数层：`THBI.ODS_ERP_PORDERQ`（数仓层）

### 3.1 三层对比（2026-11-25 只读探针实测）

| 层 | 总行数 | 近 12 月行数 | 近 12 月订单 | 最大下单日 |
|---|---|---|---|---|
| `THBI.ODS_ERP_PORDERQ`（数仓层，**采用**） | **1,036,089** | **192,467** | **46,770** | **2026-11-24** |
| `THBI.ODS_PORDERQ` | 927,648 | 84,026 | 19,422 | 2026-08-10 |
| `ZJTH.PORDERQ`（现有 `pending_delivery_query` 用） | 927,671 | 84,049 | 19,440 | 2026-10-23 |

数仓层近 12 月是源层的 **2.3 倍** —— 源层 `ZJTH` 是单分厂快照，数仓层才是全量 ERP 抽取。
`ODS_ERP_PORDERQ` 无重复：1,036,089 行 = 1,036,089 个唯一 `POHNUM_0 + POPLIN_0` 键。

**注意**：本场景与既有「待到货」场景**表层不同**（后者查 `ZJTH.PORDERQ`）。两者时间口径一致但
数据覆盖面不同，界面上须标注数据层以免误读。

### 3.2 字典表同层齐全，无需跨 schema JOIN

`THBI` 下可用：`ODS_ERP_ITMMASTER`、`ODS_ERP_BPSUPPLIER`、`DIM_IMATERIAL`、`DIM_SUPPLIER`、
`ODS_ERP_FACILITY`。（`ZJTH.ITMMASTER` / `ZJTH.BPSUPPLIER` 也存在，但**不采用**——避免跨 schema JOIN。）

**字典选型（探针实测，不能想当然用 DIM_）**：

| 字典 | 行数 | 键唯一 | 本期(19,910 行)取名缺失 | 采纳 |
|---|---|---|---|---|
| `THBI.ODS_ERP_ITMMASTER` | 366,611 | ✅ | **2 行** | ✅ 物料名 |
| `THBI.DIM_IMATERIAL` | 350,922 | ✅ | 1,883 行（9.4%） | ❌ |
| `THBI.ODS_ERP_BPSUPPLIER` | 3,812 | ✅ | **0 行** | ✅ 供应商名 |
| `THBI.DIM_SUPPLIER` | 3,500 | ✅ | 110 行 | ❌ |

`DIM_*` 系列覆盖率明显低于 `ODS_ERP_*`（DIM_IMATERIAL 缺 9.4% 物料描述）。
**结论：名称一律 JOIN `ODS_ERP_ITMMASTER` / `ODS_ERP_BPSUPPLIER`**，
`DIM_*` 不参与取数（若需 MDM 侧企业键，另由 `entity_mapping` 提供）。

### 3.3 供应商码体系已打通

MDM `entity_mapping`（`entity_type=SUPPLIER`，3,500 行，`source_system=ERP`）的
`enterprise_code == source_code` 比例 **100%**，且 ERP 码集（1,193 个）**100% 落在** MDM 码集内。
因此：

- 供应商检索（编码 / 企业键 / 全名 / 简称）→ 复用 `Supplier360Service._resolveSupplier` 解析阶梯；
- 解析出的 `enterpriseCode` 可直接作为 `BPSNUM_0` 的 `IN` 过滤值。

实测（本期窗口）：`B125` → 1,006 行、`D1` → 8,354 行、`C1` → 7,657 行、`Q001` → 599 行。

### 3.4 到货明细表 `ODS_ERP_YPRECEIPTD` 的限制

82,435 行 / 19,351 订单 / 截止 2026-11-24，但 `BPSNUM_0` **100% 为空** ⇒ 不能直接按供应商过滤，
只能经 `ODS_ERP_PORDERQ` 关联取供应商（实测 1,116 行中仅 3 行取不到名）。
且其覆盖订单数远小于 46,770，**不足以支撑「部分到货」判定**。

**决策**：到货判定基于 `ODS_ERP_PORDERQ` 行级量，不依赖到货明细表；到货明细仅用于展示实际到货日期
（`LAST_RECEIPT_DATE`，实测已收行 100% 非空）。

## 4. 口径定义（探针实测结论）

### 4.1 到货判定：行级量，不看订单状态

```
行级欠交量  OPEN_QTY = QTYSTU_0 - RCPQTYSTU_0     -- 同为库存单位
订单三态：
  全到货    SUM(OPEN_QTY) = 0
  部分到货  有欠交 且 有已收（RCPQTYSTU_0 > 0）
  未到货    所有行 RCPQTYSTU_0 = 0
```

**为什么不用订单头 `RCPLINNBR_0 >= LINNBR_0`**：头行两种判定在近 12 月仅差 4 单、本期差 0 单，
但头表另有 11,817 张「已关闭（CLEFLG_0=2）却仍有欠交」的订单，且其欠交总量级异常
（4.5e18），把闭单当已完结会**假绿**。行级量判定与既有 `pending_delivery_query` 同源，
两个场景可对照。

### 4.2 核心指标：订单到货率为主，量级率为参考

| 指标 | 角色 | 定义 | 本期实测 |
|---|---|---|---|
| **订单到货率** | **主指标** | 全到货订单数 / 总订单数 | **82.25%**（3,984 / 4,844） |
| 量级加权到货率 | 参考 | `SUM(RCPQTYSTU_0) / SUM(QTYSTU_0)` | 40.51% |
| 订单三态 | 辅指标 | 全到货 / 部分到货 / 未到货 订单计数 | 3,984 / 448 / 412 |

**为什么量级率不能当主指标（探针实证，重要）**：

本期 19,910 行中，「完全未到货」行（`RCPQTYSTU_0 = 0`）有 2,251 行，但它们贡献了
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

- **锚定字段**：下单日期 `ORDDAT_0`（与需求「某个时间段的订单」一致，且与既有场景同口径）。
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

本期 2,251 条完全未到货行中，**2,072 条的预期到货日已过**（`EXTRCPDAT_0 <= 窗口末`），
仅 179 条尚未到期（92%）。因此「未到货」行绝大多数是**真逾期**，不是「还没到期的正常在途」。

订单三态中的「未到货」因此可直接理解为「基本已逾期」，但**卡上仍用「未到货」措辞**
（与需求一致），不擅自改成「逾期」—— 逾期判定属于「待到货 / 逾期未到」场景的职责，
两个场景措辞边界要清晰。

## 5. 架构

### 5.1 组件

| 组件 | 类型 | 位置 | 职责 |
|---|---|---|---|
| `delivery_tracking_query` | 工具 | `backend/app/services/agent_tools_procurement.py` + `seed_agent_tool_configs.py` | 概览 + 双层明细取数 |
| `DELIVERY_TRACKING_AGENT` | Agent | `backend/scripts/seed_agents.py` + `seed_procurement_agent_profiles.py` | 编排与输出纪律 |
| `delivery_tracking` | 场景配置 | `backend/scripts/seed_copilot_scenarios.py` | 卡片墙 / 槽位 / 模板 / 渲染器绑定 |
| `DeliveryTrackingResultCard` | 前端组件 | `frontend/src/components/copilot/` | 结果卡（概览 + 双 Tab） |
| `pending_delivery` 渲染器扩展 | 前端 | `PendingDeliveryResultTabs.tsx` | 分发到新卡 |

### 5.2 为什么不复用 `pending_delivery_query`

两者语义正交：

- 待到货：**欠交视角**（只关心没到的），输出催货建议；
- 到货跟踪：**到货视角**（关心整体到货率与已到/未到分布），输出到货达成情况。

共用部分通过抽公共件实现，不共用 SQL 模板：
`_PendingDialect`（方言碎片）、`PendingDeliveryFilters`（过滤/窗口）的思路可提取，
但窗口语义不同（到货跟踪要 24 日锚点），故新写。

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
解析出 `enterpriseCode` 后作为 `BPSNUM_0 IN (...)` 过滤值。

**多命中消歧**：全名模糊命中多个供应商时抛 `NotFoundError` 并列候选
（与上一轮「供应商风险速览」一致），由规划器/用户消歧，**绝不静默取第一个**。

注意：ERP 侧 `BPSNUM_0` 的名称（`ODS_ERP_BPSUPPLIER.BPSNAM_0`）与 MDM `name` 可能不同，
展示以 MDM `name` 为准，回退 ERP 名。

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
| 供应商多命中 | `NotFoundError` 列候选（「请用编码或企业键指定」） |
| 供应商不存在 | `NotFoundError` |
| 窗口内无数据 | 正常返回，summary 全 0 + 说明性 answer（「该期间无下单记录」） |
| 非法 `anchorDay`（<1 或 >28） | `ValidationError`（fail-closed，不静默纠正） |
| 非法日期格式 | `ValidationError`（沿用 `PendingDeliveryFilters.__post_init__`） |
| 数据源不存在/停用 | `ConflictError`（沿用 `_loadActiveDatasource`） |
| 脏数据行 | 剔除并在 `summary.excludedRows` 显式回报 |

## 8. 测试计划

### 8.1 后端单测

- SQL 模板生成：窗口谓词三形态（`monthsBack` / `dateFrom+dateTo` / 24 日锚点），
  断言边界为左闭右开 `>= AND <`；
- 24 日锚点日期计算：跨月（11-24 → 12-24）、跨年（12-24 → 次年 1-24）、2 月（28 日上限）；
- 订单三态判定：全到 / 部分 / 未到（含「部分到货后全部到齐」归入全到货）；
- 供应商码解析：编码 / 企业键 / 全名 / 简称 / 多命中消歧；
- 方言分叉：Oracle 与 MySQL 两种 `tail` 语法（沿用 `_PendingDialect`）。

### 8.2 后端集成测试（真实 PostgreSQL + 真实 API 链路）

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

### 8.4 前端测试

- `parseDeliveryTrackingStep`：解析工具 observation；坏 JSON / 非本工具 ⇒ `null`；
- 结果卡：主指标（订单到货率）为大号数字、量级率为次要行且带口径说明、
  三态计数、双 Tab 切换、物料行明细渲染。

### 8.5 实测验收（本地栈）

- 用真实 Oracle 数据跑通：默认 12 月窗口 → 概览 + 供应商表 + 订单明细；
- 24 日锚点本期窗口 → 4,844 张订单，与 8.3 基线逐项对齐；
- 多供应商筛选（B125 / D1 / C1）；
- 供应商简称/名称检索（走解析阶梯）；
- 明细行的物料名非空（`ODS_ERP_ITMMASTER` JOIN，缺失率应 ≈ 0）。

## 9. 不做（YAGNI）

- 不改既有 `pending_delivery_query` 的表层（保持源层），避免回归既有已验收场景；
- 不做双层数据源切换参数（当前只有数仓层一个真实需求）；
- 不做 `generic_table` 通用渲染器（本场景专用卡即可，避免过度抽象）；
- 不做跨 schema JOIN（数仓层字典已齐）；
- 不做订单头状态过滤（行级量判定已足够，且避开闭单假绿）。

## 10. 风险

| 风险 | 缓解 |
|---|---|
| 与「待到货」场景表层不同，用户可能混淆 | 卡片标注数据层；两场景文案区分（到货达成 vs 欠交催货） |
| 数仓层 `BPSNUM_0` 与 MDM 码域未来可能漂移 | 解析统一走 MDM；过滤前校验码在 MDM 域内 |
| 订单明细行数膨胀 | `limit` 上限 50（沿用 `_MAX_LIMIT`），概览层不受影响 |
| 24 日锚点与「自然月」两套窗口并存易混 | 卡上显示实际 `window.dateFrom/dateTo`，不只显示口径名 |

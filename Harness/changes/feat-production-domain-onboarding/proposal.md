# 生产域接入指导方案（Proposal）

> 基于 `docs/20260828-new-module-data-standard.md` 三层准入标准，输出生产域（Manufacturing / Production）模块的接入方案。
> 范围：Sage X3 生产工单、工序领料、工序报工、完工入库。

## 0. 当前生产域现状

| 已有 | 类名 | source_table | 说明 |
|---|---|---|---|
| ✅ 工艺路线 | `RoutingOperation` | `ROUOPE` | 主数据：工序、工作中心、工时 |
| ✅ 物料清单 | `BOM` / `BOMDetail` | `BOM` / `BOMD` | 主数据：组件、用量、替代 BOM |
| ✅ 物料地点 | `ItemFacility` | `ITMFACILIT` | 主数据：制造仓库、提前期、库存参数 |
| ✅ 地点 | `Facility` | `FACILITY` | 主数据：基地、工厂标志 |

| **缺失**（本次接入） | 建议类名 | 典型 Sage X3 表 | 说明 |
|---|---|---|---|
| ❌ 生产工单主表 | `ProductionOrder` | `MFGHEAD` | 工单号、状态、计划/实际数量、计划日期 |
| ❌ 工单物料 | `ProductionOrderMaterial` | `MFGMAT` | 工单领料计划：物料、用量、批次、库位 |
| ❌ 工单工序 | `ProductionOrderOperation` | `MFGOPE` | 工单工序：工序号、计划/实际工时、成本 |
| ❌ 完工入库（产成品） | `ProductionReceipt` | `MFGFOO` | 完工入库表头/明细 |
| ❌ 工序转移 / 报工 | `OperationReport` | `MFGLAB` 或 `MFGTIM` | 工序转移、工时、报工人 |

## 1. 数据源层（运行时层）

### 1.1 数据源确认

Sage X3 生产库同 SRM/WMS 库（Oracle ZJTH 模式），无需新注册数据源。若需单独权限：

```bash
POST /api/v1/datasource
{
  "name": "sage_x3_mfg",
  "type": "oracle",
  "connection_url": "oracle+oracledb://user:pass@host:1521/?service_name=X3",
  "username": "ZJTH",
  "encrypted_password": "<Fernet>",
  "is_read_only": true,
  "is_default": false
}
```

- `is_read_only=True` 硬约束
- `username=ZJTH` 即 schema owner（Oracle 方言 schema 前缀）
- 仅 SELECT 经 SQL Guard 校验

## 1.5 数据指标标准规范（AI 友好的数据契约层）

> 本节是「生产域数据字典」，按 **数据分类 → 命名 → 引用 → 业务语义** 四个维度规范化所有字段、对象、关系。LLM/AI 应用通过这套规范**学习数据的语义层级**，无需重读整张表就能精确选表/选列/选路径。
>
> 适用对象：所有接入 qa-system 的物理表与本体类。

### 1.5.1 数据分类体系（Data Taxonomy）

生产域所有数据按**生命周期 + 变更频率 + 业务角色**划分为六大类，每类有明确的命名规范、本体建模规则、SQL 行为预期。

| # | 类别 | 定义 | 典型例子 | LLM 期望行为 |
|---|---|---|---|---|
| 1 | **主数据（Master Data）** | 长期稳定、被引用的核心实体数据 | `ITMMASTER` 物料、`BPSUPPLIER` 供应商、`FACILITY` 地点、`ROUOPE` 工艺工序 | 可作为 JOIN 锚点；查询时常被 `WHERE` 引用；NL2SQL 优先选 |
| 2 | **业务单据（Transaction Document）** | 业务事件产生的流水单据，含表头/明细 | `MFGHEAD/MFGMAT/MFGOPE/MFGFOO` 生产工单及其明细；`PORDER/PORDERQ` 采购订单 | GROUP BY 时间/状态/维度的核心；聚合查询首选；必须含状态机 |
| 3 | **参考数据（Reference Data）** | 枚举/代码表，值域有限且稳定 | 状态码 `MFGSTA_0`（1~5）、单位 `UOM`、币种 `CUR`、工厂类型 | NL2SQL 过滤条件常用；应在 `desc` 显式列出枚举值 |
| 4 | **指标数据（Metric Data）** | 派生计算或预聚合的数值，可直接查询 | `ontology_metric` 表（完工率/报废率/按时率）、预聚合宽表 | 优先使用指标而非重算；formula 必填 |
| 5 | **关系数据（Relationship Data）** | 描述实体间多对多关联 | `MFGOPE.ROUALT_0 ↔ ROUOPE.ROUALT_0`（工序实例 ↔ 主数据）、BOM 多层展开 | JOIN 路径依赖；必填在 `BUSINESS_JOINS` |
| 6 | **日志/审计数据（Log/Audit Data）** | 系统自动记录，技术性、变更追踪 | `MFGLAB` 工序报工流水、`wms_inventory_log` 库存流水 | NL2SQL 通常不直接查；过滤时带时间窗；不进本体 |

**分类规则**（判定流程）：

```
是否为系统自动产生 / 高频变更 / 无业务主键？
  → 是 → 日志/审计数据
  → 否 → 是否为预计算或公式？
    → 是 → 指标数据
    → 否 → 是否为枚举/代码表？
      → 是 → 参考数据
      → 否 → 是否描述实体间关系？
        → 是 → 关系数据
        → 否 → 是否长期稳定被引用？
          → 是 → 主数据
          → 否 → 业务单据
```

### 1.5.2 表定义规范（Table Definition Spec）

每张物理表接入前必须输出 **Table Spec Card**（一张卡），包含 12 项必填：

| # | 字段 | 要求 | 示例（MFGHEAD） |
|---|---|---|---|
| 1 | **表物理名（source_table）** | 与 DB 一致；Oracle 全大写、PG/MySQL 小写 | `MFGHEAD` |
| 2 | **表中文名（class_alias）** | 业务名，2~6 字，无特殊字符 | `生产工单` |
| 3 | **数据分类** | 六类之一（见 1.5.1） | **业务单据** |
| 4 | **主键列** | 单字段或复合，用 `pk=True` 标注 | `MFGNUM_0`（单字段） |
| 5 | **数据语义角色** | 表头 / 表体 / 主数据 / 关系桥 / 汇总 | 表头 |
| 6 | **生命周期** | 长期稳定 / 短期流水 / 一次性事件 | 短期流水（创建到关闭即归档） |
| 7 | **典型行数** | 实际生产环境的预估 | 5000~10000 张/月 |
| 8 | **写入频率** | 高频（实时）/ 中频（日）/ 低频（月） | 中频（日 50~200 单） |
| 9 | **是否被下游表引用** | 是 / 否，列出关键引用方 | 被 MFGMAT、MFGOPE、MFGFOO 引用 |
| 10 | **关联业务事件** | 该表记录的业务事件 | 工单创建/下达/完工/关闭 |
| 11 | **业务单据号字段** | 单据号列名（如 `MFGNUM_0`） | `MFGNUM_0` |
| 12 | **核心本体描述** | 50~200 字，含业务场景与典型查询 | 「生产工单主表，记录工单号、产品、计划数量、计划开工/完工日期、实际数量、状态等。」 |

**Table Spec Card** 输出位置：`backend/scripts/diag_<table>_schema.py` 同时产出 schema + card，存到 `docs/data-dictionary/<source_table>.md`。

### 1.5.3 属性定义规范（Property Definition Spec）

每个字段接入本体时必须输出 **Property Spec Card**（每字段一张），包含 11 项必填：

| # | 字段 | 要求 | 示例（MFGHEAD.MFGNUM_0） |
|---|---|---|---|
| 1 | **物理列名（alias）** | 与 DB 一致，**全大写**（Oracle） | `MFGNUM_0` |
| 2 | **中文名（name）** | 业务名 | `工单号` |
| 3 | **数据类型（type）** | 五选一 | `STRING` |
| 4 | **业务分类** | 标识 / 维度 / 度量 / 时间 / 状态 / 备注 / 扩展 / 技术（见下） | **标识（PK）** |
| 5 | **是否主键（pk）** | True/False | True |
| 6 | **是否外键（fk）** | 目标类 source_table | 无 |
| 7 | **业务别名列表（aliases）** | 消歧缩写数组 | `["生产工单", "工单"]` |
| 8 | **数据质量约束** | 非空率 / 唯一性 / 精度 / 枚举值 | 非空率 100%；唯一 |
| 9 | **业务不变式** | 该字段参与的守恒 / 状态依赖 / 口径 | 工单号生成规则：`MFG-YYYY-####` |
| 10 | **典型查询模式** | 常见 WHERE / GROUP BY 用法 | `WHERE MFGNUM_0 = 'XXX'`、`GROUP BY MFGNUM_0` |
| 11 | **本体描述（desc）** | 含单位 / 枚举 / 业务场景 | 「生产工单唯一标识，格式 `MFG-YYYY-####`，全局唯一」 |

**字段业务分类（8 类）**：

| 分类                 | 含义                  | 典型例子                               |
| ------------------ | ------------------- | ---------------------------------- |
| **标识（Identifier）** | PK / 业务唯一键          | `MFGNUM_0 / BPSNUM_0 / ITMREF_0`   |
| **维度（Dimension）**  | GROUP BY / 切片维度     | `MFGFCY_0 / BPSNUM_0 / ITMREF_0`   |
| **度量（Measure）**    | 可聚合数值               | `QTY_0 / LINAMT_0 / ACTTIM_0`      |
| **时间（Time）**       | 日期 / 时间戳            | `STDSTRDAT_0 / ACTENDDAT_0`        |
| **状态（Status）**     | 状态机字段               | `MFGSTA_0 / APPFLG_0 / YPTHFLG_0`  |
| **备注（Remark）**     | 自由文本                | `YNOTE_0 / TEX1_0`                 |
| **扩展（Extension）**  | DIE/CCE/INVDTA 等扩展槽 | `DIE_0 / CCE_0`                    |
| **技术（Technical）**  | 系统自动字段              | `EXPNUM_0 / AUUID_0 / CREDATTIM_0` |

### 1.5.4 引用关系规范（Reference Relationship Spec）

物理表之间的引用关系分为 **4 种类型**，每种在本体中的标注方式不同：

| # | 引用类型 | 定义 | 本体标注 | 示例 |
|---|---|---|---|---|
| 1 | **标识引用（Identifier Reference）** | 子表通过 PK 引用父表 | `BUSINESS_JOINS` + 子表 `fk` | `MFGMAT.MFGNUM_0 → MFGHEAD.MFGNUM_0` |
| 2 | **业务引用（Business Reference）** | 子表引用主数据（物料、供应商、地点） | `BUSINESS_JOINS` + 子表 `fk` | `MFGMAT.ITMREF_0 → ItemMaster.ITMREF_0` |
| 3 | **维度引用（Dimension Reference）** | 表引用枚举/参考数据 | `desc` 注明枚举来源 | `MFGHEAD.MFGSTA_0` ∈ `MFGStatusEnum` |
| 4 | **聚合引用（Aggregation Reference）** | 指标/汇总表引用明细表 | `formula` 中显式声明 | `ProductionKPI.ACTQTY = SUM(MFGHEAD.ACTQTY_0)` |

**4 类引用必填清单**（生产域示例）：

```
标识引用：
  ProductionOrderOperation.MFGNUM_0 → ProductionOrder.MFGNUM_0
  ProductionOrderMaterial.MFGNUM_0 → ProductionOrder.MFGNUM_0
  ProductionReceipt.MFGNUM_0       → ProductionOrder.MFGNUM_0

业务引用：
  ProductionOrder.ITMREF_0          → ItemMaster.ITMREF_0
  ProductionOrder.MFGFCY_0          → Facility.FCY_0
  ProductionOrderOperation.WCR_0    → WorkCenter.WCR_0  (新主数据)
  ProductionOrderOperation.LBRUSR_0 → User.USR_0       (新主数据)

维度引用：
  ProductionOrder.MFGSTA_0 → MFGStatusEnum.{1,2,3,4,5}
  ProductionOrder.MFGTYP_0 → MFGTypeEnum.{普通,返工,委外}
  ProductionOrderOperation.OPESTA_0 → OpeStatusEnum.{1,2,3,4}

聚合引用：
  Metric.完工率.formula = SUM(ACTQTY_0) / SUM(STDQTY_0)   ← 引用 ProductionOrder
  Metric.报废率.formula = SUM(SCRQTY_0) / (SUM(ACTQTY_0)+SUM(SCRQTY_0)) ← 同上
```

### 1.5.5 业务单据规范（Transaction Document Spec）

业务单据（生产域含 5 类）必须满足 **6 项硬规范**：

| # | 规范 | 要求 |
|---|---|---|
| 1 | **表头-明细结构** | 表头与明细必须拆表（如 `MFGHEAD` + `MFGMAT`），不允许单表混合 |
| 2 | **单据号生成规则** | 单据号格式写进 `desc`，含前缀+年份+流水（如 `MFG-YYYY-####`） |
| 3 | **状态机完整性** | 状态字段必进本体；枚举值 100% 写进 `desc`；合法转移路径写进 `desc` |
| 4 | **表头-明细守恒** | 表头合计字段 = SUM(明细行)；守恒校验脚本必过 |
| 5 | **下游单据关联** | 列出该单据可能触发的下游单据（如 MFGHEAD 完工 → MFGFOO 入库）；关联字段写进本体 |
| 6 | **时间维度齐全** | 至少有：创建日期、业务日期、计划日期、实际日期；写进 `desc` |

**生产域业务单据清单**：

| 单据 | 表头 | 明细 | 状态字段 | 关键守恒 |
|---|---|---|---|---|
| **生产工单** | `MFGHEAD` | `MFGMAT` + `MFGOPE` | `MFGSTA_0` (1~5) | TOTLINQTY = SUM(MFGMAT.QTY_0) |
| **完工入库** | `MFGFOO` | `MFGFOOD` (若有) | `FOOSTA_0` (1~4) | TOTQTY = SUM(明细.QTY_0) |
| **工序报工** | — | `MFGLAB` | `LABSTA_0` | SUM(MFGLAB.ACTQTY_0) ≤ MFGHEAD.ACTQTY_0 |
| **工序转移** | — | `MFGTIM` | `TIMSTA_0` | ACTEND ≥ ACTSTR（同工序） |

### 1.5.6 主数据规范（Master Data Spec）

主数据接入本体时必须满足 **5 项硬规范**：

| # | 规范 | 要求 |
|---|---|---|
| 1 | **唯一标识** | PK 必填且全局唯一；自然键（如 `BPSNUM_0`）进本体 |
| 2 | **长期稳定** | 主体属性变更率低（年度 < 10%）；删除应走软删除（`DELFLG_0`）而非物理删除 |
| 3 | **完整属性集** | 至少有：编码、名称、状态、创建日期、修改日期 |
| 4 | **跨域引用清晰** | 在 `desc` 注明该主数据被哪些业务单据引用（如「物料被采购订单/生产工单/销售订单引用」） |
| 5 | **本体建模标识** | 在 `seed_ontology.PROPERTIES` 中标记 `pk=True`，并加 `master_data=True` 业务标签 |

**生产域主数据新增建议**（除 ROUOPE/BOM 外）：

| 主数据 | 物理表 | 必填字段 |
|---|---|---|
| 工作中心 | `WORKCENT` (待确认) | `WCR_0` PK、`DES_0` 名称、`FCY_0` 工厂 |
| 班次 | `SHIFTS` | `SHIFTID_0` PK、`STR_0` 开始时间、`END_0` 结束时间 |
| 操作工 | `USER` | `USR_0` PK、`NAM_0` 姓名、`ROL_0` 角色 |
| 班组 | `TEAM` | `TEAMID_0` PK、`DES_0` |

### 1.5.7 参考数据规范（Reference Data Spec）

参考数据（枚举/代码表）必须满足 **4 项硬规范**：

| # | 规范 | 要求 |
|---|---|---|
| 1 | **物理存储** | 优先独立代码表（`MFGStatusEnum`），其次字典项（`TSICOD_0`），最次用字段常量（如 `MFGSTA_0=1`） |
| 2 | **枚举值 100% 列示** | 所有取值必须写进字段的 `desc`，格式：`1=草稿 2=审核 3=下达 4=完工 5=关闭` |
| 3 | **新增值守约** | 新增枚举值必须同步更新 `desc` + 验证脚本（否则 `verify_enums.py` 报错） |
| 4 | **本体建模标记** | 在 `seed_ontology` 中给枚举字段加 `is_reference=True` 业务标签 |

**生产域参考数据清单**：

| 枚举 | 取值 | 物理位置 |
|---|---|---|
| `MFGSTA_0` 工单状态 | 1=计划 2=下达 3=在制 4=完工 5=关闭 | `MFGHEAD.MFGSTA_0` |
| `MFGTYP_0` 工单类型 | 1=普通 2=返工 3=委外 4=试制 | `MFGHEAD.MFGTYP_0` |
| `OPESTA_0` 工序状态 | 1=未开始 2=在制 3=完成 4=跳过 | `MFGOPE.OPESTA_0` |
| `FOOSTA_0` 入库状态 | 1=草稿 2=已入库 3=已过账 4=关闭 | `MFGFOO.FOOSTA_0` |
| `SCRREA_0` 报废原因 | 1=质量 2=设备 3=工艺 4=物料 9=其他 | `MFGOPE.SCRREA_0` |

### 1.5.8 指标数据规范（Metric Data Spec）

指标数据接入 `ontology_metric` 表时必须满足 **7 项硬规范**：

| # | 规范 | 要求 |
|---|---|---|
| 1 | **命名规范** | 指标名必须是中文业务名；含「率 / 占比 / 比例 / 百分比 / ratio / percent / share / pct」→ `formula` 必填（**`validatePlan` 拦截**） |
| 2 | **公式明确** | `formula` 必须是合法 SQL 表达式（`SUM / AVG / COUNT / 窗口函数` 等）；不能写自然语言 |
| 3 | **分母零保护** | 含除法的 formula 必须用 `NULLIF(分母, 0)` 或 `CASE WHEN ... = 0 THEN 0` |
| 4 | **属性引用真实** | `formula` 引用的属性必须在 `selectedProperties` 中；否则 `validatePlan` 报错 |
| 5 | **维度声明** | 指标必须声明可用的 GROUP BY 维度（如「工单完工率可按工厂/产品/班组分组」） |
| 6 | **业务口径文档化** | 在 `Harness/wiki/business-domain.md` 列出每个指标的口径与边界（如「完工率含报废 / 不含返工」） |
| 7 | **本体建模标记** | 在 `seed_ontology.METRICS` 中定义（与 CLASSES / PROPERTIES 同级） |

**生产域核心指标预置**（10 个）：

| 指标名 | formula | 维度 | 业务口径 |
|---|---|---|---|
| `工单完工率` | `SUM(ACTQTY_0) / NULLIF(SUM(STDQTY_0), 0)` | 工厂/产品/月份 | 含合格+报废；不含已关闭 |
| `工单准时率` | `SUM(CASE WHEN ACTENDDAT_0 <= STDENDDAT_0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*)` | 工厂/产品 | 实际完工 ≤ 计划完工 |
| `工序按时率` | `SUM(CASE WHEN ACTEND <= STDEND THEN 1 ELSE 0 END) * 1.0 / COUNT(*)` | 工作中心/工序 | 同上 |
| `报废率` | `SUM(SCRQTY_0) / NULLIF(SUM(ACTQTY_0) + SUM(SCRQTY_0), 0)` | 工厂/产品/工序 | 报废/(合格+报废) |
| `返工率` | `SUM(CASE WHEN MFGTYP_0=2 THEN 1 ELSE 0 END) * 1.0 / COUNT(*)` | 工厂/产品 | 返工工单/总工单 |
| `工序产能利用率` | `SUM(ACTTIM_0) / NULLIF(SUM(STDTIM_0 * ACTQTY_0 / STDQTY_0), 0)` | 工作中心/工序 | 实际工时/标称工时 |
| `平均工序工时` | `AVG(ACTTIM_0)` | 工作中心/工序 | — |
| `工单平均周期` | `AVG(ACTENDDAT_0 - ACTSTRDAT_0)` | 工厂/产品 | 单位：天 |
| `物料齐套率` | `SUM(实际齐套物料数) * 1.0 / SUM(应齐套物料数)` | 工单/产品 | 实际领料/计划领料 |
| `设备故障停机率` | `SUM(DOWNTIM_0) / NULLIF(SUM(DOWNTIM_0 + RUNTIME_0), 0)` | 工作中心 | — |

### 1.5.9 AI 学习路径设计（AI Onboarding）

让 LLM / AI 应用系统性地**学习这套规范**，分三阶段：

**阶段 1：被动摄取（系统 Prompt 注入）**

在 NL2SQL System Prompt 中固定注入以下内容（写入 `seed_business_rules.py`）：

```
【数据分类体系】本系统数据分 6 类：主数据 / 业务单据 / 参考数据 / 指标数据 / 关系数据 / 日志数据。
每类字段的 SQL 行为预期不同：主数据作为 JOIN 锚点、业务单据作为 GROUP BY 聚合源、参考数据用于 WHERE 过滤、指标数据直接使用。
【字段业务分类】每字段分 8 类：标识 / 维度 / 度量 / 时间 / 状态 / 备注 / 扩展 / 技术。
【引用关系 4 类】标识引用 / 业务引用 / 维度引用 / 聚合引用。
【业务单据规范】表头与明细拆表；单据号格式 MFG-YYYY-####；状态机字段必填；表头-明细守恒。
【主数据规范】PK 全局唯一；长期稳定；删除走软删除。
【参考数据规范】枚举值必须穷举写进 desc；新增值必须更新本体。
【指标数据规范】含「率/占比/比例/百分比」关键词的 formula 必填；分母零保护；属性引用真实。
```

**阶段 2：主动推理（Few-shot 示例）**

在 Prompt 中加入 3~5 条典型查询的 Few-shot，每条标注使用了哪类数据 / 哪类引用：

```
问：「3 月份各工厂的工单完工率？」
答：SELECT FCY, SUM(ACTQTY) / SUM(STDQTY) AS 完工率
    FROM ProductionOrder WHERE ACTENDDAT BETWEEN ... GROUP BY FCY
    [数据分类：业务单据] [引用：聚合引用] [指标：完工率]
```

**阶段 3：自我纠错（验证脚本）**

- `verify_enums.py` — 枚举值 100% 与 `desc` 一致
- `verify_invariants.py` — 守恒 / 状态依赖 / FK 有效性
- `verify_metric_formula.py` — 指标公式属性引用真实 + 分母零保护
- `verify_reference_integrity.py` — 4 类引用全部声明

**学习效果验证**（5 项 AI 能力评估）：

| 能力 | 评估方式 | 合格线 |
|---|---|---|
| 选表准确率 | 50 个真实问法 → 期望本体类 | ≥ 90% |
| 选列准确率 | 同上 → 期望本体属性 | ≥ 85% |
| 派生指标理解 | 50 个占比类问法 → formula 必填 | ≥ 95% |
| 状态机理解 | 状态过滤类问法 → 正确枚举值 | ≥ 95% |
| 跨域推理 | 跨域问法 → 正确 JOIN 路径 | ≥ 85% |

### 1.5.10 数据契约总览表（生产域示例）

下表是生产域 **Table Spec Card + Property Spec Card + Reference + Metric** 的汇总视图（一表看懂全貌）：

| 类名（中文） | source_table | 数据分类 | 业务角色 | PK | 关键引用 | 关键指标 |
|---|---|---|---|---|---|---|
| `生产工单` | `MFGHEAD` | 业务单据 | 表头 | `MFGNUM_0` | ITMMASTER / FACILITY | 完工率、准时率 |
| `工单物料` | `MFGMAT` | 业务单据 | 表体 | `(MFGNUM_0, MATLIN_0)` | MFGHEAD / ITMMASTER | 齐套率 |
| `工单工序` | `MFGOPE` | 业务单据 | 表体 | `(MFGNUM_0, OPENUM_0)` | MFGHEAD / ROUOPE / WorkCenter | 按时率、工时利用率 |
| `完工入库` | `MFGFOO` | 业务单据 | 表头 | `FOONUM_0` | MFGHEAD / FACILITY | 入库及时率 |
| `工序报工` | `MFGLAB` | 日志数据 | 流水 | `(MFGNUM_0, OPENUM_0, REPNUM_0)` | MFGOPE / User | — |
| `工作中心` | `WORKCENT` | 主数据 | — | `WCR_0` | FACILITY | — |
| `操作工` | `USER` | 主数据 | — | `USR_0` | — | — |
| `工单状态枚举` | (in MFGHEAD) | 参考数据 | 字段常量 | — | — | — |
| `工序状态枚举` | (in MFGOPE) | 参考数据 | 字段常量 | — | — | — |
| `指标库` | (ontology_metric) | 指标数据 | — | — | — | 完工率/准时率/按时率/报废率等 10 个 |

> **这张表是 LLM 选表的「鸟瞰图」**：看到「工单」二字即可联想到上表 `MFGHEAD`，再按数据分类 → 关键引用 → 关键指标三步走。

### 1.2 物理表 schema 抽取（步骤 1）

执行 `backend/scripts/diag_<table>_schema.py`（参考 `diag_ppricconf_schema.py` 模板），输出：

| 表 | 关键检查 |
|---|---|
| `MFGHEAD` | PK：`MFGNUM_0` 单字段；状态字段 `MFGSTA_0` 枚举值；计划/实际日期字段 |
| `MFGMAT` | PK：`(MFGNUM_0, MATLIN_0)` 复合；外键 → `MFGHEAD.MFGNUM_0` + `ITMMASTER.ITMREF_0` |
| `MFGOPE` | PK：`(MFGNUM_0, OPENUM_0)` 复合；外键 → `MFGHEAD.MFGNUM_0` + `ROUOPE.OPENUM_0`（共用工序主数据） |
| `MFGFOO` | PK：`FOONUM_0`；外键 → `MFGHEAD.MFGNUM_0`；与 `PRECEIPT` 收货单区分（产成品入库） |

## 2. 本体建模层（最关键）

### 2.1 5 个新类（步骤 2）

| class_name | class_alias | source_table | description |
|---|---|---|---|
| `ProductionOrder` | 生产工单 | `MFGHEAD` | 「生产工单主表，记录工单号、产品、计划数量、计划开工/完工日期、实际数量、状态、工序路径与领料清单。」 |
| `ProductionOrderMaterial` | 工单物料 | `MFGMAT` | 「生产工单物料清单，记录工单所需组件物料的计划用量、批次、库位、实际领料数量与废品数量。」 |
| `ProductionOrderOperation` | 工单工序 | `MFGOPE` | 「生产工单工序实例，记录每张工单下每道工序的计划与实际工时、成本、报工状态及工作中心。」 |
| `ProductionReceipt` | 完工入库 | `MFGFOO` | 「产成品入库表，记录生产工单完工后的产成品入库单号、日期、仓库、批号与数量。」 |
| `OperationReport` | 工序报工 | `MFGLAB` | 「工序报工/转移流水，记录每道工序的实际开始/结束时间、报工人、合格数量与报废数量。」 |

### 2.2 关键属性建模（步骤 2）

#### ProductionOrder（MFGHEAD）核心属性

| name | alias | type | pk | fk | aliases | desc |
|---|---|---|---|---|---|---|
| 工单号 | `MFGNUM_0` | STRING | ✅ | — | ["生产工单", "工单"] | 工单唯一标识 |
| 产品 | `ITMREF_0` | STRING |  | ItemMaster | ["成品", "产成品"] | 生产的成品物料编码 |
| 工厂 | `MFGFCY_0` | STRING |  | Facility | — | 生产工厂编码 |
| 工单类型 | `MFGTYP_0` | STRING |  | — | ["订单类型"] | 普通/返工/委外等 |
| 状态 | `MFGSTA_0` | STRING |  | — | — | "枚举：1=计划 2=下达 3=在制 4=完工 5=关闭" |
| 计划开工日期 | `STDSTRDAT_0` | DATETIME |  | — | ["计划开始日期"] | — |
| 计划完工日期 | `STDENDDAT_0` | DATETIME |  | — | ["计划结束日期"] | — |
| 实际开工日期 | `ACTSTRDAT_0` | DATETIME |  | — | ["实际开始日期"] | — |
| 实际完工日期 | `ACTENDDAT_0` | DATETIME |  | — | ["实际结束日期"] | — |
| 计划数量 | `STDQTY_0` | DECIMAL |  | — | — | — |
| 实际数量 | `ACTQTY_0` | DECIMAL |  | — | — | — |
| 报废数量 | `SCRQTY_0` | DECIMAL |  | — | ["废品数量"] | — |

> **命名冲突预警**：`MFGNUM_0` 字段名与采购域 `POHNUM_0` 同模式「单据号」，但业务语义不同 → 必须靠 `aliases` 消歧。

#### ProductionOrderOperation（MFGOPE）核心属性

| name | alias | type | pk | fk | aliases | desc |
|---|---|---|---|---|---|---|
| 工单号 | `MFGNUM_0` | STRING | ✅ | ProductionOrder | — | 工单外键 |
| 工序号 | `OPENUM_0` | INT | ✅ | — | ["工序序号"] | 工单内工序序号 |
| 工序主数据 | `ROUALT_0` | STRING |  | RoutingOperation | ["工艺替代号"] | 对应 ROUOPE 主数据 |
| 工作中心 | `WCR_0` | STRING |  | — | ["加工中心"] | — |
| 计划工时 | `STDTIM_0` | DECIMAL |  | — | — | 单位：小时 |
| 实际工时 | `ACTTIM_0` | DECIMAL |  | — | — | 单位：小时 |
| 计划成本 | `STDCST_0` | DECIMAL |  | — | — | — |
| 实际成本 | `ACTCST_0` | DECIMAL |  | — | — | — |
| 工序状态 | `OPESTA_0` | STRING |  | — | — | "枚举：1=未开始 2=在制 3=完成 4=跳过" |
| 报工人 | `LBRUSR_0` | STRING |  | — | ["操作工"] | — |

#### 派生指标示例（formula 必填）

| 指标名 | formula | 含义 |
|---|---|---|
| `工单完工率` | `SUM(ACTQTY_0) / SUM(STDQTY_0) OVER ()` | 实际/计划（占比 → 关键词触发必填） |
| `工序按时率` | `SUM(CASE WHEN ACTENDDAT_0 <= STDENDDAT_0 THEN 1 ELSE 0 END) / COUNT(*)` | 按时完工工序数 / 总工序数 |
| `报废率` | `SUM(SCRQTY_0) / (SUM(ACTQTY_0) + SUM(SCRQTY_0))` | 报废 / (合格+报废) |

### 2.3 业务关联（JOIN）声明（步骤 2）

`BUSINESS_JOINS` 必填项：

```python
BUSINESS_JOINS = [
    # 工单 ↔ 工单物料
    ("ProductionOrderMaterial", "MFGNUM_0",
     "ProductionOrder", "MFGNUM_0"),
    # 工单 ↔ 工单工序
    ("ProductionOrderOperation", "MFGNUM_0",
     "ProductionOrder", "MFGNUM_0"),
    # 工单物料 ↔ 物料主数据
    ("ProductionOrderMaterial", "ITMREF_0",
     "ItemMaster", "ITMREF_0"),
    # 工单工序 ↔ 工艺工序主数据
    ("ProductionOrderOperation", "ROUALT_0+OPENUM_0",
     "RoutingOperation", "ROUALT_0+OPENUM_0"),
    # 完工入库 ↔ 工单
    ("ProductionReceipt", "MFGNUM_0",
     "ProductionOrder", "MFGNUM_0"),
    # 工序报工 ↔ 工单工序
    ("OperationReport", "MFGNUM_0+OPENUM_0",
     "ProductionOrderOperation", "MFGNUM_0+OPENUM_0"),
    # 工单 ↔ 产品（与 ItemMaster）
    ("ProductionOrder", "ITMREF_0",
     "ItemMaster", "ITMREF_0"),
]
```

> **复合主键 JOIN**：`ROUALT_0+OPENUM_0` 表达式拼接（`makeJoinKey` 支持），与现有 ROUOPE PK 保持一致。

### 2.4 系统字段排除（步骤 8）

显式不进入本体的字段（Excel `不需要=1` 列标记）：

```
EXPNUM_0 / AUUID_0 / CREDAT_0 / UPDDAT_0 / CREDATTIM_0 / UPDDATTIM_0
CREUSR_0 / UPDUSR_0
CREDATTIM_0
DIE_0 / CCE_0      -- 分析元槽位
INVDTA* / DISCRG*  -- 财务扩展
CLCAMT* / DCGVAL_* -- 扩展税基
```

## 3. SQL 生成层

### 3.1 方言配置

生产库同 SRM/WMS 为 Oracle，NL2SQL System Prompt 已支持，无需变更。

### 3.2 命名歧义消解（步骤 2）

| 风险 | 处置 |
|---|---|
| `MFGNUM_0`（工单号） vs `POHNUM_0`（订单号） | 别名 `["生产工单", "工单"]` + 描述含「生产工单主表」 |
| `ITMREF_0` 在多表（工单/工单物料/工单工序）都是物料 | 不进本体重复，通过所属类上下文消歧 |
| `OPENUM_0` 在 `ROUOPE`（主数据）与 `MFGOPE`（实例） | 复合 PK `ROUALT_0+OPENUM_0` + 描述明确 |
| `ACTQTY_0 / STDQTY_0` 与采购域 `QTYUOM_0` | 别名 + 字段类型 `DECIMAL` 避免整型截断 |

### 3.3 派生指标 formula 必填（步骤 6）

`工单完工率 / 报废率 / 工序按时率` 含「率 / 占比 / 比例」关键词 → `formula` 必填，触发 `validatePlan` 拦截与 `maxPlanAttempts=2` 重试。

## 4. 运行时与安全

### 4.1 性能与体量

- `MFGHEAD / MFGOPE / MFGMAT` 通常千万级行 → 在 `description` 注明「工单流水」
- `MFGHEAD` 的 `MFGSTA_0=4/5`（已关闭）建议在 prompt 引导 LLM 加 `WHERE MFGSTA_0 IN ('1','2','3')` 限定在制工单
- 单次查询走 `QUERY_ROW_LIMIT=5000`

### 4.2 多步上下文

- Top N 工单（如「最近 30 天完工率最低的 10 个工单」）→ `MFGNUM_0` 必须为 STRING（满足 `_detect_step_data_shape` 的 ENTITY_LIST 判定）
- 工序报工列表 → `OPENUM_0` INT 列但只要存在 STRING 列即可触发 ENTITY_LIST

## 5. 验收清单（10 项对应）

| # | 任务 | 输出 |
|---|---|---|
| 1 | 写 `scripts/diag_mfghead_schema.py` 等 5 个 schema 抽取脚本 | 字段、枚举、PK、FK 报告 |
| 2 | `seed_ontology.py` 新增 CLASSES / PROPERTIES / BUSINESS_JOINS | 幂等通过（`created/skipped` 计数合理） |
| 3 | 跑 `verify_ontology_consistency.py` | PG/Neo4j/Milvus 三处一致 |
| 4 | 写 5~10 个真实问法的手工验证集 | 见 §6 |
| 5 | `/api/v1/chat` 端到端（Oracle 只读） | SQL Guard 100% 通过 |
| 6 | 派生指标 `formula` 守约测试 | `test_formula_with_valid_properties_passes` 等 |
| 7 | 多步 Top N 工序 → 工单端到端 | 走 `[entity_list]` + `WHERE IN` |
| 8 | Excel `不需要=1` 标记技术字段 | `seed_ontology.py` 不含 `EXPNUM/AUUID/...` |
| 9 | 写 `Harness/changes/feat-production-domain-onboarding/summary.md` | 9 段模板（背景/设计/数据/接口/实现/测试/安全/部署/关联） |
| 10 | `uv run pytest --cov=app --cov-fail-under=80` | 覆盖率 ≥ 80% |

## 6. 真实问法验证集（5~10 条）

| # | 用户问法 | 期望走法 |
|---|---|---|
| 1 | 「最近 30 天有哪些生产工单未按计划完工？」 | 单表 MFGHEAD + 时间范围 + MFGSTA_0 状态 |
| 2 | 「3 月份 Top 10 报废率最高的工单是哪些？」 | MFGHEAD 聚合 + 派生指标 `报废率` |
| 3 | 「工单 WO-2025-001 的物料清单和领料情况？」 | 三表 JOIN：MFGHEAD + MFGMAT + ITMMASTER |
| 4 | 「比较 ZJ01 和 ZJ02 两个工厂的工序按时完工率」 | MFGOPE + MFGHEAD + Facility，分组聚合 |
| 5 | 「近 90 天各产品的计划数量 vs 实际数量对比」 | MFGHEAD 分组聚合 + 双轴图（自动出柱状/折线） |
| 6 | 「工单 WO-2025-001 的工序路径与每道工序的实际工时？」 | MFGHEAD + MFGOPE + ROUOPE 三表 JOIN |
| 7 | 「哪些物料的领料差异率超过 5%？」 | MFGMAT 聚合 + 派生指标 |
| 8 | 「按工作中心统计平均工序完成时长」 | MFGOPE 分组聚合 |
| 9 | 「5 月份返工工单数量」 | MFGHEAD 过滤 MFGTYP_0=返工 + 时间范围 |
| 10 | 「最近一周完工的产成品数量前 5 的工单」 | 多步：MFGFOO 列表 → Top 5 → 工单详情 |

## 7. 风险与依赖

| 风险 | 缓解 |
|---|---|
| 工单表数据量大（千万行） | 描述注明 + prompt 引导状态过滤；时间范围触发 scope-aware row limit |
| `MFGNUM_0` 与采购单号 `POHNUM_0` 同名风险 | `aliases` 消歧 + 描述明确 |
| 工单工序复合 PK 多列拼接 | `makeJoinKey` 已支持 `+` 表达式；测试守约 |
| 完工入库与采购收货同走 `PRECEIPT` | `ProductionReceipt` 类描述明确「产成品入库」+ 命名区分 |
| 工序报工（MFGLAB）数据稀疏 | 可选接入；若数据稀疏，先不上本体 |
| ROUOPE 与 MFGOPE 工序号混淆 | 复合 PK + 别名「工艺替代号」+ 「工序序号」 |

## 8. 实施分阶段

| Sprint | 范围 | 验收 |
|---|---|---|
| Sprint 1 | ProductionOrder 单表 + 5 个核心属性 + 单表查询测试 | 5 条单表问法通过 |
| Sprint 2 | + ProductionOrderOperation + ProductionOrderMaterial + 三表 JOIN | 3 条多表问法通过 |
| Sprint 3 | + ProductionReceipt + 完工入库链路 | 2 条入库问法通过 |
| Sprint 4 | + OperationReport（可选）+ 派生指标（完工率/报废率/按时率） | 公式必填守约 + 派生问法通过 |
| Sprint 5 | + Milvus 调优 + 端到端冒烟（oracle_receipt_schema.py 类比）+ 覆盖率达标 | 全部 10 项通过 |

## 9. 关联

- 标准依据：`docs/20260828-new-module-data-standard.md`
- 既有模式参考：`docs/20260814-PPRICCONF-ontology.md`（新增类完整范例）
- 既有变更参考：
  - `Harness/changes/feat-excel-not-needed-properties` — Excel 字段裁剪
  - `Harness/changes/fix-nl2sql-derived-metric-formula-required` — 派生指标必填
  - `Harness/changes/fix-supplier-price-dimensions` — 同名异义消歧
- 数据模型：`Harness/wiki/data-model.md`、`Harness/wiki/business-domain.md`
- NL2SQL 引擎：`Harness/wiki/nl2sql-engine.md`
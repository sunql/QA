# qa-system 数据指标标准规范（AI 友好的数据契约层）

> 版本：v1.0  · 日期：2026-08-31
> 适用对象：qa-system 全量新模块接入、本体建模、NL2SQL Prompt 工程、第三方 AI 应用集成。
> 设计目标：让 LLM / AI 应用无需重读整张物理表，即可按规范精确选表 / 选列 / 选路径。

---

## 一、数据分类体系（Data Taxonomy）

生产域及所有业务域的数据按**生命周期 + 变更频率 + 业务角色**划分为六大类，每类有明确的命名规范、本体建模规则、SQL 行为预期。

### 1.1 六大数据分类

| # | 类别 | 定义 | 典型例子 | LLM 期望行为 |
|---|---|---|---|---|
| 1 | **主数据（Master Data）** | 长期稳定、被引用的核心实体数据 | `ITMMASTER` 物料、`BPSUPPLIER` 供应商、`FACILITY` 地点、`ROUOPE` 工艺工序 | JOIN 锚点；常被 WHERE 引用；NL2SQL 优先选 |
| 2 | **业务单据（Transaction Document）** | 业务事件产生的流水单据，含表头 / 明细 | `MFGHEAD/MFGMAT/MFGOPE/MFGFOO`；`PORDER/PORDERQ` | GROUP BY 时间 / 状态 / 维度的核心；聚合首选；必须含状态机 |
| 3 | **参考数据（Reference Data）** | 枚举 / 代码表，值域有限且稳定 | 状态码 `MFGSTA_0`（1~5）、单位 `UOM`、币种 `CUR` | NL2SQL 过滤条件常用；`desc` 显式列出枚举值 |
| 4 | **指标数据（Metric Data）** | 派生计算或预聚合的数值，可直接查询 | `ontology_metric` 表（完工率 / 报废率 / 按时率） | 优先使用指标而非重算；formula 必填 |
| 5 | **关系数据（Relationship Data）** | 描述实体间多对多关联 | `MFGOPE.ROUALT_0 ↔ ROUOPE.ROUALT_0`（工序实例 ↔ 主数据）；BOM 多层展开 | JOIN 路径依赖；必填在 `BUSINESS_JOINS` |
| 6 | **日志 / 审计数据（Log / Audit Data）** | 系统自动记录，技术性、变更追踪 | `MFGLAB` 工序报工流水、`wms_inventory_log` 库存流水 | NL2SQL 通常不直接查；过滤时带时间窗；不进本体 |

### 1.2 分类判定流程

```
是否为系统自动产生 / 高频变更 / 无业务主键？
  → 是 → 日志 / 审计数据
  → 否 → 是否为预计算或公式？
    → 是 → 指标数据
    → 否 → 是否为枚举 / 代码表？
      → 是 → 参考数据
      → 否 → 是否描述实体间关系？
        → 是 → 关系数据
        → 否 → 是否长期稳定被引用？
          → 是 → 主数据
          → 否 → 业务单据
```

---

## 二、表定义规范（Table Definition Spec）

每张物理表接入前必须输出 **Table Spec Card**（一张卡），共 12 项必填。

| # | 字段 | 要求 | 示例（MFGHEAD） |
|---|---|---|---|
| 1 | **表物理名（source_table）** | 与 DB 一致；Oracle 全大写、PG/MySQL 小写 | `MFGHEAD` |
| 2 | **表中文名（class_alias）** | 业务名，2~6 字，无特殊字符 | `生产工单` |
| 3 | **数据分类** | 六类之一 | **业务单据** |
| 4 | **主键列** | 单字段或复合，`pk=True` 标注 | `MFGNUM_0`（单字段） |
| 5 | **数据语义角色** | 表头 / 表体 / 主数据 / 关系桥 / 汇总 | 表头 |
| 6 | **生命周期** | 长期稳定 / 短期流水 / 一次性事件 | 短期流水（创建到关闭即归档） |
| 7 | **典型行数** | 实际生产环境的预估 | 5000~10000 张 / 月 |
| 8 | **写入频率** | 高频（实时）/ 中频（日）/ 低频（月） | 中频（日 50~200 单） |
| 9 | **是否被下游表引用** | 是 / 否，列出关键引用方 | 被 MFGMAT、MFGOPE、MFGFOO 引用 |
| 10 | **关联业务事件** | 该表记录的业务事件 | 工单创建 / 下达 / 完工 / 关闭 |
| 11 | **业务单据号字段** | 单据号列名（如 `MFGNUM_0`） | `MFGNUM_0` |
| 12 | **核心本体描述** | 50~200 字，含业务场景与典型查询 | 「生产工单主表，记录工单号、产品、计划数量、计划开工 / 完工日期、实际数量、状态等。」 |

**Table Spec Card 输出位置**：`backend/scripts/diag_<table>_schema.py` 同时产出 schema + card，存到 `docs/data-dictionary/<source_table>.md`。

---

## 三、属性定义规范（Property Definition Spec）

每个字段接入本体时必须输出 **Property Spec Card**，共 11 项必填。

| # | 字段 | 要求 | 示例（MFGHEAD.MFGNUM_0） |
|---|---|---|---|
| 1 | **物理列名（alias）** | 与 DB 一致，**全大写**（Oracle） | `MFGNUM_0` |
| 2 | **中文名（name）** | 业务名 | `工单号` |
| 3 | **数据类型（type）** | 五选一：`STRING / INT / DECIMAL / DATETIME / BOOLEAN` | `STRING` |
| 4 | **业务分类** | 标识 / 维度 / 度量 / 时间 / 状态 / 备注 / 扩展 / 技术（见 §3.1） | **标识（PK）** |
| 5 | **是否主键（pk）** | True / False | True |
| 6 | **是否外键（fk）** | 目标类 source_table | 无 |
| 7 | **业务别名列表（aliases）** | 消歧缩写数组 | `["生产工单", "工单"]` |
| 8 | **数据质量约束** | 非空率 / 唯一性 / 精度 / 枚举值 | 非空率 100%；唯一 |
| 9 | **业务不变式** | 该字段参与的守恒 / 状态依赖 / 口径 | 工单号生成规则：`MFG-YYYY-####` |
| 10 | **典型查询模式** | 常见 WHERE / GROUP BY 用法 | `WHERE MFGNUM_0 = 'XXX'`、`GROUP BY MFGNUM_0` |
| 11 | **本体描述（desc）** | 含单位 / 枚举 / 业务场景 | 「生产工单唯一标识，格式 `MFG-YYYY-####`，全局唯一」 |

### 3.1 字段业务分类（8 类）

| 分类 | 含义 | 典型例子 |
|---|---|---|
| **标识（Identifier）** | PK / 业务唯一键 | `MFGNUM_0 / BPSNUM_0 / ITMREF_0` |
| **维度（Dimension）** | GROUP BY / 切片维度 | `MFGFCY_0 / BPSNUM_0 / ITMREF_0` |
| **度量（Measure）** | 可聚合数值 | `QTY_0 / LINAMT_0 / ACTTIM_0` |
| **时间（Time）** | 日期 / 时间戳 | `STDSTRDAT_0 / ACTENDDAT_0` |
| **状态（Status）** | 状态机字段 | `MFGSTA_0 / APPFLG_0 / YPTHFLG_0` |
| **备注（Remark）** | 自由文本 | `YNOTE_0 / TEX1_0` |
| **扩展（Extension）** | DIE / CCE / INVDTA 等扩展槽 | `DIE_0 / CCE_0` |
| **技术（Technical）** | 系统自动字段 | `EXPNUM_0 / AUUID_0 / CREDATTIM_0` |

---

## 四、引用关系规范（Reference Relationship Spec）

物理表之间的引用关系分为 4 种类型，每种在本体中的标注方式不同。

### 4.1 4 类引用对照

| # | 引用类型 | 定义 | 本体标注 | 示例 |
|---|---|---|---|---|
| 1 | **标识引用（Identifier Reference）** | 子表通过 PK 引用父表 | `BUSINESS_JOINS` + 子表 `fk` | `MFGMAT.MFGNUM_0 → MFGHEAD.MFGNUM_0` |
| 2 | **业务引用（Business Reference）** | 子表引用主数据（物料、供应商、地点） | `BUSINESS_JOINS` + 子表 `fk` | `MFGMAT.ITMREF_0 → ItemMaster.ITMREF_0` |
| 3 | **维度引用（Dimension Reference）** | 表引用枚举 / 参考数据 | `desc` 注明枚举来源 | `MFGHEAD.MFGSTA_0 ∈ MFGStatusEnum` |
| 4 | **聚合引用（Aggregation Reference）** | 指标 / 汇总表引用明细表 | `formula` 中显式声明 | `ProductionKPI.ACTQTY = SUM(MFGHEAD.ACTQTY_0)` |

### 4.2 生产域 4 类引用必填清单

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

---

## 五、业务单据规范（Transaction Document Spec）

业务单据（含生产域 5 类）必须满足 6 项硬规范。

| # | 规范 | 要求 |
|---|---|---|
| 1 | **表头 - 明细结构** | 表头与明细必须拆表（如 `MFGHEAD` + `MFGMAT`），不允许单表混合 |
| 2 | **单据号生成规则** | 单据号格式写进 `desc`，含前缀 + 年份 + 流水（如 `MFG-YYYY-####`） |
| 3 | **状态机完整性** | 状态字段必进本体；枚举值 100% 写进 `desc`；合法转移路径写进 `desc` |
| 4 | **表头 - 明细守恒** | 表头合计字段 = SUM(明细行)；守恒校验脚本必过 |
| 5 | **下游单据关联** | 列出该单据可能触发的下游单据；关联字段写进本体 |
| 6 | **时间维度齐全** | 至少有：创建日期、业务日期、计划日期、实际日期；写进 `desc` |

### 5.1 生产域业务单据清单

| 单据 | 表头 | 明细 | 状态字段 | 关键守恒 |
|---|---|---|---|---|
| **生产工单** | `MFGHEAD` | `MFGMAT` + `MFGOPE` | `MFGSTA_0` (1~5) | TOTLINQTY = SUM(MFGMAT.QTY_0) |
| **完工入库** | `MFGFOO` | `MFGFOOD`（若有） | `FOOSTA_0` (1~4) | TOTQTY = SUM(明细.QTY_0) |
| **工序报工** | — | `MFGLAB` | `LABSTA_0` | SUM(MFGLAB.ACTQTY_0) ≤ MFGHEAD.ACTQTY_0 |
| **工序转移** | — | `MFGTIM` | `TIMSTA_0` | ACTEND ≥ ACTSTR（同工序） |

---

## 六、主数据规范（Master Data Spec）

主数据接入本体时必须满足 5 项硬规范。

| # | 规范 | 要求 |
|---|---|---|
| 1 | **唯一标识** | PK 必填且全局唯一；自然键（如 `BPSNUM_0`）进本体 |
| 2 | **长期稳定** | 主体属性变更率低（年度 < 10%）；删除应走软删除（`DELFLG_0`）而非物理删除 |
| 3 | **完整属性集** | 至少有：编码、名称、状态、创建日期、修改日期 |
| 4 | **跨域引用清晰** | 在 `desc` 注明该主数据被哪些业务单据引用 |
| 5 | **本体建模标识** | 在 `seed_ontology.PROPERTIES` 中标记 `pk=True`，并加 `master_data=True` 业务标签 |

### 6.1 生产域主数据新增建议

| 主数据 | 物理表 | 必填字段 |
|---|---|---|
| 工作中心 | `WORKCENT` | `WCR_0` PK、`DES_0` 名称、`FCY_0` 工厂 |
| 班次 | `SHIFTS` | `SHIFTID_0` PK、`STR_0` 开始时间、`END_0` 结束时间 |
| 操作工 | `USER` | `USR_0` PK、`NAM_0` 姓名、`ROL_0` 角色 |
| 班组 | `TEAM` | `TEAMID_0` PK、`DES_0` |

---

## 七、参考数据规范（Reference Data Spec）

参考数据（枚举 / 代码表）必须满足 4 项硬规范。

| # | 规范 | 要求 |
|---|---|---|
| 1 | **物理存储** | 优先独立代码表（`MFGStatusEnum`），其次字典项（`TSICOD_0`），最次用字段常量（如 `MFGSTA_0=1`） |
| 2 | **枚举值 100% 列示** | 所有取值必须写进字段的 `desc`，格式：`1=草稿 2=审核 3=下达 4=完工 5=关闭` |
| 3 | **新增值守约** | 新增枚举值必须同步更新 `desc` + 验证脚本（否则 `verify_enums.py` 报错） |
| 4 | **本体建模标记** | 在 `seed_ontology` 中给枚举字段加 `is_reference=True` 业务标签 |

### 7.1 生产域参考数据清单

| 枚举 | 取值 | 物理位置 |
|---|---|---|
| `MFGSTA_0` 工单状态 | 1=计划 2=下达 3=在制 4=完工 5=关闭 | `MFGHEAD.MFGSTA_0` |
| `MFGTYP_0` 工单类型 | 1=普通 2=返工 3=委外 4=试制 | `MFGHEAD.MFGTYP_0` |
| `OPESTA_0` 工序状态 | 1=未开始 2=在制 3=完成 4=跳过 | `MFGOPE.OPESTA_0` |
| `FOOSTA_0` 入库状态 | 1=草稿 2=已入库 3=已过账 4=关闭 | `MFGFOO.FOOSTA_0` |
| `SCRREA_0` 报废原因 | 1=质量 2=设备 3=工艺 4=物料 9=其他 | `MFGOPE.SCRREA_0` |

---

## 八、指标数据规范（Metric Data Spec）

指标数据接入 `ontology_metric` 表时必须满足 7 项硬规范。

| # | 规范 | 要求 |
|---|---|---|
| 1 | **命名规范** | 指标名必须是中文业务名；含「率 / 占比 / 比例 / 百分比 / ratio / percent / share / pct」→ `formula` 必填（`validatePlan` 拦截） |
| 2 | **公式明确** | `formula` 必须是合法 SQL 表达式（`SUM / AVG / COUNT / 窗口函数` 等）；不能写自然语言 |
| 3 | **分母零保护** | 含除法的 formula 必须用 `NULLIF(分母, 0)` 或 `CASE WHEN ... = 0 THEN 0` |
| 4 | **属性引用真实** | `formula` 引用的属性必须在 `selectedProperties` 中；否则 `validatePlan` 报错 |
| 5 | **维度声明** | 指标必须声明可用的 GROUP BY 维度（如「工单完工率可按工厂 / 产品 / 班组分组」） |
| 6 | **业务口径文档化** | 在 `Harness/wiki/business-domain.md` 列出每个指标的口径与边界（如「完工率含报废 / 不含返工」） |
| 7 | **本体建模标记** | 在 `seed_ontology.METRICS` 中定义（与 CLASSES / PROPERTIES 同级） |

### 8.1 生产域核心指标预置（10 个）

| 指标名 | formula | 维度 | 业务口径 |
|---|---|---|---|
| `工单完工率` | `SUM(ACTQTY_0) / NULLIF(SUM(STDQTY_0), 0)` | 工厂 / 产品 / 月份 | 含合格 + 报废；不含已关闭 |
| `工单准时率` | `SUM(CASE WHEN ACTENDDAT_0 <= STDENDDAT_0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*)` | 工厂 / 产品 | 实际完工 ≤ 计划完工 |
| `工序按时率` | `SUM(CASE WHEN ACTEND <= STDEND THEN 1 ELSE 0 END) * 1.0 / COUNT(*)` | 工作中心 / 工序 | 同上 |
| `报废率` | `SUM(SCRQTY_0) / NULLIF(SUM(ACTQTY_0) + SUM(SCRQTY_0), 0)` | 工厂 / 产品 / 工序 | 报废 / (合格 + 报废) |
| `返工率` | `SUM(CASE WHEN MFGTYP_0=2 THEN 1 ELSE 0 END) * 1.0 / COUNT(*)` | 工厂 / 产品 | 返工工单 / 总工单 |
| `工序产能利用率` | `SUM(ACTTIM_0) / NULLIF(SUM(STDTIM_0 * ACTQTY_0 / STDQTY_0), 0)` | 工作中心 / 工序 | 实际工时 / 标称工时 |
| `平均工序工时` | `AVG(ACTTIM_0)` | 工作中心 / 工序 | — |
| `工单平均周期` | `AVG(ACTENDDAT_0 - ACTSTRDAT_0)` | 工厂 / 产品 | 单位：天 |
| `物料齐套率` | `SUM(实际齐套物料数) * 1.0 / SUM(应齐套物料数)` | 工单 / 产品 | 实际领料 / 计划领料 |
| `设备故障停机率` | `SUM(DOWNTIM_0) / NULLIF(SUM(DOWNTIM_0 + RUNTIME_0), 0)` | 工作中心 | — |

---

## 九、AI 学习路径设计（AI Onboarding）

让 LLM / AI 应用系统性地**学习这套规范**，分三阶段。

### 9.1 阶段 1：被动摄取（System Prompt 注入）

在 NL2SQL System Prompt 中固定注入以下内容（写入 `seed_business_rules.py`）：

```
【数据分类体系】本系统数据分 6 类：主数据 / 业务单据 / 参考数据 / 指标数据 / 关系数据 / 日志数据。
每类字段的 SQL 行为预期不同：主数据作为 JOIN 锚点、业务单据作为 GROUP BY 聚合源、参考数据用于 WHERE 过滤、指标数据直接使用。

【字段业务分类】每字段分 8 类：标识 / 维度 / 度量 / 时间 / 状态 / 备注 / 扩展 / 技术。

【引用关系 4 类】标识引用 / 业务引用 / 维度引用 / 聚合引用。

【业务单据规范】表头与明细拆表；单据号格式 MFG-YYYY-####；状态机字段必填；表头 - 明细守恒。

【主数据规范】PK 全局唯一；长期稳定；删除走软删除。

【参考数据规范】枚举值必须穷举写进 desc；新增值必须更新本体。

【指标数据规范】含「率 / 占比 / 比例 / 百分比」关键词的 formula 必填；分母零保护；属性引用真实。
```

### 9.2 阶段 2：主动推理（Few-shot 示例）

在 Prompt 中加入 3~5 条典型查询的 Few-shot，每条标注使用了哪类数据 / 哪类引用：

```
问：「3 月份各工厂的工单完工率？」
答：SELECT FCY, SUM(ACTQTY) / SUM(STDQTY) AS 完工率
    FROM ProductionOrder WHERE ACTENDDAT BETWEEN ... GROUP BY FCY
    [数据分类：业务单据] [引用：聚合引用] [指标：完工率]
```

### 9.3 阶段 3：自我纠错（验证脚本）

| 验证脚本 | 校验内容 |
|---|---|
| `verify_enums.py` | 枚举值 100% 与 `desc` 一致 |
| `verify_invariants.py` | 守恒 / 状态依赖 / FK 有效性 |
| `verify_metric_formula.py` | 指标公式属性引用真实 + 分母零保护 |
| `verify_reference_integrity.py` | 4 类引用全部声明 |

### 9.4 AI 能力评估指标（5 项）

| 能力 | 评估方式 | 合格线 |
|---|---|---|
| 选表准确率 | 50 个真实问法 → 期望本体类 | ≥ 90% |
| 选列准确率 | 同上 → 期望本体属性 | ≥ 85% |
| 派生指标理解 | 50 个占比类问法 → formula 必填 | ≥ 95% |
| 状态机理解 | 状态过滤类问法 → 正确枚举值 | ≥ 95% |
| 跨域推理 | 跨域问法 → 正确 JOIN 路径 | ≥ 85% |

---

## 十、数据契约总览表（生产域示例）

下表是生产域 **Table Spec Card + Property Spec Card + Reference + Metric** 的汇总视图，作为 LLM 选表的「鸟瞰图」。

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
| `指标库` | (ontology_metric) | 指标数据 | — | — | — | 完工率 / 准时率 / 按时率 / 报废率等 10 个 |

---

## 附录 A：上线前 10 项验收清单

| # | 验收项 | 责任方 |
|---|---|---|
| 1 | Table Spec Card 12 项 100% 填写 | 数据建模 |
| 2 | Property Spec Card 11 项 100% 填写 | 数据建模 |
| 3 | 4 类引用全部声明（标识 / 业务 / 维度 / 聚合） | 数据建模 |
| 4 | `seed_ontology.PROPERTIES` 八字段必填 | 后端开发 |
| 5 | `seed_ontology.METRICS` 7 项规范守约 | 后端开发 |
| 6 | PG / Neo4j / Milvus 三处一致 | 运维 |
| 7 | `verify_enums.py` 通过 | 后端测试 |
| 8 | `verify_invariants.py` 通过 | 后端测试 |
| 9 | `verify_metric_formula.py` 通过 | 后端测试 |
| 10 | AI 能力评估 ≥ 合格线（选表 ≥ 90% 等） | 产品 / QA |

---

## 附录 B：反面案例（不要这样做）

| 反例 | 后果 | 正例 |
|---|---|---|
| 字段业务分类缺失 | LLM 选错语义角色 | 强制 8 分类之一 |
| 枚举值仅写「状态」无枚举 | LLM 无法 WHERE 过滤 | `desc` 列 1=草稿 2=审核... |
| 主数据未软删除 | 历史数据丢失，跨域引用断链 | 加 `DELFLG_0` |
| 指标公式未做分母零保护 | 偶尔除零异常 | `NULLIF(SUM(x), 0)` |
| 4 类引用只声明 1 类 | JOIN 路径遗漏 | 4 类全声明 |
| `formula` 含未在 selectedProperties 的属性 | `validatePlan` 拦截 | 引用真实属性 |
| 状态机转移路径缺失 | LLM 把已关闭单据算进在制 | 写进 `desc` |
| 表头与明细混在同一张表 | 守恒校验脚本无法写 | 拆 `MFGHEAD + MFGMAT` |

---

## 附录 C：参考文档

- 标准依据：`docs/20260828-new-module-data-standard.md`
- 既有模式参考：`docs/20260814-PPRICCONF-ontology.md`（新增类完整范例）
- 既有变更参考：
  - `Harness/changes/feat-excel-not-needed-properties` — Excel 字段裁剪
  - `Harness/changes/fix-nl2sql-derived-metric-formula-required` — 派生指标必填
  - `Harness/changes/fix-supplier-price-dimensions` — 同名异义消歧
  - `Harness/changes/fix-semantic-class-retrieval` — 向量语义召回
- 数据模型：`Harness/wiki/data-model.md`
- 业务流程：`Harness/wiki/business-domain.md`
- NL2SQL 引擎：`Harness/wiki/nl2sql-engine.md`
- 生产域接入提案：`Harness/changes/feat-production-domain-onboarding/proposal.md`

---

*本文档由 qa-system Harness 治理，变更需走 `Harness/changes/feat-<name>/summary.md` 9 段模板流程。*

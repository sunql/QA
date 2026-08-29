# 新模块数据准入标准

> 基于 qa-system 现有 Phase 1~5 实现抽取的「新数据/新模块」准入建议。
> 目的：新模块接入后 NL2SQL 准确率高、出图正确、扩展不踩既有坑。

## 适用范围

- 接入新的物理表 / 业务域（如供应商域外、质量域、设备域）
- 已有物理表新增字段或新增语义
- 接入新数据源（`POST /api/v1/datasource`）

## 三层准入

```
本体建模层（语义检索 + 选表）
      ↓
SQL 生成层（NL2SQL 产出可执行 SQL）
      ↓
运行时层（只读安全 + 多方言 + 多数据源）
```

> **准入前两道门槛（数据质量 + 业务规则）必须先过**，否则即使本体写得再漂亮，LLM 也会基于脏数据生成错误 SQL / 出错图表。

## 〇、数据质量要求（准入第一道门槛）

数据质量关注的是**单条数据的「完整性、唯一性、准确性、一致性、时效性、有效性」**。下面逐项给出在新模块接入时必须验证的硬指标与责任方。

### 0.1 完整性（Completeness）

| 维度 | 准入要求 | 验证方式 | 不达标处置 |
|---|---|---|---|
| **必填字段非空率** | 主键、外键、状态字段、日期字段的非空率 ≥ 99.5% | `SELECT COUNT(*) WHERE col IS NULL` × 表行数 | 阻断接入，要求源系统补数；或在本体内标记为「可能为 NULL」并在 `desc` 注明 |
| **关键业务字段** | 数量、金额、日期字段非空率 ≥ 95% | 同上 | 阻断；缺失行无法支撑 NL2SQL 聚合 |
| **本体声明的字段** | 与 `seed_ontology.PROPERTIES` 一一对应，列存在性 100% | `verify_ontology_consistency.py` 的 `property_missing` | 阻断 |
| **派生指标依赖链** | `formula` 引用的列必须 100% 存在 | `validatePlan` 在聚合循环内校验 `property.name` 是否在 selectedProperties | 阻断并重试 |
| **本体语义完整性** | 每个类必须至少有 1 个 STRING 类型字段（用于多步上下文 ENTITY_LIST 判定） | `seed_ontology` 入口校验 | 阻断 |

### 0.2 唯一性（Uniqueness）

| 维度 | 准入要求 | 验证方式 | 不达标处置 |
|---|---|---|---|
| **主键唯一性** | PK 列无重复行 | `SELECT col, COUNT(*) FROM t GROUP BY col HAVING COUNT(*)>1` | 阻断接入；PK 重复会让 NL2SQL JOIN 产生笛卡尔积爆炸 |
| **复合 PK 唯一性** | `(pk1, pk2)` 联合无重复 | 同上分组 | 阻断 |
| **业务唯一键** | 自然业务键（如「供应商编码 + 物料编码 + 生效日期」）无重叠 | 同上 | 阻断或纳入本体描述（如「价格唯一按供应商+物料+生效起期」） |
| **本体类名唯一** | `class_name` 全局唯一（PG `ontology_class` 唯一约束） | seed 抛 IntegrityError | 阻断；冲突会让 Milvus id 碰撞互删 |
| **本体属性名唯一** | `(class_id, property_name)` 唯一 | seed 幂等跳过已存在 | 阻断（同名覆盖 = 误删既有属性） |

### 0.3 准确性（Accuracy）

| 维度 | 准入要求 | 验证方式 | 不达标处置 |
|---|---|---|---|
| **数值字段精度** | 数量 / 金额字段精度与 ERP 一致（通常 DECIMAL(18,4) 或 DECIMAL(20,6)） | 抽 5~10 条比对源系统 | 不阻断但 `desc` 注明精度与四舍五入规则 |
| **枚举值合法性** | 状态字段 / 类型字段的所有取值必须在 `desc` 列出的枚举范围内 | `SELECT DISTINCT col FROM t` 比对 `desc` 枚举 | 阻断；新增枚举值需更新本体后再接入 |
| **外键有效性** | 外键列的取值 100% 存在于目标主键表（**虽然库无 FK 约束，业务上必须守约**） | `SELECT col FROM t WHERE col NOT IN (SELECT pk FROM ref)` | 阻断；脏 FK 会让 NL2SQL JOIN 丢数据 |
| **日期字段合法性** | `DATETIME` 字段无 `1900-01-01 / 9999-12-31` 等哨兵值 | `SELECT WHERE col IN ('1900-01-01', '9999-12-31')` | 阻断；哨兵值会让 `BETWEEN` 范围查询失真 |
| **金额单位一致** | 同名字段在不同表语义单位一致（如 `QTY_0` 在 PORDERQ 与 MFGHEAD 都是「件」而非「kg」） | 抽 5~10 条对比 + 业务确认 | 阻断；单位混算会让 SUM 偏差数倍 |

### 0.4 一致性（Consistency）

| 维度 | 准入要求 | 验证方式 | 不达标处置 |
|---|---|---|---|
| **跨表同名同义** | `POHNUM_0` 在 PORDER / PORDERQ / PRECEIPT / YPRECEIPTD 必须同义 | 抽 5~10 行 JOIN 验证 | 阻断；同名异义会破坏 NL2SQL 跨表 JOIN |
| **状态字段联动** | 父表状态 ↔ 子表状态联动合法（如 PORDER.APPFLG_0=1（已签字）时子行必须存在） | 业务规则验证脚本 | 阻断；状态不一致会让 NL2SQL 的 WHERE 过滤漏数据 |
| **数量守恒** | 父表合计 = 子表合计（如 PORDER.TOTLINQTY_0 = SUM(PORDERQ.QTYUOM_0)） | `SELECT ABS(parent - SUM(detail)) / parent > 0.001` | 阻断；不守恒会让聚合查询随机偏差 |
| **时序一致性** | 创建日期 ≤ 修改日期 ≤ 当前日期；开工 ≤ 完工 | `SELECT WHERE CRE > UPD OR UPD > NOW()` | 阻断 |
| **本体三处一致** | PG / Neo4j / Milvus 三处 class 数与 property 数完全一致 | `verify_ontology_consistency.py` | 阻断 |

### 0.5 时效性（Timeliness）

| 维度 | 准入要求 | 验证方式 | 不达标处置 |
|---|---|---|---|
| **数据新鲜度** | 数据延迟 ≤ 业务允许的同步窗口（T+1 / 实时） | 与源系统比对 `MAX(CREDAT_0)` | 阻断或加 SLA 声明 |
| **历史归档完整** | 历史数据完整保留（无物理删除），软删除 / 关闭状态字段必须保留 | 抽查历史年度数据是否可查 | 阻断 |
| **时区字段明确** | `DATETIME` 字段的时区在 `desc` 注明（UTC / 本地 / ERP 服务器时区） | 抽 5~10 条与源系统比对 | 阻断；时区错位会让「最近 30 天」查询漏数据 |
| **状态字段不滞后** | 状态字段（如 `MFGSTA_0`）必须在事件发生后 ≤ N 分钟更新 | 源系统 SLA | 阻断或加 `desc` 注明「可能滞后 N 小时」 |

### 0.6 有效性（Validity）

| 维度 | 准入要求 | 验证方式 | 不达标处置 |
|---|---|---|---|
| **类型合法** | `INT` 字段无小数 / 非数字；`DECIMAL` 无非数字字符 | `SELECT WHERE col != CAST(col AS INT)` | 阻断 |
| **长度合法** | `STRING` 字段无超长截断 | `SELECT WHERE LENGTH(col) > max_len` | 阻断 |
| **格式合法** | 编码类字段（如 `BPSNUM_0` 供应商编码）符合源系统规则 | 正则校验 | 阻断 |
| **正负号合法** | 数量 / 金额字段符号业务合法（如退货数量为负是合法的，但 PORDERQ 的 `QTYUOM_0` 必须 ≥ 0） | 业务确认 + `desc` 注明 | 阻断 |
| **行数预估** | 表行数在 LLM 单次查询成本内（≤ 1000 万为佳，超出需在 `desc` 注明「归档表」） | `SELECT COUNT(*)` | 不阻断但 `desc` 注明 |

## 〇·五、业务规则要求（准入第二道门槛）

业务规则关注的是**多条数据之间的「不变式、状态机、维度口径、跨域联动」**。必须显式编码到本体描述 / Prompt / 派生指标 / 注释中，LLM 才能正确处理。

### 0.5.1 业务不变式（Business Invariants）

**定义**：任何时候都成立的跨字段 / 跨表 / 跨行约束。

| 不变式 | 示例 | 编码方式 |
|---|---|---|
| **总量等于分量之和** | `PORDER.TOTLINQTY_0 = SUM(PORDERQ.QTYUOM_0)` | 在 `PORDER.description` 与 `PORDERQ.description` 双向注明 |
| **外键存在性** | `PORDERQ.BPSNUM_0 ∈ BPSUPPLIER.BPSNUM_0` | 已在 0.3 外键有效性中校验；不达标阻断 |
| **状态依赖** | `PORDER.APPFLG_0 = 1` ⇒ `PORDERQ.LINAPPFLG_0 = 1`（签字后所有行必须签字） | 在 `PORDER.description` 注明状态联动规则 |
| **金额守恒** | `LINAMT_0 = QTYUOM_0 * NETPRI_0`（允许尾差） | 在 `PORDERQ.description` 注明「LINAMT 由 QTY × NETPRI 算出，可能有尾差」 |
| **唯一业务键** | `(BPSNUM_0, ITMREF_0, STRDAT_0)` 是供应商价格清单唯一键 | 在 `PPRICLIST.description` 注明 |

### 0.5.2 状态机规则（State Machine）

**定义**：单据 / 实体的状态字段取值及合法转移路径。

**编码模板**（必须写入本体 `desc`）：

```
状态字段名 Xxx_0：
- 1=草稿（可修改，可删除）
- 2=已审核（不可修改，可下游引用）
- 3=已下达（已触发下游单据，不可撤销）
- 4=已完成（数量已锁定，可关闭）
- 5=已关闭（只读，不可再引用）
合法转移路径：1 → 2 → 3 → 4 → 5；任何状态可跳到 5（强制关闭）
```

**强制校验清单**：

| 维度 | 要求 |
|---|---|
| 状态字段必须进本体 | 否则 LLM 无法按状态过滤（如「未收货的采购订单」） |
| 状态枚举值 100% 穷举 | `SELECT DISTINCT Xxx_0` 与 `desc` 枚举必须一致；新增枚举值必须更新本体 |
| 合法转移路径必须写进 `desc` | 让 LLM 知道哪些状态组合合法（如「已关闭（5）不可再生成收货单」） |
| 维度字段联动 | 例：`YPTHFLGM_0=1` 时才能用 YPRECEIPT（到货单）；`=2` 时跳过 YPRECEIPT 直接走 PRECEIPT |

**反面案例**（参考 `Harness/changes/fix-supplier-price-dimensions`）：

- 状态字段仅写「状态」无枚举 → LLM 无法生成 `WHERE APPFLG_0 = 1` 类查询
- 维度字段无联动说明 → LLM 会同时查 YPRECEIPT 与 PRECEIPT，导致零库存供应商的「到货完成率」虚增
- 合法转移路径缺失 → LLM 可能把「已关闭」单据也算进「在制」聚合

### 0.5.3 维度口径规则（Dimension Disambiguation）

**定义**：同一指标在不同维度下有不同语义，必须显式声明。

**典型场景**（参见 `Harness/wiki/business-domain.md`）：

| 场景 | 维度字段 | 口径规则 |
|---|---|---|
| **订单完成率** | `BPSUPPLIER.YPTHFLGM_0` | `1`（非零库存供应商）与 `2`（零库存供应商）必须**分组**统计，混算会同时虚增/虚减两侧 |
| **供应商价格** | `PPRICLIST` 三类价格 | `PLI_0` 不同取值对应不同取价优先级（T10/T11/T20/T21/T30/T31）→ 必须按 PLI_0 分组 |
| **物料分类** | `TSICOD_0/1/2/3` | 四个分类码含义不同（统计分类 vs 车型编码 vs 部门分类），不可混用 |
| **时间口径** | `CREDAT_0 / ORDDAT_0 / RCPDAT_0` | 创建日期 vs 订单日期 vs 收货日期 → 「3 月订单」必须用 `ORDDAT_0` 而非 `CREDAT_0` |
| **数量单位** | `QTYPUU_0 / QTYSTU_0 / QTYUOM_0` | 采购单位 vs 库存单位 vs 订单单位 → SUM 必须先 `UOMPUUCOE_0` 换算 |

**编码要求**：

- 维度字段必须进本体
- 在本体 `description` 中显式说明「按 X 分组」的口径
- 复杂口径写进 `Harness/wiki/business-domain.md` 业务流程段落
- LLM Prompt 注入（见 0.5.5）

### 0.5.4 派生指标语义规则（Derived Metric Semantics）

**定义**：由基础列计算得出的指标，必须显式定义公式与维度。

**准入清单**：

| 维度 | 要求 |
|---|---|
| **关键词必填** | 命名含「占比 / 比率 / 比例 / 百分比 / ratio / percent / share / pct」→ `formula` 必填（`validatePlan` 拦截） |
| **分母分子明确** | `公式中显式写出 SUM(...) / SUM(...)` 而非 `AVG(...)`（AVG 算占比会失真） |
| **分母零保护** | `formula` 中包含 `NULLIF(SUM(...), 0)` 或 `CASE WHEN ... = 0 THEN 0 ELSE ... END` |
| **窗口函数适用** | 「占比」类必须用 `SUM(x)/SUM(SUM(x)) OVER()`（计算各行占整体的比例） |
| **属性引用真实** | `formula` 引用的列必须在 `selectedProperties` 中（`validatePlan` 校验） |
| **业务语义文档化** | 在 `Harness/wiki/business-domain.md` 列出每个指标的口径与边界（如「完工率含报废 / 不含返工」） |

**反面案例**：

- `alias="TOP10占比"` 但 `formula` 空 → `validatePlan` 拦截（见 `fix-nl2sql-derived-metric-formula-required`）
- `formula="AVG(QTY_0)"` 算「占比」→ 语义错误，AVG 对占比无意义
- `formula="SUM(QTY_0)/SUM(STD_0)"` 分母可能为 0 → 除零异常
- 跨表比率未显式写 JOIN 路径 → `validatePlan` 解析失败

### 0.5.5 Prompt 注入规则（Prompt Injection）

**定义**：业务规则不能只写在本体 `desc` 里，必须同步注入到 NL2SQL System Prompt。

**三类必注入**：

| 类型 | 注入位置 | 示例 |
|---|---|---|
| **维度口径** | `_buildPlanSystemPrompt` 的「业务规则」段 | 「订单完成率必须按 `BPSUPPLIER.YPTHFLGM_0` 的 `1/2` 分组统计」 |
| **状态机** | `_buildPlanSystemPrompt` 的「状态字段」段 | 「`PORDER.APPFLG_0 = 1` 已是已签字状态，行级 `LINAPPFLG_0` 必须为 1」 |
| **派生指标关键词** | `_buildPlanSystemPrompt` 的「派生指标」段 | 「涉及占比/比率/百分比时 formula 必填」 |

**注入模板**（写入 `seed_business_aliases.py` 或新增 `seed_business_rules.py`）：

```python
BUSINESS_RULES = [
    {
        "trigger_class": "Supplier",
        "rule": "订单完成率必须按 YPTHFLGM_0 的 1/2 分组统计，混算会虚增/虚减",
        "prompt_inject": "【业务规则】涉及订单完成率/收货完成率/到货及时率时，"
                         "必须显式按 BPSUPPLIER.YPTHFLGM_0 IN ('1','2') 分组；"
                         "YPTHFLGM_0=2 的供应商跳过 YPRECEIPT，直接走 PRECEIPT。",
    },
    {
        "trigger_class": "PurchaseOrder",
        "rule": "PORDER 与 PORDERQ 状态必须联动",
        "prompt_inject": "【业务规则】PORDER.APPFLG_0=1 时，"
                         "PORDERQ.LINAPPFLG_0 必须全为 1；"
                         "查询「未签字订单」时两个 flag 都加 WHERE =0。",
    },
]
```

### 0.5.6 跨域联动规则（Cross-Domain Rules）

**定义**：跨业务域（采购 ↔ 生产 ↔ 销售 ↔ 财务）的联动规则。

| 联动 | 规则 |
|---|---|
| **采购 → 库存** | 收货单 `PRECEIPT` 数量 → `wms_inventory.quantity` 增量（不在本体直接建模，但 LLM 推断会用到） |
| **采购 → 财务** | `LINAMT_0`（不含税） + `AMTTAXLIN1_0`（税额） = `LINATIAMT_0`（含税） |
| **生产 → 库存** | 完工入库 `MFGFOO` 数量 → `wms_inventory.quantity` 增量（产成品） |
| **生产 → 销售** | 工单 `MFGHEAD.ACTQTY_0` 通常对应销售订单的「已发货」 |
| **工艺 → 工单** | `MFGOPE` 的 `OPENUM_0` 必须 ∈ `ROUOPE` 主数据中定义的工序集 |

**编码要求**：

- 跨域联动在 `Harness/wiki/business-domain.md` 的「跨域流程」段集中说明
- 涉及跨域查询时，Prompt 注入对应规则
- 复杂跨域走多步流水线（已在 `nl2sql-engine.md` 多步上下文段实现）

### 0.5.7 业务规则验证清单（上线前必过）

| # | 验证项 | 工具 |
|---|---|---|
| 1 | 不变式校验脚本（守恒 / 外键 / 状态依赖） | `scripts/verify_invariants.py` |
| 2 | 状态枚举穷举（`SELECT DISTINCT` 比对 `desc` 枚举） | `scripts/verify_enums.py` |
| 3 | 派生指标 formula 守约 | `test_formula_with_valid_properties_passes` |
| 4 | 维度口径 5~10 个真实问法覆盖 | 手工验证集（见提案 §6） |
| 5 | 跨域联动问法覆盖 | 手工验证集 |
| 6 | Prompt 注入规则文本与本体 `desc` 一致 | 单元测试 |
| 7 | 业务规则文档更新到 `Harness/wiki/business-domain.md` | 手工 review |

---

## 一、本体建模层标准（最关键）

### 1.1 类（Class）准入

| 字段 | 要求 | 反例 |
|---|---|---|
| `class_name` | 英文 PascalCase，**唯一**，与物理表 1:1 | `Porder`（小写）、与已有类重名 |
| `class_alias` | 中文业务名，2~6 字，无特殊字符 | `采购订单(PO)`（含括号） |
| `source_table` | 与数据源实际表名精确一致（Oracle 全大写、PG/MySQL 小写） | 手工大小写不一致 |
| `description` | 1~2 句说明业务用途 + 典型场景 + 关键业务名词 | 「订单表」 |
| PK 标注 | 库无声明 PK 时**手工标 `pk=True`**，至少 1 个 | 全部 false |

### 1.2 属性（Property）准入（每条必填）

| 字段        | 要求                                                | 缺失后果                  |
| --------- | ------------------------------------------------- | --------------------- |
| `name`    | 中文业务名                                             | LLM 找不到字段             |
| `alias`   | **物理列名**，与 DB 列精确一致                               | SQL 拼接失败              |
| `type`    | 五选一：`STRING / INT / DECIMAL / DATETIME / BOOLEAN` | 校验拦截                  |
| `pk`      | 主键标 True                                          | JOIN 推断缺失             |
| `fk`      | 外键目标类的 `source_table`                             | `BUSINESS_JOINS` 无法生成 |
| `col`     | 默认 = `alias`；函数列填表达式                              | SELECT 报错             |
| `aliases` | **业务别名列表**（消歧缩写，如 `["报价", "供应商报价"]`）              | 同名异义无法消解              |
| `desc`    | 含单位 / 枚举值 / 业务场景                                  | 向量召回率低 + LLM 选错列      |

### 1.3 必须排除的字段（默认不进入本体）

- **技术系统字段**：`EXPNUM_0 / AUUID_0 / CREDATTIM_0 / UPDDATTIM_0` 等
- **扩展槽位**：`DIE_0 / CCE_0 / INVDTA* / DISCRG* / CLCAMT* / DCGVAL_*`

通过 Excel `不需要=1` 列标记删除（见 `Harness/changes/feat-excel-not-needed-properties`）。

### 1.4 业务关联（JOIN）准入

`BUSINESS_JOINS` 必须显式声明所有外键 JOIN，按四元组 `(from_class, from_col, to_class, to_col)`：

- 主键 ↔ 外键成对声明
- 跨表头的特殊 JOIN（如到货单 → 收货单）需独立声明
- `validatePlan` 连通性校验依赖此声明，缺一条则多表查询失败

### 1.5 Milvus 向量语义

- **类文本**：类名 + 别名 + 描述，合计 50~200 字
- **属性文本**：中文名 + 业务别名 + 说明，合计 30~150 字
- **避免纯技术命名**（如 `字段AUTO_001`）——向量检索靠语义相似度
- 维度变更（当前 `dim=1024`）后**必须**重 `--cleanup` 重建
- 类/属性 id 必须分别独立序列，避免 Milvus 互删（见 `docs/20260814-PPRICCONF-ontology.md` §5）

## 二、SQL 生成层标准

### 2.1 数据源注册

通过 `POST /api/v1/datasource` 注册：

| 字段 | 要求 |
|---|---|
| `type` | `mysql` / `postgresql` / `oracle` 之一（**决定方言 prompt**） |
| `connection_url` | 含主机/端口/库名；**禁止 `file://`**（SQL Guard 黑名单） |
| `is_read_only` | **必须 True** |
| `encrypted_password` | Fernet 加密，密钥由 `ENCRYPTION_KEY` 管理 |
| `username` | Oracle 模式下即 schema owner（不再硬编码 `ZJTH.`） |

### 2.2 方言与 SQL 可执行性

| 方言 | 行数限制 | 列名引用 | Schema 前缀 |
|---|---|---|---|
| **Oracle** | `FETCH FIRST N ROWS ONLY` | 列加双引号保留大小写 | `username.Table` |
| **MySQL/PG** | `LIMIT N` | 列小写可不加引号 | 无 |

表/列名变更后**必须同步 `seed_ontology.py`**，否则 LLM 生成 SQL 引用旧名会失败。

### 2.3 派生指标 `formula` 必填

涉及以下关键词的指标命名：`占比 / 比率 / 比例 / 百分比 / ratio / percent / share / pct`

- `formula` 字段**必填**（否则 `validatePlan` 拦截）
- 命名建议：`TOTAL_QTY / COUNT_NUM` 用于基础聚合，`占比_X` 用于派生
- 详见 `Harness/changes/fix-nl2sql-derived-metric-formula-required`

### 2.4 关键业务字段必须显式标注

参考既有经验：

- `BPSUPPLIER.YPTHFLGM_0`（收货模式 `1=正常 / 2=零库存`）决定整条供应链指标口径 → 本体描述必须写明
- `PPRICCONF.CRINBR_0` 条件维度字段按"条件序号→简称/表/字段/描述"分组列示
- 状态字段枚举值必须写进 `desc`（如 `YPTHFLG_0`：草稿/到货/质检完毕/收货 4 态）

## 三、运行时与安全标准

### 3.1 性能与体量

- 单表行数超 1000 万行 → 描述里注明「历史归档表」，引导 LLM 走汇总
- 派生指标 / 时间序列查询走**范围感知行数限制**（`NL2SQL_NO_SCOPE_ROW_LIMIT=100`）
- 单次查询最大 `QUERY_ROW_LIMIT=5000`（SQL Guard 兜底）

### 3.2 时间字段规范

- 业务时间字段必须显式 `DATETIME` 类型
- `CREDAT_0 / UPDDAT_0` **不可作为业务时间字段**

### 3.3 多步上下文传递

新增类作为「主键列表」（如 Top N 物料）输出时：

- 渲染策略由 `_detect_step_data_shape` 自动判定：≤50 行 + 至少 1 字符串列 → `ENTITY_LIST` 标签 + `WHERE IN` 强指令
- 主键列必须**至少有一个 STRING 类型**，否则被识别为聚合值丢上下文
- 显式多步问题不得判为 REFINE/FOLLOW_UP（参见 `nl2sql-engine.md` 多步上下文强注入）

### 3.4 幂等与回滚

- `seed_ontology.py` 必须**幂等**（按 `source_table` 复用类、按 `(class_id, property_name)` 跳过属性）
- 上线前**跑一致性脚本**确认 PG / Neo4j / Milvus 三处对齐
- 删除属性走 service 内置 cascade（Neo4j `DETACH DELETE` + PG row delete）
- Milvus 必须 `--cleanup` 删集重建（避免批量 delete-then-insert 残留重复）

## 四、上线前验收清单（10 项必过）

| # | 检查项 | 工具 / 命令 |
|---|---|---|
| 1 | 物理表 schema 抽取（含 PK/FK/索引/枚举值） | `scripts/diag_<table>_schema.py` |
| 2 | 类 + 属性 + JOIN 在 `seed_ontology.py` 声明并 idempotent 通过 | `uv run python seed_ontology.py` |
| 3 | PG / Neo4j / Milvus 三处数量一致 | `verify_ontology_consistency.py` |
| 4 | 向量语义检索命中（用 5~10 个真实问法测试） | 手工 NL2SQL 验证 |
| 5 | SQL Guard 通过（仅 SELECT、5000 行兜底、无注入字符） | `/api/v1/chat` 端到端 |
| 6 | 派生指标 `formula` 必填守约 | `test_formula_with_valid_properties_passes` |
| 7 | 多步上下文（Top N → WHERE IN）端到端验证 | 显式多步 prompt 测试 |
| 8 | 系统技术字段已显式排除（`EXPNUM / AUUID / CREDATTIM`） | Excel `不需要=1` 列标记 |
| 9 | 变更记录到 `Harness/changes/feat-<name>/summary.md` | 9段模板（背景/设计/数据/接口/实现/测试/安全/部署/关联） |
| 10 | 回归测试覆盖率 ≥ 80% | `uv run pytest --cov=app --cov-fail-under=80` |

## 五、典型反例（不要这样做）

| 反例 | 后果 | 正例 |
|---|---|---|
| `class_alias = "采购订单(PO)"` 含特殊字符 | LLM 召回异常 | `采购订单` |
| `description = "订单表"` 太短 | 向量召回率低 | 「采购订单主表，记录订单号、日期、供应商、采购员与交货条款等。」 |
| `aliases = []` 空列表 | 同名列歧义无法消解 | 至少 1 个业务别名 |
| 用 `EXPNUM_0` 当主键 | 系统导出号非业务键，JOIN 错位 | 用 `POHNUM_0` 等业务单号 |
| `type = "VARCHAR"` | 不在白名单，校验失败 | `STRING` |
| 删属性不走 service，直接 SQL | Neo4j / Milvus 立即出现孤儿 | `OntologyService.deleteProperty` + commit-by-id |
| 不写 `formula` 的占比指标 | `validatePlan` 拦截，Step 失败 | 显式填 `SUM(x)/SUM(SUM(x))OVER()` |
| 类与已存在类同名 | Milvus id 碰撞互删 | 唯一 `class_name` |

## 六、关联

- 既有变更参考：
  - `Harness/changes/feat-ppricconf` / `docs/20260814-PPRICCONF-ontology.md` — 新增类的完整范例
  - `Harness/changes/feat-excel-not-needed-properties` — Excel `不需要=1` 字段裁剪
  - `Harness/changes/fix-nl2sql-derived-metric-formula-required` — 派生指标必填
  - `Harness/changes/fix-supplier-price-dimensions` — 同名异义消歧
  - `Harness/changes/fix-semantic-class-retrieval` — 向量语义召回
- 数据模型：`Harness/wiki/data-model.md`
- 业务流程：`Harness/wiki/business-domain.md`、`Harness/wiki/supplier-receipt-workflow.md`
- NL2SQL 引擎：`Harness/wiki/nl2sql-engine.md`
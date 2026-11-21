# 变更：采购平台 V0.3 · M0.5 前半 —— KPI 12 条归因 + `property_alias` 映射（A6）

- **日期**：2026-11-18
- **作者**：Claude
- **Phase**：M0 底座 / W2 轨 A（A6）
- **状态**：done（**已合入 main**，分批合入 1/4；**生产未部署**；alias `--apply` **未执行**，与 A9 同批）
- **关联变更**：[2026-11-11-kpi-definition-consumption](../2026-11-11-kpi-definition-consumption/summary.md)（P0 normalizer，本批的**前提**）；[2026-11-18-procurement-m0-answer-parity](../2026-11-18-procurement-m0-answer-parity/summary.md)（A5）
- **迁移版本**：无 DDL / 无数据迁移（head 仍 `0153`，`0154` 留给 A4-0）
- **验收包**：[docs/update/1118/M0/M0.5-KPI归因清单.md](../../../docs/update/1118/M0/M0.5-KPI归因清单.md)
- **证据**：[kpi-attribution-probe.txt](../../../docs/update/1118/M0/evidence/kpi-attribution-probe.txt)、[kpi-attribution-with-category.txt](../../../docs/update/1118/M0/evidence/kpi-attribution-with-category.txt)、[property-alias-dryrun.txt](../../../docs/update/1118/M0/evidence/property-alias-dryrun.txt)
- **计划**：[并行轨与汇合点 §3 轨 A · A6](../../../docs/update/1118/智能采购平台_V0.3落地实施计划_并行轨与汇合点_1118.md)

> 用户已拍板：**生产暂不部署**；**功能需要全部完成**；**本地全绿后统一部署**。
> 决策 **D-2**：本地档通过即可开闸。决策 **D-3**：未确认的候选**不得被生产读路径消费**。

---

## 1. 需求

计划 A6 行要求两件事：

1. **12 条 KPI 逐条归因**（治理层现状 / 技术层现状 / 差异归因 / 缺什么），每条落到
   「治理层需改 / 技术层需改 / 仅列名不同（回填即可）/ 真口径分歧」四类之一；
2. **采购域 `property_alias` 回填**（计划当时判断：「4182 条属性只有 8 条有 alias，
   全是 ASSET 域 ⇒ 这就是 12/12 mismatch 的真根因」）。

本批的结论是：**第 2 条的前提不成立**，第 1 条才是真工作量。

---

## 2. 设计评审

### 2.1 前提修正（本批最重要的产出）

计划写「12/12 mismatch」。2026-11-18 用与 chat 运行时同源的参数复测
（`reconcileMetric(governanceSpecOf, techniqueOf, resolution)`）：

```text
8 pass / 4 unknown / 0 mismatch
```

原因：`2026-11-11-kpi-definition-consumption` 的 `kpi_governance_normalizer --apply`
已把治理侧结构化列（`numerator_columns` / `denominator_columns` / `grain_keys`）
**改写成技术 measure 的物理列名**并落库（`revision_count=2`）。⇒ 列名在**结构化口径**上
已经对齐，`property_alias` 不是对账阻塞项。

由此产生一个可验收的硬结论：**「仅列名不同」= 0 条**。这直接推翻 F5 行，
并解释了为什么 alias 回填做完也不会把 verdict 从 mismatch 变 pass（本来就不是 mismatch）。

### 2.2 归因按「下一步谁动手」分类，而不是按现象

- **技术层需改**：治理定义明确，实现偏离（SQL / measure / 绑定 / 时间参数化）；
- **真口径分歧**：双方都有依据，必须业务拍板；
- **治理层需改**：治理侧定义/字段本身缺或错，技术无从实现；
- **仅列名不同**：语义等价，加 alias 即闭合。

用这把尺子，12 条的分布是 **1 / 9 / 0 / 2**（治理 / 技术 / 仅列名 / 真分歧）。

### 2.3 alias 回填的定位：展示 + resolver 阶段 3，不是口径修复

`property_alias` 影响两处：`display_name_resolver` / `nl2sql_schema` 的列名位（展示），
以及 resolver 的**阶段 3**（阶段 1/2 已命中时根本走不到）。所以：

- 回填**不改变**当前 12 条的对账结论（脚本在写库前**模拟前后各算一遍**来证明）；
- 回填会让 live alias ≠ `semantic_release.manifest_json.classes[].properties[].propertyAlias`
  ⇒ `ENABLE_SEMANTIC_RELEASE` 的 OFF（直读）与 ON（快照）分叉 ⇒ **`--apply` 必须与 A9 发布同批**。

### 2.4 只回填 32 / 45 个列

45 个引用列中 13 个是**子表里的 join 键副本**（`PO_NO` / `PO_LINE_NO` / `SUPPLIER_CODE` 等）。
给它们建 alias 会在同一 KPI 作用域内与主表 alias 冲突 ⇒ resolver 返回 unknown ⇒
**把本来能解析的 token 变不可判定**。所以按「主表持有规范名，子表副本留空」处理，
并在 dry-run 里做**作用域歧义机械校验**。

---

## 3. 数据模型变更

无 DDL、无迁移。本批是只读探针 + 纯函数 + 一个默认 dry-run 的回填脚本，
以及 `docs/update/1118/M0/` 下的两个数据文件。

`--apply` 若执行，只更新 `ontology_property.property_alias`（32 行），且只写空值行。

---

## 4. 接口契约变更

| 契约 | 变更 | 处置 |
|---|---|---|
| `kpi-attribution.json` | 新增（`{kpi_code: {category, note}}`，键集必须与库内 12 条完全一致） | 探针 `--attribution` 校验：缺/多/非四类值 ⇒ 退出 2 |
| `property-alias-mapping.json` | 新增（`{version, scope, rules, excluded, entries}`，条目带 `className`/`sourceColumn`/`alias`/`rationale`） | 回填脚本 `parseMapping` 校验；重复/空值 ⇒ 拒绝 |
| `backfill_property_alias.py` CLI | 新增：`--database-url` / `--mapping` / `--apply` / `--allow-prod-write` | 退出码 0 正常 / 1 拒绝 / 2 落库复核失败 |
| 既有退出码（A5/D17） | **不变**，本批不新增语义 | — |

---

## 5. 实现要点

| 文件 | 职责 |
|---|---|
| `backend/scripts/kpi_metric_attribution_probe.py` | 只读探针：sqlglot 抽取 6 处引用列（measure.expr / measure.filter / formula / filter_expression / grain_keys / time_column）→ 本体覆盖；reconcile（带 resolution）；四分类渲染与校验；`--json` |
| `backend/scripts/backfill_property_alias.py` | alias 回填 CLI：读映射 → 规划 → 作用域歧义 → **模拟对账** → dry-run 报告 / `--apply` 单事务写库 + 读回复核 |
| `backend/app/services/kpi_property_alias_backfill.py` | 纯函数层：`parseMapping` / `planAliasFill`（不覆盖）/ `findScopeCollisions` / `renderReport` |
| `backend/app/tests/unit/test_kpi_metric_attribution_probe.py` | 16 条：抽取、字面量、六处来源、不可解析/中文说明 ⇒ None、表名可判性、覆盖判定反向守卫、四分类 |
| `backend/app/tests/unit/test_kpi_property_alias_backfill.py` | 19 条：不覆盖、写错/歧义必报、作用域冲突（含**存量 alias 撞车**）、覆盖率、反向守卫（不同作用域不算冲突） |
| `backend/app/tests/integration/test_kpi_property_alias_backfill_apply.py` | 3 条（真 PG）：人工 alias 不被覆盖且整体回滚 / 空 alias 填充并读回复核 / 墓碑同名类不被写 |
| `docs/update/1118/M0/kpi-attribution.json` | 12 条四分类 + 归因 note |
| `docs/update/1118/M0/property-alias-mapping.json` | 32 条 alias + 13 条留空理由 |

关键实现选择：

- **不可判 ≠ 无引用**：SQL 解析失败时 `referenceTokens` 返回 `None`，渲染成
  「不可判（SQL 解析失败）」，不给空集结论（本仓反复踩过的假绿）。
- **`I.RESULT = 'FAIL'` 里 `RESULT` 是列**：只剔字符串字面量与函数名，谓词列必须保留，
  否则「本体缺 RESULT 列」会被漏报（单测最初写错，已按实现语义修正）。
- **模拟与落库同语义**：`_applyAliases` 只在 `property_alias` 为空时注入，
  与 `applyPlans` 的 `WHERE alias IS NULL OR btrim(alias)=''` 对齐。
- **写库前重算对账**：verdict / resolved / unknown / missing 任一变化 ⇒ 拒绝写库。

---

## 6. 测试

### 6.1 单测

```text
./scripts/run_local_tests.sh app/tests/unit/test_kpi_metric_attribution_probe.py    → 16 passed
./scripts/run_local_tests.sh app/tests/unit/test_kpi_property_alias_backfill.py     → 19 passed
./scripts/run_local_tests.sh app/tests/integration/test_kpi_property_alias_backfill_apply.py → 3 passed
```

### 6.2 实测（2026-11-18，本地开发库 PG 5433 / `qa_metadata`，只读）

```text
归属探针（12 条）             verdict = pass 8 / unknown 4 / mismatch 0
分类计数                       治理层需改=1 / 技术层需改=9 / 仅列名不同=0 / 真口径分歧=2
本体覆盖                       8 条 ESCAPE_HATCH：缺列=无；SQL 表不在源类=无
                               4 条 NONE：两栏均「不可判」（不是「无」）
alias dry-run                  fill=32 keep=0 冲突=0（存量 0）；覆盖率 entries∪excluded ⊇ 引用集
模拟对账                       9 条参与，解析/对账变化 0 条
```

### 6.3 覆盖率

纯函数层（映射解析 / 规划 / 冲突判定）由 13 条单测覆盖；
探针抽取层由 12 条单测覆盖。脚本的 IO 编排（读库/写库）不在单测范围内，
由 dry-run 实测 + 读回复核承担。

---

## 7. 安全审查

- **只读探针**：`kpi_metric_attribution_probe.py` 不写库；DSN 打码输出；
  白名单只认 `qa_metadata` / `qa_metadata_test*`。
- **写库闸门**：`backfill_property_alias.py` 默认 dry-run；`--apply` 需
  `--allow-prod-write`；落库前校验 `current_database()` 与 DSN 目标一致；
  **写正式库前强制备份 `ontology_property`**（`db_snapshot_guard.ensureBackups`，
  四道闸的闸 4）；`WHERE alias IS NULL OR btrim(alias)=''` 保证不覆盖人工 alias；
  `rowcount != 1` ⇒ 整体回滚；写前还有「存量 alias 冲突」「模拟对账变化 0」
  「模拟覆盖齐全」三道只读闸。
- **假的"绿"已被堵死**：中文口径说明不再被当成 SQL（`exp.Query` 判定）、
  空集模拟不再当通过、覆盖率不再靠文档手工对账。
- **证据脱敏**：三份证据文件里的 DSN 均为 `qa_user:***@host` 形式，无口令。
- **无新增外部输入面**：映射是本地文件，做结构校验；探针不执行生成的 SQL。

### 7.1 代码评审（code-reviewer，2026-11-18）

结论 **WARNING**：CRITICAL 0 / HIGH 1 / MEDIUM 5 / LOW 3。HIGH 与全部 MEDIUM 已修，
并补了真库回归；LOW 逐条处置如下。

| # | 级别 | 问题 | 处置 |
|---|---|---|---|
| 1 | HIGH | `findScopeCollisions` 只看本批 fills，**看不到库内已有 alias** ⇒ 「回填值撞存量」时不报冲突，写完后 resolver 返 unknown（评审已复现） | 新增 `existingAliases` 参数：有效 alias = 存量 ∪ 本批 fills；冲突组含 fills 才算 `isIntroduced` 并拦写库。单测改为正向断言（`test_本批回填与库内已有alias冲突必须报出`）+ 存量冲突只报不拦的反向用例 |
| 2 | MED | 模拟对账在空集上通过（`_loadPropertiesByMetric` 返回空 ⇒ 「0 条参与、变化 0」⇒ `--apply` 照写） | 由 fills 推导**应有**参与集合（scope 命中 fill 列），实测参与集合缺任一条或为空 ⇒ 拒绝写库 |
| 3 | MED | `_tablesOutsideSources` 把「解析失败」折成空集 ⇒ NONE 类指标渲染「SQL 表不在源类=无」（假绿） | 返回 `list \| None`，渲染「不可判（无 SQL 或解析失败）」。**深挖发现**：sqlglot 会把中文口径说明解析成 `Div` 表达式（不报错）⇒ 新增 `_statementTokens`：`formula` 必须是 `exp.Query` 才算可判。4 条 NONE 指标的证据从「无」变为「不可判」 |
| 4 | MED | `--apply` 写正式库没有备份（违反 guard 模块闸 4） | 复用 `db_snapshot_guard.ensureBackups`：`--apply` 且库名 = `qa_metadata` 时先备份 `ontology_property`（缺失即 `pg_dump`，拿不到拒绝写） |
| 5 | MED | 目标按**非唯一** `class_name` 定位，且未处理墓碑类 | `PropertyRow`/`AliasPlan` 增加 `class_id`，UPDATE / 复核一律按 `class_id` + 精确列名；同名类多条有效记录 ⇒ 直接报错（不猜）。**另发现**：采购域 8 个源类在 dev 库 `valid_to` 非空却仍被 `ontology_metric_source` 引用 ⇒ 目标筛选改为「有效类 ∪ 被指标引用的类」，运行时 resolver 本身不按 `valid_to` 过滤（模拟须同源） |
| 6 | MED | 无真库测试钉住「不覆盖人工 alias」 | 新增 `app/tests/integration/test_kpi_property_alias_backfill_apply.py`（3 例）：人工 alias 不被覆盖且整体回滚 / 空 alias 填充并复核 / 墓碑同名类不被写 |
| 7 | LOW | 探针 docstring 写了不可达的 exit 1 | 改为「0 正常；2 用法/白名单/attribution 校验失败」 |
| 8 | LOW | 回填脚本 docstring 写「12 条各算一遍」，实际 9 条 | 改为「有源类的 KPI（当前 9 条）」 |
| 9 | LOW | `kpi-attribution.json` 把 PAYMENT 的列名差异归因给 alias | 改为「P0 normalizer 的 identity 阶段命中，不是 alias（dry-run keep=0）」 |
| 10 | LOW | `excluded` 段没有任何校验，「45 = 32 + 13」只靠手工对账 | 新增 `parseExcluded` + `assertMappingCoverage`：probe 口径抽出的每个 (源类, 物理列) 必须被 `entries ∪ excluded` 覆盖，否则拒绝 |
| 11 | LOW | `E-回收登记.md` 仍写「12/12 mismatch」与 §1 矛盾 | **未改**：5 个问卷文件是用户编写、不提交（见 §9），此条转给问卷 owner |

> 评审环境备注：评审 agent 未用 `scripts/run_local_tests.sh`，裸 pytest 找不到
> `qa_metadata_test` ⇒ 它自己没跑成测试。本批用统一入口复跑：**38 passed**
> （unit 35 + integration 3）。

---

## 8. 部署验证

**未部署**（按用户决策）。本批无 DDL，本地无需重建库。

真实数据验证：12 条来自本地开发库的实际 `kpi_catalog` / `ontology_metric` /
`ontology_metric_source` / `ontology_property`；8 条 ESCAPE_HATCH 的 SQL 用 sqlglot
`oracle` dialect 解析后与源类物理列逐一对齐，零缺口。

---

## 9. 关联

- 归因清单：[M0.5-KPI归因清单.md](../../../docs/update/1118/M0/M0.5-KPI归因清单.md)
- 上游：P0 normalizer（[2026-11-11-kpi-definition-consumption](../2026-11-11-kpi-definition-consumption/summary.md)）
- 上游：A5（[2026-11-18-procurement-m0-answer-parity](../2026-11-18-procurement-m0-answer-parity/summary.md)）
- 下一步：
  - **A7** 口径槽位模板（输入 = 本清单 §3 的「缺什么」列）；
  - **A8** 源绑定候选（合同价 vs 价目表价必须带「待确认」标记，D-3）；
  - **A9** 采购域首次发布（**alias `--apply` 与重发布同批**）。
- **问卷 A 已回收（2026-11-18，工作副本未提交）**：答案把分类账从 `1/9/0/2` 推向
  `治理层需改 3 / 技术层需改 8 / 真口径分歧 0 / 摘除 1`（NCR 暂不可算）。主要修正：
  基准价 = **历史均价（过去 1 年）**（不是合同价也不是价目表价 ⇒ 治理与实现都要改）；
  付款及时率用**付款申请日**；采购周期要**按金额加权** + 中位数；
  **自然月 = 25 日 0 时 → 次月 25 日 24 时**（影响所有时间口径）；
  `承诺交货日期` 当前**值为空**（OTD / 逾期 / 延误三条的数据缺口）；
  A4.1 与 A4.2 关于「提前到货是否计入」**自相矛盾**，需回问。
  详见清单 §7。
- **登记不阻塞**：
  - 5 条缺 `time_column`（OTD / OVERDUE / CYCLE_TIME / DELAY_DAYS / PAYMENT_ON_TIME）—— 具体取哪个日期列待 A7/问卷确认；
  - 质量域（DEFECT / FPY）需要数仓先确认表存在（问卷 C）；NCR 已按回答摘除首期；
  - `DWD_PURCHASE_CONTRACT` 在本体不存在 —— 答案为「历史均价」后，合同价建模不再是前置条件。

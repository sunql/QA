# M0 问卷摄入包（G1 汇合）

> 日期：2026-11-18 ｜ 分支 `feat/procurement-m0` ｜ **未部署**（沿用 D-2：生产暂不部署，功能全完成后统一发版）
> 输入：`docs/update/1118/M0/业务问卷/` A/B/C/D **四卷问卷** + E **回收登记**（用户编写，**不提交本仓**）
> 上游：[`../M0.5-KPI口径槽位模板.md`](../M0.5-KPI口径槽位模板.md)（A7 · 13 槽位）｜[`../A8-采购源绑定候选与表结构设计.md`](../A8-采购源绑定候选与表结构设计.md)（A8 · 34 候选）

---

## 0. 这个包解决什么问题

问卷回答是**业务口径的权威来源**，但它描述的是**业务看到的现象**，不是**数据里的事实**。
本包把 172 条回答转成三样**可被机器检查**的东西：

1. **逐题登记**（哪条答案被采纳 / 带条件 / 需回问 / 不可算 / 落在 13 槽位之外）
2. **填好的槽位**（12 KPI × 13 槽位，每个槽位带取值 + 依据 + 状态）
3. **冲突与降级登记**（哪些答案互相矛盾、哪些数据缺口导致降级、哪些必须回问）

关键纪律：**每一个数据断言都到 THBI Oracle / SRM MySQL 实测核对过**，不照抄问卷散文。
实测推翻或修正了问卷中 **6 处判断**（见 [`source-capability.md`](./source-capability.md) §4）。

---

## 1. 文件清单

| 文件 | 内容 | 规模 |
|---|---|---|
| [`answers-registry.md`](./answers-registry.md) | 172 题逐题登记（卷 + 题号复合键） | A/B/C/D 四卷 + 统计 |
| [`kpi-slots-filled.json`](./kpi-slots-filled.json) | 12 KPI × 13 槽位，机器可读 | 156 槽位 |
| [`source-capability.md`](./source-capability.md) | SRM + THBI 源侧实测能力 + 12 KPI 可算性裁决 | 5 节 |
| [`conflicts-and-degradations.md`](./conflicts-and-degradations.md) | C-01…C-22 冲突 / G-01…G-05 缺口 / R-01…R-17 降级 / Q1…Q18 回问 | 5 节 |
| [`../evidence/source-capability-scan.txt`](../evidence/source-capability-scan.txt) | 只读探针原始输出（405 行） | 证据文件 |
| [`../../../../../backend/scripts/check_procurement_answer_package.py`](../../../../../backend/scripts/check_procurement_answer_package.py) | **包内一致性检查器**（入口：汇总 + 退出码契约） | v4 |
| [`../../../../../backend/scripts/check_procurement_answer_package_lib.py`](../../../../../backend/scripts/check_procurement_answer_package_lib.py) | 检查器**解析层**（口径常量 + Markdown/JSON 解析，只依赖标准库） | 解析层 |
| [`../../../../../backend/scripts/check_procurement_answer_package_checks_a.py`](../../../../../backend/scripts/check_procurement_answer_package_checks_a.py) | 检查器**断言层 A**（槽位模型 ↔ §9.1 表格 ↔ DB 快照） | 8 项断言 |
| [`../../../../../backend/scripts/check_procurement_answer_package_checks_b.py`](../../../../../backend/scripts/check_procurement_answer_package_checks_b.py) | 检查器**断言层 B**（登记表 ↔ 对外文档） | 8 项断言 |
| [`../../../../../backend/scripts/check_procurement_answer_package_mutations.py`](../../../../../backend/scripts/check_procurement_answer_package_mutations.py) | **检查器的变异反证**（注入 36 种已知缺陷，证明检查器不是「永远绿」） | 36 个变异 |

### 复跑

```bash
# ① 源侧事实（只读探针，幂等）
cd backend && uv run python scripts/probe_procurement_source_capability.py \
  --out ../docs/update/1118/M0/evidence/source-capability-scan.txt

# ② 包内一致性（无依赖，秒级）
python3 backend/scripts/check_procurement_answer_package.py

# ③ 变异反证（证明 ② 不是摆设；只动临时目录，不碰仓库）
python3 backend/scripts/check_procurement_answer_package_mutations.py
```

探针纪律：全部语句是 `SELECT` 聚合；走 `BusinessDbAdapter.execute_read_only`（SQL Guard 只读路径）；
口令由系统 Fernet 解密，**不打印、不落盘、不进 argv**；证据文件不含真实 DSN。

> ⚠️ **一致性检查器不是摆设**：本包交付前自查发现 §9.1 表与 JSON 有 26 处字形漂移，
> 且 `summary.slotCounts` 写的是 `confirmed 96 / conditional 22 / open 26 / blocked 8 / n/a 4`，
> 而逐槽位重算是 **79 / 20 / 33 / 9 / 15**（总数 156 一致 ⇒ 只错在分项）。
> 已按「JSON 为准、表由 JSON 生成」修正，并由检查器把这条不变量变成断言。
> 同理，登记表对外承诺的「22 冲突 + 5 缺口 + 17 降级 + 18 回问」也由编号连续性断言守住。
>
> 检查器经代码评审后**重写三次**：v1 有 3 处假绿（只比计数不比集合 ⇒ 重复/越界条目能混过去）；
> v2 改为按集合断言，并补了「崩溃不等于通过」（缺文件/坏 JSON/表行少列 → 转成可读失败项）；
> v3 再补 3 处评审发现 —— ① `time_column` 取值改为**等值**比对（原先用包含判断，
> `RECEIPT_DATE` 是 `PROMISED_RECEIPT_DATE` 的子串 ⇒ 日期列选错会漏过去，这正是 C-04 的坑）；
> ② 统计块的「触发回问」与 P0/P1 拆分改为与冲突文档 §4 实际行数对账；
> ③ 反证脚本先断言**原始副本必须全绿**，且**崩溃不算抓到缺陷**（只接受产物层面的失败信息）。
>
> **v4（本次）**把断言从 8 项扩到 **16 项**，并拆成「解析层 `_lib` + 断言层 `_checks_a`/`_checks_b`」
> （单文件 < 800 行的硬上限）。新增的 8 项：§9.1 表头**列序**、§9.1 **行序**、图例**映射对**完备、
> `metricId ↔ kpiCode` 与 DB 快照对账、§2 **值域 + 必填条件 + 业务不变式**（RATIO 必有分子分母、
> MEASURE 分母必须为空、`confirmed` 必有值、`not_applicable` 必无值）、槽位 `answers` 回指、
> 处置↔落点交叉引用、README 逐 KPI 状态 ↔ JSON 三分桶、证据/文件清单存在性。
> 反证脚本同步扩到 **36 个变异**，并新增「**必须命中预期关键词**」这一条
> （不能靠一个无关的检查项顺便变红蒙混过关）。
>
> **v4 顺手抓出并修掉的真实缺陷**：① `KPI_SUPPLIER_MAINTENANCE_RATIO` 的 `time_grain` / `time_column`
> 标了 `confirmed` 却取值为空（与 §4.10「当前时点快照 ⇒ 两者应保持为空」矛盾）⇒ 改判 **`not_applicable`**；
> ② §9.1 表的 **行序**与 JSON `kpis` 数组不一致（`INVOICE_MATCH_RATE` 与 `PAYMENT_ON_TIME` 互换）
> ⇒ 已按 JSON 顺序对齐。两条都被断言钉住（`confirmed` ⇒ 必有值；`not_applicable` ⇒ 必无值）。
> 36 个变异全部被捕获，见 ③。

---

## 2. 结论摘要

### 2.1 一句话

**12 条 KPI 里 4 条现在就能落槽发布，6 条需要回问后落，2 条降级或摘除；所有源绑定仍是 D-3 意义上的「候选」，未经 C 卷第 3 部分人工确认不得进生产读路径。**

### 2.2 逐 KPI 状态

| KPI | 状态 | 关键约束 |
|---|---|---|
| `KPI_SUPPLIER_OTD` | ✅ 可算 | 读法待 Q3；归属期用 `PROMISED_RECEIPT_DATE`（否则未到货行掉出分母） |
| `KPI_SUPPLIER_OVERDUE_RATIO` | ✅ 可算 | 与 OTD 同源同分母 |
| `KPI_INVOICE_MATCH_RATE` | ✅ 可算 | `INVOICE_DATE` 在发票**头**表 ⇒ 必须头行 JOIN |
| `KPI_SUPPLIER_DEFECT_RATE` | ✅ 可算 | **原判「不可算」推翻**：源类 `DWD_ARRIVAL_ORDER_DTL` 已存在（81,754 行） |
| `KPI_SUPPLIER_DELAY_DAYS` | ⚠ 待回问 | A4.1/A4.2/A4.3 三问互斥（Q4） |
| `KPI_PURCHASE_CYCLE_TIME` | ⚠ 待回问 | 起点可精确匹配但只覆盖 13.8% ⇒ 只统计可匹配行 + 输出覆盖率（Q12） |
| `KPI_PURCHASE_PRICE_VARIANCE` | ⚠ 待回问 | 基准价覆盖 41.2%，且源要换（Q14） |
| `KPI_COST_SAVING` | ⚠ 待回问 | 同上 + 涨跌要拆两条指标 |
| `KPI_PAYMENT_ON_TIME` | ⚠ 待回问 | 「付款申请日」无列 ⇒ `BILL_DATE` 代理（Q5） |
| `KPI_SUPPLIER_FPY` | ⚠ 待回问 | 单位冲突：行数 vs 数量（Q6） |
| `KPI_SUPPLIER_MAINTENANCE_RATIO` | ❌ 降级 | 品类准入表 0 行 + 无停用列 + 码义未知 ⇒ 只读台账展示 |
| `KPI_SUPPLIER_NCR_RATE` | ❌ 摘除首期 | 质量数据未进系统 |

### 2.3 实测推翻的 6 处问卷判断

| # | 问卷断言 | 实测 | 影响 |
|---|---|---|---|
| 1 | A2.1「承诺交货日期为空」 | THBI `PROMISED_RECEIPT_DATE` 填 **99.9998%**；空的是 SRM 的 `required_delivery_date`（0.65%） | **解除** OTD/逾期/延误三条的降级 |
| 2 | A7 §4.5「基准价 = `DWD_SUPPLIER_PRICE_LIST`」 | 该表 `MATERIAL_CODE` **100% NULL** | 基准价源改为 `DWD_PURCHASE_QUOTATION_YEARLY_DTL` |
| 3 | C4.3「来料检验：没有」 | 只对 SRM 空表成立；THBI `DWD_ARRIVAL_ORDER_DTL` 有 81,754 行 | A9/A10 改判为「源类已存在」 |
| 4 | C4.4「NCR：没有」 | 成立 | A11 维持摘除 |
| 5 | A6/A7 §4.9「本体没有来料检验类」 | 本体无该类，但**物理表存在且有数据** | 不必新建虚构类；补属性即可 |
| 6 | C4.1「采购合同：没有」 | 成立，且 `DWD_PURCHASE_CONTRACT` 在 THBI **不存在**（ORA-00942） | 治理层字段须改写 |

### 2.4 三个必须让业务知道的数字

1. **基准价覆盖率 41.2%**（81,020 / 196,753 行）⇒ A7.5「缺失按 0」会让成本节约**系统性低估约 59%**（R-09：首期不抹平，同屏输出覆盖率）。
2. **完成度是真实三档分布**：全完成 177,137 / 部分完成 9,244 / 未完成 10,372（2025 起）⇒ A1.1 的两种读法算出的数不同（Q3）。
3. **「付款申请日」在数仓里不存在**：`BANK_DATE` 填充 0%，`VALUE_DATE > BILL_DATE` 仅 6 笔 ⇒ 用 `BILL_DATE` 代理（Q5）。

### 2.5 两处结构性发现（比 6 处问卷修正影响面更大）

这两条不改问卷答案，改的是**绑定清单的范围**：

| # | 发现 | 影响 |
|---|---|---|
| **S-1** | **本体有两套并行采购建模**：指标公式引用的是 7 个**墓碑类**（`_LINE` 族），A8 的 34 候选是另一套 **`_DTL` 族** —— 两者是**不同的物理表**（`DWD_PURCHASE_ORDER_LINE` 927,648 行 vs `DWD_PURCHASE_ORDER_DTL` 1,023,442 行），且 `_DTL` 族**缺** `PROMISED_RECEIPT_DATE` / `ORDER_QTY` / `LINE_AMOUNT_EXCL_TAX` / `MATERIAL_CODE` / `RECEIPT_DATE` / `RECEIPT_NO` | **绑定范围 34 → 41 类**（＋7 个复活墓碑类）。只绑 34 个，9 条 KPI 会在库级断链。7 个墓碑类的物理列数 = 本体属性数 ⇒ **复活无列级缺口** |
| **S-2** | **PO↔请购有精确关联表**：`DWD_REQUISITION_ORDER_LINK`（138,118 行，键列 100% 填充），但只覆盖 2025+ 订单行 **13.8%**（27,159 / 196,753）、PO 覆盖 8,096 / 54,554 | 采购周期起点**可精确算**（原判「无关联键」被推翻），但首期须「**只统计可精确匹配的行 + 输出覆盖率**」，**不做近似匹配**（近似比精确+覆盖率更不可信） |

---

## 3. 与 D 系列决策的对应

| 决策 | 在本包中的落点 |
|---|---|
| D-2（生产暂不部署） | 本包全部内容标注 `deployed: false` |
| D-3（未确认候选不得被生产读路径消费） | `kpi-slots-filled.json` 的 `meta.candidateOnly`；R-05「绑定全部 `is_candidate = true`」 |
| D-4（口径权威以报表为准） | C-01：首期以**问卷 A** 为 SSOT，报表口径到位后做差异登记，不追溯改口径 |
| D2（两张表） | 不变；本包只产出槽位取值，不动表结构 |
| C-09（安全红线） | 业务「有所有权限」必须收敛为**只读账号**（Q7），口令走内部渠道，不入问卷正文 |

---

## 4. 下一步

1. **M1-1.1**：源绑定 service + CRUD API + 绑定向导（candidate → confirmed 迁移、复合键/有效期/过滤白名单校验）。
   绑定范围 **41 类**（34 候选 + 7 复活墓碑类，A8 §5.3），清单见
   [`../procurement-source-binding-candidates.json`](../procurement-source-binding-candidates.json)。
2. **M1-1.2**：`semantic_release_check` ⇒ 迁移 **0155**（0154 已被源绑定两张表占用；**A4-0 也需要 0155，两条须串行排号**）。
3. **回问 Q1–Q8（P0）** 交业务；P1 的 Q9–Q18 不阻塞开工。
4. 发布前把 `kpi-slots-filled.json` 中 `status: open` 的 **33** 个槽位补齐（`formula` 10 + `status` 11 + `metric_alias` 12），
   并把 `execution_source = NONE` 的 2 条指标留在发布闸门之外。

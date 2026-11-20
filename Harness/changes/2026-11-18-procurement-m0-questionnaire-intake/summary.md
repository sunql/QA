# 变更：M0.7（G1 汇合）—— 问卷摄入包 + 源侧实测

- **日期**：2026-11-18
- **作者**：Claude
- **Phase**：M0 采购语义可发布 / M0.6 问卷回收 → M0.7 摄入汇合
- **状态**：done（**已合入 main**，分批合入 1/4；**生产未部署**；纯文档 + 只读探针批）
- **关联变更**：[2026-11-18-m1-procurement-binding-tables](../2026-11-18-m1-procurement-binding-tables/summary.md)（M1-1.1 结构层，迁移 `0154`）· [2026-11-18-procurement-m0-source-binding](../2026-11-18-procurement-m0-source-binding/summary.md)（A8，本批修订其绑定范围）
- **迁移版本**：无（本批不含 DDL；迁移 `0154` 已由 M1-1.1 结构层占用，**`0155` 留给 M1-1.2 / A4-0 串行排号**）
- **产物**：`docs/update/1118/M0/answers/`（4 份）＋ `docs/update/1118/M0/evidence/source-capability-scan.txt`（405 行）
- **探针脚本**：`backend/scripts/probe_procurement_source_capability.py`（只读，可复跑）
- **MEMORY**：[qa-system-procurement-v03-landing-program.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-procurement-v03-landing-program.md)

> 本批把 **172 条问卷回答**转成 4 样机器可检查的产物，并在过程中用**源侧只读实测**
> 推翻了问卷中 6 处断言 + 发现 2 处结构性事实（后者改的是绑定范围，影响面更大）。

---

## 1. 需求

用户指令（2026-11-18）：「问卷都已经完成了，继续下一步吧，如果需要用到问卷的时候，去问卷目录获取就可以了」。

落地计划把「G1 汇合点」定义为 M0.6 回收后的汇合动作：**把问卷回答转成可被机器检查的结构化产物**，
而不是把散文抄进文档。验收标准（可测量）：

1. 每条回答都有**唯一处置**（采纳 / 带条件 / 需回问 / 不可算 / 缺口 / 落在槽位外），可逐题核对。
2. 12 条 KPI 的 **13 个槽位**全部有取值或显式标记「不适用 / 被阻塞」，机器可读。
3. 问卷中**每一条数据断言**都到 THBI Oracle / SRM MySQL 实测核对过，实测与问卷不一致时**以实测为准并登记差异**。
4. 冲突、缺口、降级、回问**各有一份登记表**，且每条降级都指向被影响的 KPI。
5. 产物可复跑（探针脚本 + 证据快照）。

---

## 2. 设计评审

### 2.1 候选方案 A：直接按问卷散文落槽位

- 做法：把 172 条回答逐条翻译成槽位值。
- 优点：快；与业务原话一一对应。
- 缺点：**问卷描述的是业务看到的现象，不是数据里的事实**。本次实测发现 6 处不一致，
  其中 A2.1「承诺交货日期为空」若照抄，会让 OTD / 逾期率 / 延误天数三条 KPI 被错误降级
  （实测 THBI `PROMISED_RECEIPT_DATE` 填 **99.9998%**）；A7 的基准价源若照抄，会用上一张
  `MATERIAL_CODE` **100% NULL** 的表。
- 结论：**否决**。

### 2.2 候选方案 B：只做登记，不落槽位（停在冲突表）

- 做法：只产出冲突 / 缺口 / 回问清单，等业务答完再落槽。
- 优点：零猜测。
- 缺点：与 D-3「并行轨」冲突 —— M1 结构闸已经在 `0154` 落地，继续停表会浪费回收窗口；
  且**技术可算性判断本身不需要业务答复**（哪些表有数据、哪些列填了多少，是事实问题）。
- 结论：**否决**。

### 2.3 候选方案 C（采纳）：问卷登记 + 源侧实测 + 槽位 JSON 三件套

- 做法：逐题登记（172 行）＋ 写只读探针实测 SRM/THBI 能力 ＋ 按实测结果落 12×13 槽位
  ＋ 冲突/降级/回问三张登记表。
- 取舍表：

| 维度 | A（照抄问卷） | B（只登记） | **C（登记＋实测＋落槽）** |
|---|---|---|---|
| 口径正确性 | ❌ 6 处会被照抄错 | ✅ | ✅（以实测为准，差异登记） |
| 并行轨不停摆 | ✅ | ❌ 停表 | ✅ |
| 猜测风险 | 高 | 无 | 中（标注 conditional + 回问） |
| 成本 | 低 | 低 | 中（一次探针 + 一次全量扫描） |

- **最终决定：方案 C**。核心纪律：**槽位取值分三档** ——
  `confirmed`（答案直接采纳）/ `conditional`（带代理口径、覆盖率、近似等条件）/ `blocked`（当前不可算）；
  一切 `conditional` 都必须写清条件与影响面，并落到回问清单。

### 2.4 探针纪律（不可放宽）

1. **只读**：全部语句是 `SELECT` 聚合（`COUNT`/`MIN`/`MAX`/`GROUP BY`），不取明细行。
2. **走系统自带只读通道**：`BusinessDbAdapter.execute_read_only`（SQL Guard 校验过的路径），
   不自己拼连接、不自己解密口令。
3. **不落密**：口令由系统 Fernet 解密后仅用于建连，**不打印、不落盘、不进 argv**；
   证据文件不含真实 DSN。
4. **目标库**：`--datasource-id` 指定，默认 THBI Oracle(1) + SRM-TH-01(10)。
5. **失败也取证**：每条探针独立捕获异常，失败以 `[ERROR]` 记入证据（本次有 2 条真的失败了：
   `LISTAGG ... ON OVERFLOW TRUNCATE` 被 SQL Guard 的关键字黑名单拦下 —— 改写为 pivot 形式后通过）。

---

## 3. 数据模型变更

**无。** 本批不改任何表 / 列 / 索引 / CheckConstraint，无迁移。

被本批**修订范围**的是既有结构（非 DDL）：

| 对象 | 变化 | 依据 |
|---|---|---|
| 源绑定清单 `procurement-source-binding-candidates.json` | `counts` 新增 `revivedTombstones=7` / `totalBindingScope=41`；新增 `revivedTombstones[]`（7 条，含物理表 / 行数 / 列存在性） | §6 的 S-1 |
| `procurement_source_binding` / `procurement_property_binding`（迁移 `0154`） | **不变** —— 但落库时的行数从 34 变 41 | 同上 |
| 迁移号 | `0154` 已被 M1-1.1 结构层占用 ⇒ **M1-1.2 与 A4-0 都需要 `0155`**，须串行排号 | A7 文档尾注同步校正 |

---

## 4. 接口契约变更

**无。** 本批不含 API / DTO / 状态机改动。

与下一批（M1-1.1 service + CRUD API）的接口输入已被本批固定：

| 后续接口要素 | 本批定下的取值 | 状态 |
|---|---|---|
| 绑定写入口 | `POST /procurement/binding/confirm`（candidate → confirmed） | 设计输入已定，实现归 M1-1.1 |
| 绑定记录条数 | 41（34 + 7） | ✅ |
| 复合键 | `class_id + datasource_id + schema_name + source_table`（同表双类靠 `class_id` 区分） | ✅ |
| 属性绑定键序号 | 4 列复合主键（`DWD_PURCHASE_QUOTATION_YEARLY_DTL` 等） | ✅ |
| 有效期 | `valid_from` / `valid_to` + 部分唯一索引 | ✅（迁移 `0154` 已建） |
| 过滤白名单 | 独立列，不进 JSON（D2） | ✅ |
| 候选隔离 | `is_candidate` / `confirmed_by` / `confirmed_time` 库级 CHECK（D-3） | ✅（迁移 `0154` 已建） |

---

## 5. 实现要点

### 5.1 产物（`docs/update/1118/M0/answers/`）

| 文件 | 内容 | 规模 |
|---|---|---|
| `README.md` | 包索引 + 纪律 + 结论摘要 + 复跑命令 | 4 节 |
| `answers-registry.md` | 172 题逐题登记（**卷 + 题号复合键**） | ✅89 / ⚠27 / ⛔16 / ◐11 / ∅20 / ▸8 |
| `kpi-slots-filled.json` | 12 KPI × 13 槽位（取值 + 依据 + 状态） | 156 槽位 = confirmed 77 / conditional 20 / open 33 / blocked 9 / n/a 17（另有 6 项补充注记槽，不计入） |
| `source-capability.md` | SRM + THBI 实测能力 + 12 KPI 可算性裁决 | 5 节 |
| `conflicts-and-degradations.md` | C-01…C-22 / G-01…G-05 / R-01…R-17 / Q1…Q18 | 5 节 |
| `backend/scripts/check_procurement_answer_package.py` + `_lib` + `_schema` + `_checks_a` + `_checks_b` | **包内一致性检查器 v5**（16 项断言：槽位计数 / 槽位取值·§2 值域·业务不变式 / §9.1 表逐格+取值+行序 / §9.1 表头列序 / §9.1 统计行 / 图例映射对 / metricId↔快照对账 / answers 回指 / 绑定类集合 / 登记逐行 / 回问分级 / 编号连续 / 处置↔落点 / README 摘要 / README 逐 KPI 状态 / 证据与文件清单）+ **结构前检层**（结构缺陷 ⇒ 退出码 1，不是 2） | 无依赖（纯标准库），秒级 |
| `backend/scripts/check_procurement_answer_package_mutations.py` | **检查器的变异反证**（注入 43 种已知缺陷 + 预期关键词断言 + **三态判定** `caught`/`escaped`/`tool-error`，证明检查器不是「永远绿」） | 无依赖，秒级 |

### 5.2 探针脚本 `backend/scripts/probe_procurement_source_capability.py`

- `Probe` 是 frozen dataclass（`title` + `sql`）；`PROBE_SETS: dict[int, tuple[Probe, ...]]` 按数据源分组。
- `runDatasource(session, datasourceId)` 逐条执行，**每条独立捕获异常**（失败也记证据）。
- `main()` 支持 `--datasource-id`（可重复）与 `--out`；engine 与 adapter 均显式 dispose。
- THBI 组含 60+ 条探针（行数 / 关键列填充率 / 值域 / 区间 / 覆盖率 / 两套命名族对照 / 关联表覆盖率），
  SRM 组 30+ 条。

### 5.3 关键算法：完成度三分档与基准价覆盖率

- 完成三分档：`RECEIVED_QTY >= ORDER_QTY` / 部分完成 / `RECEIVED_QTY` 为 0
  ⇒ **177,137 / 9,244 / 10,372**（2025 起 196,753 行）—— A1.1 两种读法都技术可算 ⇒ 不降级，只回问。
- 基准价覆盖率：用**预聚合去重集**做 `LEFT JOIN`（供应商+物料 / 仅物料）
  ⇒ **41.2% / 46.5%**。
  ⚠️ 直接写相关子查询会**超时**（200k×170k 行，30 s adapter 上限）—— 必须先 `SELECT DISTINCT` 再 LEFT JOIN。

### 5.4 题号碰撞处理

A 卷与 B 卷都用 `B2.x`–`B6.x` 编号但内容不同 ⇒ 登记一律用**「卷 + 题号」复合键**
（`A·B3.1` / `B3.1`），任何引用答案的地方必须带卷前缀（登记为 C-21）。

---

## 6. 测试

**无新增单元/集成测试**（本批不含代码逻辑，只有探针脚本与文档）。

| 验证项 | 方式 | 结果 |
|---|---|---|
| 探针可复跑 | `uv run python scripts/probe_procurement_source_capability.py --out …` | ✅ 405 行证据，含 2 条预期内的 `[ERROR]`（SQL Guard 关键字拦截，已改写规避） |
| 源侧事实 | 探针输出逐条核对 | ✅ 每条数据断言均可回指证据行 |
| 槽位 JSON 合法性 | `python3 -m json.tool` | ✅ 可解析 |
| 绑定清单计数 | `counts.totalBindingScope = 34 + 7 = 41` 与 `len(bindings)` / `len(revivedTombstones)` 对账 | ✅ 一致 |
| 问卷一致性 | 172 题登记 vs 原始问卷逐题比对 | ✅ 唯一题 172（A 76 + B 27 + C 40 + D 29），已答 152 / 空白 20 |
| **包内一致性** | `python3 backend/scripts/check_procurement_answer_package.py` | ✅ **16 项全过**（槽位计数 + scope 口径 / 槽位取值·§2 值域·业务不变式 / §9.1 表逐格+取值（**等值**）/ §9.1 表头列序 / §9.1 统计行 / 图例映射对 / metricId↔快照对账 / answers 回指 / 绑定类集合 34+7=41 / 登记逐行（题号·分卷·分节·处置）/ 回问 P0·P1 拆分 / C·G·R·Q 编号连续 / 处置↔落点 / README 摘要数字 / README 逐 KPI 状态 / 证据与文件清单） |
| **检查器反证** | `python3 backend/scripts/check_procurement_answer_package_mutations.py` | ✅ **43 个变异全部被捕获**，每个都 exit 1 且报的是**产物层面**的问题（**崩溃 / 退出码 ≠ 1 不算抓到缺陷**）；三态判定 `caught`/`escaped`/`tool-error` |

**覆盖率**：不适用（无代码变更）。

---

## 7. 安全审查

**触发 security-reviewer**（条件：外部 API 调用 / 加解密 / 凭据处理）。

| 级别 | 发现 | 处置 |
|---|---|---|
| **CRITICAL** | 无 | — |
| **HIGH** | 无 | — |
| **MEDIUM** | 探针会把 SQL 原文写进证据文件（含库名 / 表名 / 列名） | 业务元数据非个人数据；**无明细行、无个人字段** ⇒ 接受，记录在探针 docstring 的 4 条纪律里 |
| **MEDIUM** | 业务问卷 C2.3 写「有**所有权限**」 | **红线，不降级**（C-09 / Q7）：必须收敛为**只读账号**（`GRANT SELECT`、禁 DDL/DML、连接数上限、时段限制）；口令走内部渠道单独给，**不写进问卷正文**、不入仓 |
| **LOW** | 探针连接使用系统已解密的业务库凭据 | 不打印、不落盘、不进 argv；argv 只接受数据源 ID |

**证据文件口令检查**：全文无 DSN、无账号口令（`grep` 可验）。

---

## 8. 部署验证

**未部署**（沿用 D-2：生产暂不部署，功能全完成后统一发版）。

本批无 DDL、无代码改动 ⇒ 无需 compose 冒烟。

**真实数据验证脚本**：本批的「真实数据验证」就是探针本身 ——
`backend/scripts/probe_procurement_source_capability.py` 幂等可复跑，
输出落 `docs/update/1118/M0/evidence/source-capability-scan.txt`（405 行）。
两个数据源（THBI Oracle / SRM MySQL）均连通并取到真实行数与填充率。

---

## 9. 关联

- **设计稿 / 计划**：[`docs/update/1118/M0/README.md`](../../../docs/update/1118/M0/README.md) ·
  [`docs/update/1118/智能采购平台_V0.3落地实施计划_M0-M7_1118.md`](../../../docs/update/1118/智能采购平台_V0.3落地实施计划_M0-M7_1118.md)
- **本批产物**：[`answers/README.md`](../../../docs/update/1118/M0/answers/README.md) ·
  [`answers/kpi-slots-filled.json`](../../../docs/update/1118/M0/answers/kpi-slots-filled.json) ·
  [`answers/conflicts-and-degradations.md`](../../../docs/update/1118/M0/answers/conflicts-and-degradations.md) ·
  [`evidence/source-capability-scan.txt`](../../../docs/update/1118/M0/evidence/source-capability-scan.txt)
- **Wiki**：[`Harness/wiki/data-model.md`](../../wiki/data-model.md)（`ontology_class` 段新增「DWD 族两套并行命名」与 41 类绑定范围）
- **Rules**：[`Harness/rules/开发流程规范.md`](../../rules/开发流程规范.md) · [`Harness/rules/数据与AI治理规范.md`](../../rules/数据与AI治理规范.md) · [`Harness/rules/变更记录强制规范.md`](../../rules/变更记录强制规范.md)
- **变更（predecessor / successor）**：[`2026-11-18-m1-procurement-binding-tables`](../2026-11-18-m1-procurement-binding-tables/summary.md) ·
  [`2026-11-18-procurement-m0-source-binding`](../2026-11-18-procurement-m0-source-binding/summary.md)
- **MEMORY**：[`qa-system-procurement-v03-landing-program.md`](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-procurement-v03-landing-program.md)

---

## 附：6 处问卷断言被实测推翻 + 2 处结构性发现

| # | 问卷断言 | 实测 | 处置 |
|---|---|---|---|
| 1 | A2.1 承诺交货日期为空 | THBI 填 99.9998%（空的是 SRM 的 0.65%） | **解除** 3 条 KPI 的降级，改回问 |
| 2 | A7 基准价 = `DWD_SUPPLIER_PRICE_LIST` | 该表 `MATERIAL_CODE` 100% NULL | 基准价源改 `DWD_PURCHASE_QUOTATION_YEARLY_DTL` |
| 3 | C4.3 来料检验「没有」 | 只对 SRM 空表成立；THBI `DWD_ARRIVAL_ORDER_DTL` 81,754 行 | A9/A10 改判为「源类已存在」 |
| 4 | C4.4 NCR「没有」 | 成立 | A11 维持摘除首期 |
| 5 | A6/A7「本体没有来料检验类」 | 本体无该类，但物理表存在且有数据 | 不必新建虚构类，补属性即可 |
| 6 | C4.1 采购合同「没有」 | 成立，`DWD_PURCHASE_CONTRACT` ORA-00942 | 治理层字段须改写 |
| **S-1** | —（本体侧） | `_DTL` 族 ≠ `_LINE` 族，是不同物理表且缺关键列 | **绑定范围 34 → 41 类**（C-22） |
| **S-2** | —（本体侧） | `DWD_REQUISITION_ORDER_LINK` 存在（138,118 行），但只覆盖 13.8% 订单行 | 周期起点**可精确**但覆盖率低 ⇒ 只算可匹配行 + 输出覆盖率（C-19 改判） |

---

## 附二：交付前自查发现并修正的产物漂移

| 项 | 现象 | 处置 |
|---|---|---|
| `summary.slotCounts` | 写 `confirmed 96 / conditional 22 / open 26 / blocked 8 / n/a 4`，但**逐槽位重算是 79 / 20 / 33 / 9 / 15**（总数 156 一致 ⇒ 只错分项） | 以逐槽位为准改 JSON + 两处文档引用；并加 `scope` 说明「口径 = 13 模型槽位 × 12 KPI，不含 6 项补充注记槽」 |
| `M0.5 §9.1` 表 | 与 JSON 有 **26 处字形不一致**（`formula` 10 处、`metric_alias` 12 处、NCR `metric_type`/`status`、DEFECT `aggregation_order`、MAINT `time_column`） | **表改为由 JSON 逐格生成**（保留加粗与列内取值文本）；并写明字形↔状态词映射 |
| `answers/README.md` §4 | 「`status: open` 的 26 个槽位」 | 改为 **33**（`formula` 10 + `status` 11 + `metric_alias` 12） |
| 口径语义未标注 | JSON 未说明「记录的是**目标**口径状态，不是 DB 当前值」 | 加 `meta.stateSemantics`（含 DB 现状：11 条 `time_column` 为空、DEFECT/FPY `execution_source` NONE→ESCAPE_HATCH、CYCLE_TIME `agg_function` AVG→SUM、DEFECT_RATE COUNT→SUM） |

⇒ 为此新增 `backend/scripts/check_procurement_answer_package.py`，把上述不变量变成**可执行断言**（含变异反证）。

---

## 附三：检查器经代码评审后的重写（v1 → v2 → v3）

v1 检查器被 `code-reviewer` 评审出 **3 个 HIGH 假绿**（全部经变异复现：产物被改坏，检查器仍报「全部一致」exit 0）：

| # | 假绿 | 复现方式 | v2 处置 |
|---|---|---|---|
| HIGH-1 | 「34 + 7 = 41」只比**计数**，不比**类集合** | 把 `bindings[1]` 换成 `bindings[0]` 的副本（`classId` 相同）⇒ 条数仍是 34，但类并集塌成 40 | 按 `classId` 集合断言：不重复、bindings∩revived=∅、并集=41；另校验 `unique`/`schema-ambiguous` 分解 |
| HIGH-2 | §9.1 表**跳过**未知 KPI 码 + 只比行数 | 把一个 KPI 码写错、同时复制另一行 ⇒ 行数不变，但那行**完全没被校验** | 改为**集合相等**：未知码 / 重复行 / 缺行都报 |
| HIGH-3 | C/G/R/Q 编号用 `set()` 折叠 ⇒ 复制行不可见 | 复制 `C-05` 一行 ⇒ 行数 23、编号集合仍 {1..22} | 行数与唯一编号**分别**断言，且编号集合必须恰为 1..N |

同时修掉的 MEDIUM / LOW（v2）：

| # | 问题 | 处置 |
|---|---|---|
| M1 | 缺文件 / 坏 JSON / 表行少列 ⇒ 吐 traceback，且 `main()` 一次性构造 checks ⇒ **一项崩、整轮无输出** | 每项检查包 `try/except`，异常转成失败项；退出码 **2 = 检查器失败** / **1 = 内容不一致** / 0 = 通过 |
| M2 | `summary.slotCounts.total` 与 §9.1 汇总行从不被校验 | 都纳入断言（含 `◐`/`○` 的 `open` 拆分） |
| M3 | §9.1 表**取值**（如 `time_column` 的列名）不校验 ⇒ 写错日期列也不报 | `time_column` 的取值文本纳入比对（列名错 ⇒ C-04 那类缺陷就抓得住） |
| M4 | 登记表只读题号列 ⇒ 空白被改成「✅ 采纳」也不报 | 处置分布逐行重算 + 与 `## 统计` 块对账；分卷/分节标题题数也对账 |
| M5 | **登记文档自身矛盾**：C 卷标题写「39 题」，实际 40 题 | 已改为 40（**本包自身的真实缺陷**，由 M4 的断言兜住） |
| L1 | G 行正则要求加粗 ⇒ 改个样式就假红；`\d{2}` ⇒ 三位数编号不可见 | 加粗可选、`\d{2,3}`、分隔符宽松 |
| L2 | 期望值同时写在脚本常量与文档里 ⇒ 只跟常量比，抓不到「文档自相矛盾」 | 分卷/分节题数、统计块数字**从文档解析**，再跟逐行重算对账 |

v2 再经 `code-reviewer` 复审，又出 **3 个 HIGH + 5 个 MEDIUM + 5 个 LOW**（v3 处置）：

| # | 问题 | v3 处置 |
|---|---|---|
| **HIGH-1** | `time_column` 取值用**子串包含**判定 ⇒ `RECEIPT_DATE` ⊂ `PROMISED_RECEIPT_DATE`，**改 JSON（SSOT）也不报** | 取值改为**等值**比对（抽单元格里的反引号 token 精确比较）—— 这正是 C-04「统计期归属必须用承诺日」的坑 |
| **HIGH-2** | 反证脚本把**崩溃**（exit 2）也算「抓到缺陷」，且**不验证原始副本是绿的** | 反证脚本先跑**基线断言**（原始副本必须 exit 0），且缺陷型变异只接受**产物层面**的失败信息（出现「检查器异常」即判漏网） |
| **HIGH-3** | 统计块声明的「触发回问 / 触发降级」与「P0/P1 拆分」**无人校验** | 新增检查项 6：与冲突文档 §4 两个子节的**实际行数**对账 |
| MED-1 | 分节对账只单向（声明→实际），有行无声明的节被静默丢弃 | 改为**双向**（仅对确有 `###` 分节的 A 卷生效，B/C/D 是单表列题） |
| MED-2 | `_glyph()` 对未知状态词抛 `KeyError` | 改 `.get()` 返回 `None`，由调用方报「未知状态词」 |
| MED-3 | 反证脚本无法证明**检查 1 / 分节题数 / G·R·Q 编号**这些断言真的在跑 | 变异集扩到 18 个，新增 JSON `slotCounts` / 分节题数 / G·R·Q 编号 / README 摘要 / P0·P1 拆分等场景 |
| MED-4 | README 首页复述的摘要数字**无人校验** | 新增检查项 8：README 的 172 / 156 / 41 / 22·5·17·18 / 405 / 4+6 与被检查产物逐一对账（并顺手修了「五卷 vs 四卷」的表述矛盾） |
| MED-5 | 表行只要求「至少」13 格 ⇒ 多余列不报；按 `\|` 裸切会误伤 `\|\|` | 改为**恰好** 13 格；按**未转义**的 `\|` 切分 |
| LOW-1 | ID 正则 `(?:\s\|\|$)` ⇒ `C-05x` 会被当成 C-05；编号按 int 比 ⇒ `C-022` ≡ `C-22` | 改为 `(?:\s\|$)`；编号按**字符串**比 |
| LOW-2 | `publishableNow/AfterReask/blocked` 划分不校验 | 新增断言：三者无重叠、无遗漏、并集 = 12 条 KPI |
| LOW-4 | 反证脚本自身的 `next()` 无默认值（找不到 §9.1 会抛）、变异不落地时原因不明 | `next(..., None)` + 变异前后**内容摘要**比对，未落地即判「脚本自身出错」而非「漏网」 |
| LOW-5 | 「产物缺失」与「检查器自身异常」都走同一条 except | 分开：**产物缺失 / 产物损坏 → exit 1**（包不一致）；**检查器异常 → exit 2**（工具坏了） |

**变异反证**：`backend/scripts/check_procurement_answer_package_mutations.py` 注入 18 种已知缺陷
（5 HIGH 场景 + 10 MEDIUM + 2 崩溃场景 + 1 基线断言），要求检查器对每一个都非零退出
**且报的是产物层面的问题** ⇒ **18/18 全部捕获**，证明检查器不是「永远绿」。
反证脚本只动临时目录，不碰仓库产物。

---

## 附四：检查器第三轮重写（v3 → v4）：16 项断言 + 36 个变异

第三轮评审（3 轮 code-reviewer）后把断言从 8 项扩到 **16 项**，并按单文件 < 800 行的硬上限
拆成「解析层 `_lib`（718 行）/ 断言层 `_checks_a`（510 行）/ `_checks_b`（391 行）」+ 入口（106 行）。

### 4.1 新增的 8 项断言

| # | 断言 | 抓什么 |
|---|---|---|
| 9 | `metricId ↔ kpiCode` 与 `evidence/kpi-slot-snapshot.txt` 对账 | JSON 的 `metricId` 手抄错 / DB 快照漂移 / 一个指标两条 KPI 撞 id |
| 10 | §9.1 **表头 14 列列序** | 列序错 ⇒ 所有取值整体错位（逐格字形看着还是对的） |
| 11 | §9.1 **行序**与 JSON `kpis` 数组一致 | 行序漂移（内容自洽，读者按表顺序理解会错） |
| 12 | §9.1 图例的**字形→状态词映射对**完备且与 §2 一致 | 图例漏字形 / 映射写错；**只看「字形出现过」不够** —— 图例散文句里提到字形也算「有」（V4-5 变异实测：删掉真正的映射行仍然全绿 ⇒ 改为解析映射对） |
| 13 | 槽位取值落在 §2 **值域**内 + 业务不变式（RATIO 必有分子分母、MEASURE 分母必须为空、`confirmed` 必有值、`not_applicable` 必无值、`grain_keys` 恒非空） | 取值越域 / 类型与槽位不配套 / 状态与取值自相矛盾 |
| 14 | 槽位 `answers` 回指的题号 ⊆ 登记表题号 | 依据悬空（引用了不存在的题） |
| 15 | 处置 ↔ 落点交叉引用（`∅` 必须指向 G 系列；G 系列只属缺口类处置；引用编号必须存在） | 缺口登记指向不存在的 G / 处置与落点不相容 |
| 16 | README §2.2 逐 KPI 状态 ↔ JSON 三分桶；`meta.evidence` / `meta.slotModel` / README §1 清单的路径**真实存在** | 对外文档与 SSOT 不一致 / 引用了已删除的文件 |

### 4.2 反证脚本的三处升级

1. **变异集 18 → 36**：新增 17 条 v4 专项变异（每项断言至少一条）+ 1 条「DB 快照缺失」。
2. **必须命中预期关键词**：只判「非零退出」的话，一个变异被**别的**检查项顺手抓出来也算过
   ⇒ 每条变异都指名它期望被抓的那条信息（关键词只在**问题行**里找，不看检查项标题）。
3. **前检**：缺文件 / 坏 JSON 由入口的**产物前检**拦下并报成 exit 1（产物层问题），
   而不是让检查器抛 traceback 变成 exit 2（检查器自己坏了）⇒ 崩溃与产物缺陷不再混淆。

### 4.3 v4 顺手抓出并修掉的真实缺陷（不是评审臆想）

| # | 缺陷 | 处置 |
|---|---|---|
| D-1 | `KPI_SUPPLIER_MAINTENANCE_RATIO` 的 `time_grain` / `time_column` 标 `confirmed` 却**取值为空**，与 §4.10「当前时点快照 ⇒ 两者应保持为空」矛盾 | 改判 **`not_applicable`**（JSON + §9.1 两个 ⊘ + 统计行 n/a 17）；并立不变量「`confirmed` ⇒ 必有值；`not_applicable` ⇒ 必无值」 |
| D-2 | §9.1 表的**行序**与 JSON `kpis` 数组不一致（`INVOICE_MATCH_RATE` 与 `PAYMENT_ON_TIME` 互换） | 按 JSON 顺序对齐（JSON 是 SSOT），并立「行序」断言 |
| D-3 | §2 定义解析**未限定小节** ⇒ 模板里 §3/§4/§9 的含槽位名表格会**覆盖** §2 的定义 | 解析限定在 §2 小节（`section2()`） |
| D-4 | 图例**横跨两行**（第一行 `●`…`⊘`，第二行 `◐`/`○`=open）⇒ 只取第一行会漏掉 `open` 与两个 open 字形 | 图例合并为多行块（`parseSection91`） |
| D-5 | 落点引用的编号用 `\b` 判定，而 Python 的 `\w` **含中文** ⇒ 「见C-02」这种写法会被判成「无边界」而**漏掉引用** | 改用显式 ASCII 字符类看门（`(?<![A-Za-z0-9_-])`） |

⇒ v4 校验结果：**16/16 全绿**，**36/36 变异全部捕获**。


---

## 附五：检查器第四轮重写（v4 → v5）：退出码契约归因 + 三处「假绿」堵漏

v4 的检查器经 `code-reviewer` 评审出 **6 个 MEDIUM / 7 个 LOW**（0 CRITICAL / 0 HIGH）。
本轮处置 **3 个 MEDIUM**（都在「检查器自己的失败模式」上）+ 配套 LOW，
新增结构前检层 `_schema.py`，变异集 **36 → 43**。

### 5.1 MEDIUM-1：退出码契约**双向破裂**（产物缺陷被说成工具故障）

| 情形 | v4 | v5 | 后果 |
|---|---|---|---|
| 产物缺文件 / 坏 JSON | 1 ✅ | 1 ✅ | — |
| 产物**结构缺陷**（某条 KPI 少一个槽位键） | **2** ❌ | **1** ✅ | v4 把「产物坏了」说成「工具坏了」⇒ 反证脚本按「崩溃 ≠ 抓到缺陷」判罚 ⇒ **归因反了** |
| 前检自身抛异常 | **traceback + 1** ❌ | **2** ✅ | v4 把「工具坏了」说成「产物坏了」 |

**处置**：
1. **结构前检独立成层**（`check_procurement_answer_package_schema.py`）——
   把断言层依赖的结构契约显式化（顶层键 / `meta` 必需键 / `summary.slotCounts` 必需键 /
   每条 KPI 的必需键 / **13 个模型槽位齐全** / 每个槽位含 `status` / 候选文件 `bindings`+`counts`）。
   结构已坏 ⇒ 后续断言的结论只会是噪声 ⇒ 入口**不再执行**后续断言。
   **结构缺陷 = 产物层问题 ⇒ 退出码 1**。
2. 前检与结构前检**各自包 try/except** ⇒ 工具侧故障一律退出码 **2**。
3. **模块 docstring 的契约表同步改正**（v4 的 docstring 仍写着「缺文件 / 坏 JSON ⇒ 2」，
   与实际行为相反 ⇒ 契约写在文档上却双向破裂）。

### 5.2 MEDIUM-2：`meta.evidence` 缺失/为空 ⇒ **空转即通过**

v4：`references = list(model.meta.get("evidence", []))` ⇒ 清单为空 ⇒ 循环体一次都不执行
⇒ 「没有任何问题」被当成「检查通过」⇒ **假绿**。

**处置**：`meta.evidence` 必须**显式存在且非空**；`meta.slotModel` 必须存在；
每条引用都必须落在**前检清单**（`REQUIRED_ARTIFACTS`）里 —— 引用一个「没人校验的文件」等于没引用。

### 5.3 MEDIUM-3：`slotCounts.scope` 的口径数字**没人读**

v4：补充注记槽条目数只跟**脚本里写死的 `6`** 比 ⇒ 产物把 scope 改成「另有 99 项」时，
实际条目（仍是 6）跟写死的 6 相等 ⇒ **全绿**（假绿）。`scope` 里的
「13 个模型槽位 × 12 KPI」「合计槽位条目 162」**完全没人解析**。

**处置**：新增 `lib.parseScopeCounts()` 从产物解析 scope 的四个数字
（模型槽位数 / KPI 数 / 补充注记条目数 / 合计条目数）+ 点名的补充注记槽名，
再与**实际重算值**对账（`156 + 补充条目数 == 合计条目数`）；并断言
`meta.metricCount == 实际 KPI 数`、`summary.fullyConfirmed/conditional/blocked`
三桶的 KPI 码合法且**覆盖全部 12 条**。写死常量 `EXPECTED_SUPPLEMENTARY_ENTRIES` 已删除。

### 5.4 反证脚本升到**三态**（`caught` / `escaped` / `tool-error`）

v4 把「工具故障」与「检查器漏网」都记成「没抓到」⇒ 两者都只报退出码 1
⇒ 「检查器崩了」会被误读成「检查器有假绿」，**处置方向完全相反**。

| 状态 | 含义 | 退出码 |
|---|---|---|
| `caught` | 报出产物层问题且命中预期关键词 | 0 |
| `escaped` | 仍然全绿 / 报的不是这条 ⇒ **假绿** | 1 |
| `tool-error` | 变异没落地 / 检查器退出码 **≠ 1** / 检查器 traceback ⇒ 本轮结论不可用 | 2 |

⚠️ 关键点：**Python 未捕获异常本身就以退出码 1 结束** —— 与「产物层问题」**同码**！
⇒ 只看退出码会把「检查器的 traceback」误判成「抓到了缺陷」。
⇒ `_toolCrashed()` 必须额外识别 `Traceback (most recent call last)`。

### 5.5 新增 7 条变异（v5 专项）—— 修复效果**可被证明**

| 变异 | v4 表现 | v5 表现 |
|---|---|---|
| V5-1 少一个模型槽位键 | **exit 2**（归因反了） | exit 1「缺少模型槽位」 |
| V5-2 `meta.evidence` 清空 | **exit 0 全绿** | exit 1「meta.evidence 是空列表」 |
| V5-3 `meta.evidence` 悬空引用 | **exit 0 全绿** | exit 1「不在产物清单里」 |
| V5-4 `meta.slotModel` 被删 | **exit 0 全绿** | exit 1「缺少键：slotModel」 |
| V5-5 `meta.metricCount` 不符 | **exit 0 全绿** | exit 1「meta.metricCount=99 ≠ 12」 |
| V5-6 scope「另有 6 项」被改 | **exit 0 全绿** | exit 1「补充注记槽条目 6 ≠ scope 声明的 99」 |
| V5-7 scope「合计槽位条目」被改 | **exit 0 全绿** | exit 1「合计槽位条目 999 ≠ 156 + 6 = 162」 |

⇒ v5 校验结果：**16/16 全绿**（exit 0），**43/43 变异全部捕获**（exit 0）。

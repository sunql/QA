# KPI 口径消费 P0 止血（规范化 + 诊断接口）

> 分支：`feat/kpi-definition-consumption`（base = `origin/main` `4e535289`）
> 设计稿：`docs/superpowers/specs/2026-11-11-kpi-definition-consumption-design.md`
> 计划：本目录 `plan.md`
> 状态：**已完成，未部署**。生产仍为 alembic `0140`、运行旧代码；本批**零 DDL**。

## 1. 需求

`kpi_catalog` 的治理结构化列（`numerator_columns` / `denominator_columns` /
`grain_keys`）存的是**业务中文名**（「供应商」「月」），与 `ontology_metric`
的技术形态（物理列名）永远对不上 ⇒ 对账恒 `mismatch` ⇒ 契约块恒空 ⇒
KPI 口径对 NL2SQL 完全不可用。

本批做三件事（P0 止血，不做 P1–P3）：

1. **规范化**：把治理结构化列改写为物理列名（由 measure / formula 确定性推导）；
2. **诊断接口**：`GET /api/v1/admin/kpi/diagnose?question=...` 让人能看见「为什么没命中 / 为什么没注入」；
3. **落库一次并复验**：在 dev 库 `qa_metadata`（5433）执行一次 `--apply`，留 before/after 取证。

## 2. 根因（已实证）

| # | 根因 | 证据 |
|---|---|---|
| 1 | 治理列写的是业务中文名，物理列判不出来 | 12/12 KPI 的 `grain_keys` 含「供应商/工厂/月」 |
| 2 | 层 2 对账（治理 ⇄ 技术）按字符串比对 ⇒ 中文 vs 列名恒不等 | `reconcileMetric` → `grain_mismatch` |
| 3 | 对账 mismatch ⇒ 契约注入被拦截 ⇒ 块为空 | `kpi_contract_builder` 空块 |
| 4 | 没有任何入口能回答「为什么这个问句没命中 KPI」 | 无诊断接口 |

## 3. 变更清单

| commit | 内容 |
|---|---|
| `809b8714` | 设计稿定稿（P0 先行边界） |
| `1297b09d` | 实施计划（D1–D3，按天拆分） |
| `4e9926af` | **D1**：`kpi_governance_normalizer` 纯函数（派生规则 / 三层对账 / Tier） + CLI dry-run 报告 |
| `334cad4a` | D1 复审修复：SQL 解析认识字面量 / 包装函数；Tier B 与幂等语义 |
| `49e95536` | **D2**：`GET /api/v1/admin/kpi/diagnose`（admin）+ 真实 PG HTTP 集成测试 |
| `4df846b0` | D2 复审修复：NONE 源 Tier C、`injected` 按块成员关系、匹配故障不吞 |
| `cf8aef45` | **D3**：`applyPlans` 落库 + 读回复核 + 幂等 + 单事务回滚 |
| `a75d20f0` | D3 复审修复：复核按**无序**比列、校验 metric 挂靠、拒绝 verdict 降级 |
| `1e04248d` | realdata S3：PLAN 偏差只在治理列未规范化时生效（`planDeviationActive`） |
| `0e1923da` | 规范化前快照（before.json + dry-run 报告） |
| `6c3fafb7` | realdata S3：期望改为按 **execution_source** 判定（tier B 规范化后为 pass） |
| `05a7f442` | 安全复审修复：`_assertOwnership` 校验治理列/度量快照未过期；更正「生产永远拒绝」失实说明 |
| `86d0e12f` | 安全复审修复：CLI 帮助文本/计划文档如实说明「守卫只看库名」 |
| `e3aa96ca` | **度量表达式保真**：派生 measure 保留别名限定与 `TRUNC`；列集合提取改由 `measureColumnIdentifiers` 负责；未挂靠 metric 的草稿 KPI 跳过而非中断整批 |
| `0c0b451d` | 回填脚本警示注释 + wiki §16 + 本变更记录 |
| 本文件所在 commit | 合并闸门净新增修复：两处 `_matchKpiCodes` 替身补 `strict` 形参；seed 用例显式 eager-load（§7 踩坑 8） |

**新增文件**

- `backend/app/services/kpi_governance_normalizer.py`（派生规则纯函数，581 行）
- `backend/app/api/v1/kpi_diagnose.py`（admin 诊断接口）
- `backend/scripts/kpi_normalize_governance.py`（CLI，默认 dry-run）
- 测试：`test_kpi_normalize_governance.py`（单元 447 行）+ `test_kpi_normalize_governance_apply.py`（集成，真 PG）+ `test_kpi_diagnose_api.py`（真实 PG + HTTP）

## 4. before / after（dev 库 `qa_metadata` @ 5433，2026-11-11）

完整报告见同目录 `dry-run-before.txt` / `dry-run-after.txt`；机器可读快照见
`before.json` / `after.json`。

| KPI | 分子列 | 分母列 | 粒度键 |
|---|---|---|---|
| KPI_COST_SAVING | `STANDARD_PRICE,ACTUAL_PRICE` → `ORDER_QTY,UNIT_PRICE` | — | `供应商,物料,月` → `SUPPLIER_CODE,MATERIAL_CODE` |
| KPI_INVOICE_MATCH_RATE | `PO,QTY,PRICE` → `LINE_AMOUNT_EXCL_TAX` | `[]` → `AMOUNT_EXCL_TAX` | `供应商,月` → `SUPPLIER_CODE,INVOICE_DATE` |
| KPI_PAYMENT_ON_TIME | `PAYMENT_DATE` → `VALUE_DATE` | `DUE_DATE` | `供应商,月` → `SUPPLIER_CODE` |
| KPI_PURCHASE_CYCLE_TIME | `GOODS_RECEIPT_DATE,PR_CREATED_DATE` → `CREATION_DATE,RECEIPT_DATE` | — | `供应商,工厂,月` → `SUPPLIER_CODE,FACILITY_CODE` |
| KPI_PURCHASE_PRICE_VARIANCE | `ACTUAL_PRICE,STANDARD_PRICE` → `UNIT_PRICE` | `STANDARD_PRICE` → `UNIT_PRICE` | `物料,工厂,月` → `MATERIAL_CODE,FACILITY_CODE` |
| KPI_SUPPLIER_DEFECT_RATE | `IQCRESULT` → `[]`（NONE 源清空） | — | `供应商,物料,工厂,月` → `[]` |
| KPI_SUPPLIER_DELAY_DAYS | `ACTUAL_RECEIPT_DATE,PROMISED_DATE` → `PROMISED_RECEIPT_DATE,RECEIPT_DATE` | — | `供应商,工厂,月` → `SUPPLIER_CODE,FACILITY_CODE` |
| KPI_SUPPLIER_FPY | `INSPECTION_RETRY_COUNT,IQCRESULT` → `[]`（NONE 源清空） | — | `供应商,物料,工厂,月` → `[]` |
| KPI_SUPPLIER_MAINTENANCE_RATIO | `[]`（NONE 源） | — | `供应商（快照）` → `[]` |
| KPI_SUPPLIER_NCR_RATE | `[]`（NONE 源） | — | `供应商,物料,工厂,月` → `[]` |
| KPI_SUPPLIER_OTD | `ACTUAL_RECEIPT_DATE` → `RECEIPT_DATE` | `PROMISED_DATE` → `PROMISED_RECEIPT_DATE` | `供应商,工厂,月` → `SUPPLIER_CODE,FACILITY_CODE` |
| KPI_SUPPLIER_OVERDUE_RATIO | `ACTUAL_RECEIPT_DATE` → `RECEIPT_DATE` | `PROMISED_DATE` → `PROMISED_RECEIPT_DATE` | `供应商,工厂,月` → `SUPPLIER_CODE,FACILITY_CODE` |

4 条 `NONE` 源（DEFECT_RATE / FPY / MAINTENANCE_RATIO / NCR_RATE）没有可对账的技术形态
⇒ 清空结构化字段、verdict 记 `unknown`，业务待补事项写入 CLI 报告的 `business_todo`。

**派生度量的落库值（复审后修正）**：首轮 `--apply` 写入的派生 measure 被去掉了表别名与
`TRUNC`，退化成不可执行/恒零的表达式（如 `(UNIT_PRICE - UNIT_PRICE) * ORDER_QTY`）。
代码复审发现后改为**表达式原样保留限定名与函数**，列集合提取下沉到
`measureColumnIdentifiers`（只在比较列集合时剥别名/函数名）。dev 库上受影响的 4 条
（metric 6/7/8/9）先置空 `*_measure` 再重跑 `--apply` 修复，现库内值为：

| metric | numerator_measure | denominator_measure |
|---|---|---|
| 6 `KPI_PURCHASE_CYCLE_TIME` | `AVG TRUNC(R.RECEIPT_DATE) - TRUNC(L.CREATION_DATE)` | — |
| 7 `KPI_SUPPLIER_DELAY_DAYS` | `AVG TRUNC(R.RECEIPT_DATE) - TRUNC(O.PROMISED_RECEIPT_DATE)` | — |
| 8 `KPI_COST_SAVING` | `SUM (P.UNIT_PRICE - O.UNIT_PRICE) * O.ORDER_QTY` | — |
| 9 `KPI_PURCHASE_PRICE_VARIANCE` | `AVG (O.UNIT_PRICE - P.UNIT_PRICE)` | `AVG NULLIF(P.UNIT_PRICE, 0)` |

修复后重跑 dry-run：**12/12 noop**、`summary: pass=8 unknown=4 mismatch=0`。

## 5. 验收证据

| # | 验收项 | 结果 |
|---|---|---|
| 1 | dry-run 打印 12 条 before/after | ✅ `dry-run-before.txt` / `dry-run-after.txt` |
| 2 | `--apply` 后目标状态 | ✅ `applied：12 条变更`；`summary: pass=8 unknown=4 mismatch=0`；8 条 Tier A |
| 3 | 幂等 | ✅ 重跑 dry-run **12/12 noop**；第二次 `--apply` **0 条**（`second-apply-noop.txt`） |
| 4 | 诊断接口 | ✅ admin 200 / 非 admin 403；返回命中方式、verdict/reasons、注入块是否为空、Tier A 可执行性（`test_kpi_diagnose_api.py`） |
| 5 | 对账复验（realdata S3） | ✅ 测度修复**后**重跑 S3：**9 步全 PASS**（`RESULT: PASS steps=9 passed=9 failed=0 skipped=0`），`S3.3 mismatch 集合 = ∅`，`S3.4 列级 finding = ∅`，分布 `{'pass': 8, 'unknown': 4}`。⚠️ 直接跑 `scripts/kpi_metric_realdata.py` 会 `FAIL S1.1` 并跳过 S2–S8 —— 该脚本把 alembic 版本**精确钉在 `0137`**，dev 库已到 `0142`；本轮用只跑 `step3_reconcile` 的 runner 取证（S3 的判据不依赖版本闸门） |
| 6 | 定向回归 | ✅ 主 7 套件 **118 passed**；另 4 套件 75 passed / 1 failed，失败项 `test_kpi_catalog_search_api.py::test_search_requires_auth` 在 `origin/main` 基线**同样失败**（`getSettings` 的 `lru_cache` 让测试内 `monkeypatch.setenv` 不生效）⇒ **净新增 0** |
| 7 | 生产 | ✅ **未写、未部署**；生产仍 `0140` |

## 6. 关键实现约束（复审后定稿）

- **单事务**：任一条失败（更新行数 ≠ 1 / 复核不一致 / 仍 mismatch / DB 异常）⇒ 全量回滚。
- **写后读回复核**（`_verifyApplied`）：`populate_existing=True` 重新读库，三组列按**无序**比较
  （plan 的 `grain_keys` 保持作者原序），`metric_id` 必须未变，reconcile verdict 必须**等于**计划的
  `verdict_after`（pass 回落 unknown 同样算失败）。
- **写前守卫**：`_assertUniqueTargets`（重复 kpi_code / 共享 metric_id 拒绝）、
  `_assertOwnership`（重读 KPI + metric：`metric_id`、三组治理列、两侧度量任一与计划快照
  不一致 ⇒ 拒绝；防止覆盖并发编辑或在过期派生值上写库）。
- **库守卫**：`assertSafeTarget`；`qa_metadata`（dev/prod 同名）必须显式 `--allow-prod-write`；
  建连后再用 `current_database()` 复核「真的连到了以为的那个库」。
  ⚠️ 守卫**只看库名、不校验 host/port**：dev 与 prod 都叫 `qa_metadata`，`--allow-prod-write`
  对生产 DSN 同样放行。环境区分靠操作者核对脚本打印的 `host:port`（`maskDsn` 不带口令）。
- **退出码**：0 正常；1 目标仍有 mismatch / 连错库；2 派生失败或落库复核失败（已回滚）。

## 7. 踩坑与更正（后续勿重犯）

1. **realdata S3 的期望不能按 PLAN tier 判**。PLAN tier B 覆盖「一侧度量 + 算术式 /
   物理源部分缺失」，其中 `KPI_SUPPLIER_MAINTENANCE_RATIO` 的 **PLAN tier = B 但 DB
   `execution_source = NONE`**。规范化后的正确期望是 **`NONE` ⇒ `unknown`，其余 ⇒ `pass`**
   （实测 8 pass + 4 unknown）。按 tier 判会把 4 条正确结果判成失败（本轮实跑发现并修正）。
2. **`_verifyApplied` 必须无序比列**：`plan.grain_keys` 保持作者原序（如
   `SUPPLIER_CODE,PROVINCE`），拿 `sorted()` 去比未排序目标会误报回滚。
3. **回填脚本 `kpi_metric_backfill_apply._governanceValues` 写的是业务名**：在已规范化的库上
   重跑会覆盖回业务名、重新制造 mismatch ⇒ 只当历史初始化工具；已在函数 docstring 注明。
4. **`session.rollback()` 会 expire 主键属性**：测试里要在调用 `applyPlans` 前先取出 id。
5. **失效对象必须 `populate_existing=True`**：`expire_all()` 后直接访问属性会触发
   `MissingGreenlet`。
6. **派生 measure 必须保真**：落库的 `*_measure.expr` 要**原样保留**表别名限定（`O.UNIT_PRICE`）
   与函数包裹（`TRUNC(...)`）。把 `O.UNIT_PRICE - P.UNIT_PRICE` 压成
   `UNIT_PRICE - UNIT_PRICE` 会得到恒 0 的表达式，`TRUNC` 去掉则改变取整语义；
   而**列集合比较**（层 2 治理 ⇄ 技术）才需要剥别名与函数名。两条职责现分别落在
   `kpi_governance_normalizer._normalizeExpr`（只 trim）与
   `nl2sql_semantic_guard.measureColumnIdentifiers`（剥别名/函数名）。首轮实现把两者合并，
   导致 4 条度量化简后落库 —— 复审已修，dev 库已重写。
7. **测试用例别依赖弱引用 identity map 里的对象存活**：`test_seed_feature_rules.py::
   test_seed_risk_score_has_3_tiers` 原先假设「同一 session 里刚 seed 过的 FeatureRule
   实例还在 identity map 里、`thresholds` 已加载」。SQLAlchemy `Session` 的 identity map
   是**弱引用**，clean 实例随时可能被 GC；一旦被回收，再次 SELECT 得到的是
   `thresholds` 未加载的新实例，async 下的同步懒加载就抛 `MissingGreenlet`。
   本分支只是新增了一个路由导入（`kpi_diagnose`）改变了分配/回收时机，就把这条用例
   从「碰巧绿」翻成「稳定红」——**不是产品回归**。修法是用例显式
   `selectinload(FeatureRule.thresholds)`（生产路径 `getRule`/`listRules` 本来就这么加载）。
8. **`kpi_metric_realdata.py` 的 S1.1 是精确版本闸门**（`version == "0137"`）：库一升级到
   `0142`，S1.1 直接 FAIL 且 S2–S8 全部 SKIP（「全跳」退出码 2，不会被读成通过）。取 S3 证据时
   要么跑库内 `step3_reconcile`（判据与版本无关），要么先放宽该闸门（本批未改，留给脚本归属方）。

## 8. 未做的事（明确边界）

- ❌ P1 语义匹配升级、P2 Tier A 执行、P3 结构化 → SQL 编译器与 `time_grain` 解析；
- ❌ 4 条 `NONE` 源 KPI 的技术形态（业务方补齐前只能 `unknown`）；
- ❌ 生产部署（本批不部署；生产仍在 `0140`）；
- ❌ 生产库写入（只写了 dev `qa_metadata` @ 5433，且经用户事先批准）。

## 9. 回滚

- 代码：`git revert` 本分支提交；无 DDL、无迁移，回滚不涉及数据结构。
- dev 库数据：`before.json` 保存了 12 条 KPI 治理字段与 12 条 metric 度量的**完整原值**，
  可用同一批 SQL 参数化回写（仅限 dev）。

# 变更：语句形态 formula 的校验口径（不得逐 token 当属性）

- **日期**：2026-10-01
- **作者**：Claude / 启琳
- **Phase**：NL2SQL 计划校验（`validatePlan` 公式分支三路口径 + CTE 判定 SSOT 化 + 失败留痕日志）
- **状态**：**实现完成、测试绿、待部署**。生产 `alembic` 无变更（**本次无迁移**）。
- **关联变更**：[fix-nl2sql-derived-metric-formula-required](../fix-nl2sql-derived-metric-formula-required/summary.md)（占比类必须带 formula 的硬约束，正是它把本问题逼出来的前置）
- **迁移版本**：**无**
- **SSOT 出处**：`Harness/wiki/nl2sql-engine.md`（本变更新增 §语句形态 formula 的校验口径，并顺手修正该文件里 `validatePlan` 的过期路径与一处不实描述）
- **commit**：见本目录提交

---

## 1. 问题

用户 2026-10-01 生产报障。问：

> 5月份供货量最多的三家供应商所供货物总量占5月份总供货量的比例是多少

连续三次得到同一个错误：

```
无法生成通过校验的查询计划，请换一种问法或补充本体元数据
公式中的属性 ONLY 不属于选定的任何类；公式中的属性 m2 不属于选定的任何类；
公式中的属性 DWD_GOODS_RECEIPT_DTL 不属于选定的任何类；
公式中的属性 DIM_IMATERIAL 不属于选定的任何类；
公式中的属性 d2 不属于选定的任何类；公式中的属性 THBI 不属于选定的任何类
```

六个 token **全是 SQL 结构，没有一个是本体属性**：`THBI` 是 schema 名、`DWD_GOODS_RECEIPT_DTL` / `DIM_IMATERIAL` 是表名、`d2` / `m2` 是表别名、`ONLY` 来自 `FETCH FIRST n ROWS ONLY`。

## 2. 根因

该问题要「先取前三家、再算占比」，单条窗口函数表达不了，所以 LLM 把**一整条 SELECT** 放进了 `Aggregation.formula`（prompt 规则 4 本来就允许 `WITH ... SELECT ...` 这类语句形态，模型只是没写成 CTE）。

`validatePlan` 对带 formula 的聚合只有两路：CTE 形态豁免属性校验，其余一律逐 token 校验。于是语句形态落进第二路，`_extractFormulaProperties`（`app/services/nl2sql_refs.py`，只做「剥字符串字面量 → 剥函数调用名 → 取标识符 → 过滤关键字」**四步，完全没有上下文感知**）把 schema 名/表名/表别名/`ONLY` 全部当成属性幻觉上报。

**本地复现**（同一夹具，非推断）：

| 项 | 值 |
|---|---|
| 误报条数 | **9**（用户侧 6 —— 差的 3 个是 `RCV_QTY`/`ITEM_CODE`/`ITMREF_0`，真实列名，在 `owned` 里所以未被上报） |
| 用户报的 6 个 token | 恰好是这 9 条去掉那 3 个真实列名后的**全集** |
| `parseFormula(...).is_cte` | `False` |

三个叠加缺陷：

1. **语句形态没有专门分支** —— 且 CTE 逃生门本身是脆的：`formula.strip().upper().startswith("WITH ")` 要求 `WITH` 后**紧跟一个空格**，`WITH\n` / `WITH\t` 直接漏判（已实测 `is_cte=False`）。
2. **`ONLY` 不在关键字集** —— `_FORMULA_SQL_KEYWORDS` 有 `FETCH`/`ROWS`/`FIRST` 却没有 `ONLY`，与 2026-09-28 修过的 `ASC/DESC` 同类漏项。
3. **该支报错没有可操作提示** —— `公式中的属性 X 不属于选定的任何类` 是唯一**不拼任何 hint** 的分支（其余分支都拼 `_propertyOwnerHint` / `_timeBucketGroupHint`）。重试反馈无指向 → 模型原样重犯 → `maxPlanAttempts=2` 耗尽 → 整轮失败。这解释了「连点三次都一样」。

**取证缺口**：计划校验失败当**零日志**，`session_message` 也无 detail 列、无 attempt 表 ⇒ 真实 formula 原文事后不可回看，只能靠现象反推（本次即如此）。

## 3. 判据与设计

### 判据：`SELECT` 与 `FROM`/`JOIN` **同时**出现

刻意**不看首词**。错误文本只列出被误报的 token，无法区分「整条 SELECT」与「表达式里嵌子查询」（`SUM(a)/(SELECT SUM(b) FROM t)`）——两者首词不同（`SELECT` vs `SUM`）但**都含 `SELECT` + `FROM`**。一个判据同时覆盖两种形态，不必猜首词。

**必须两个条件同时满足**（首版只看 `FROM`/`JOIN`，被 code review 判 HIGH，已复现并修正）：`EXTRACT(MONTH FROM 到货日期)` / `TRIM(BOTH ' ' FROM X)` 里的 `FROM` 是**函数实参分隔符**，不是语句子句。`EXTRACT` 那条提取出的 token 全是真实属性（只剩 `QTY` / `到货日期`）⇒ **加谓词之前它是能通过校验的**；只看 `FROM` 会把它从「能过」变成「被拒」，且提示语内容不实（说它是整条 SQL 语句），反而把模型带偏。真语句必然同时含 `SELECT`。

实现：`_stripFormulaLiterals` 剥掉字符串字面量后，`\bSELECT\b` 与 `\b(?:FROM|JOIN)\b` 都必须命中 ⇒ `formulaHasSqlStructure`。

### 三路口径（`validatePlan`）

| 形态 | 判定 | 处理 |
|---|---|---|
| CTE | `isCteFormula`（`^\s*WITH\b`，容许前导空白） | **不做**属性存在性校验（现状不变） |
| 语句结构 | `formulaHasSqlStructure` | 报**一条**可操作引导 `_STRUCTURAL_FORMULA_HINT`，不逐 token 报属性 |
| 纯聚合表达式 | 以上皆否 | 逐 token 做属性存在性校验（原逻辑，如期拦真幻觉） |

### 为什么是「拒绝 + 引导」而不是「豁免」

`Aggregation.formula` **没有确定性渲染器** —— `planToText`/`_aggText`（`app/domain/query_plan.py`）只把它拼成 `"{formula} AS {alias}"` 塞进 SQL 生成 prompt。豁免语句形态 = 让 SQL 阶段收到 `SELECT ... FETCH FIRST 3 ROWS ONLY AS 占比` 这种畸形「聚合」行，把**早期响亮的失败换成晚期安静的失败**。CTE 形态之所以被豁免，是因为它至少是**有结构的草稿**。

### 误伤边界（首版论证被证伪，已修正）

初版论证是：「`owned` 只含属性名/别名，一条含 `FROM` 的公式要通过校验，必须其 schema 名、表名、表别名**全部恰好等于某个属性名** —— 近乎不可能。故新分支**只改变「今天已经在失败」的公式的报错内容**。」

**这条论证是错的。** `EXTRACT(MONTH FROM 到货日期)` 就是反例：它的 `FROM` 不是语句子句，提取出的 token 全是真实属性，公式改前**能通过**。code review 抓到后已复现（`formulaHasSqlStructure` 返回 `True`、`validatePlan` 由 `[]` 变成 1 条不实提示）并修正为「`SELECT` 与 `FROM`/`JOIN` 同时出现」。

修正后的边界：**含 `SELECT` 的**公式，其表名/schema 名/表别名要全部恰好等于属性名仍近乎不可能（真语句场景成立）；**不含 `SELECT` 的** `EXTRACT`/`TRIM` 类公式落回逐 token 校验，行为与改动前**完全一致**。

实施前已核实：`app/tests/` 与 `tests/` 中**没有任何**公式字面量含 `FROM`/`JOIN`（含两处多行公式），故不存在「既有用例期望 `公式中的属性` 却含 `FROM`」的计划-测试冲突。

## 4. 落地内容

| 文件 | 改动 |
|---|---|
| `backend/app/services/formula_parser.py` | 新增 `_CTE_LEADING_RE` + 公开谓词 `isCteFormula`；`parseFormula` 路由改用它（顺带修掉 `WITH\n` 漏判）；`_SQL_KEYWORDS` 补 `ONLY` |
| `backend/app/services/nl2sql_refs.py` | 抽出 `_stripFormulaLiterals`（两个消费者）；新增 `_SELECT_KEYWORD_RE` / `_STRUCTURAL_SQL_RE` + 公开谓词 `formulaHasSqlStructure`（要求 **SELECT 与 FROM/JOIN 同时命中**）；`_FORMULA_SQL_KEYWORDS` 补 `ONLY` |
| `backend/app/services/nl2sql_plan.py` | `validatePlan` 公式分支改三路；新增 `_STRUCTURAL_FORMULA_HINT` 与 `formatPlanFormulas`；提示语 **insert 到 issues 首位并去重**（截断从尾部砍 —— 追加会被前置换的 issue 挤出预算，N 条语句公式还会各占 150 字符）；删掉「SQL Guard 已校验 CTE 语法」这句**不实注释**（`_assert_read_only` 从不检查 formula，只在生成的 SQL 上跑） |
| `backend/app/services/nl2sql_service.py` | `generateValidatedPlan` 校验失败时记 `logger.warning`（attempt + formula 原文 + issues），最终失败再记一条 |
| `backend/app/tests/unit/test_formula_parser.py` | 新增 `TestIsCteFormula`（9 例）+ `TestParseFormulaCteRouting`（3 例） |
| `backend/app/tests/unit/test_query_plan_validation.py` | 新增 `TestFormulaStructurePredicate`（5 例）+ `TestValidatePlanFormulaShape`（5 例）；`TestSqlKeywordSetsStayInSync` 加 `test_only_is_covered` |
| `backend/app/tests/integration/test_nl2sql_structural_formula_retry.py` | 新增端到端重试测试（假 LLM 第 1 次回语句形态计划、第 2 次回 CTE 计划） |

**`isCteFormula` 的第二个消费者**：`validatePlan` 用它判 CTE 豁免（原先用 `parseFormula(...).is_cte`）。SSOT 化后 `parseFormula` 的路由与豁免判定不可能再漂移。

## 5. 验证

### 判别器（修前红 → 修后绿）

同一夹具公式 `SELECT SUM(d2.RCV_QTY) FROM THBI.DWD_GOODS_RECEIPT_DTL d2 JOIN THBI.DIM_IMATERIAL m2 ON ... FETCH FIRST 3 ROWS ONLY`：

| | issues |
|---|---|
| 修前（实测） | **9 条** `公式中的属性 … 不属于选定的任何类` |
| 修后（实测） | **1 条**：`formula 不能是整条 SQL 语句（含 FROM/JOIN）；占比/比率请改用 ① 单层窗口函数 … 或 ② CTE 形式 WITH a AS (SELECT ...) SELECT ... FROM a（需先取 Top-N 再算占比时用 ②）` |

子查询形态 `SUM(QTY) / (SELECT SUM(QTY) FROM THBI.DWD_GOODS_RECEIPT_DTL)` 同样 → 1 条引导（证明判据不依赖首词）。

### 防过度修复（反向守卫）

- `SUM(NONEXISTENT) / SUM(SUM(NONEXISTENT)) OVER ()` → **仍报** `公式中的属性 NONEXISTENT`（纯表达式的真幻觉不得被放过）
- `SUM(CASE WHEN EXTRACT(MONTH FROM 到货日期) = 5 THEN QTY ELSE 0 END) / SUM(SUM(QTY)) OVER ()` → **通过**（`FROM` 是函数实参分隔符。这是 code review HIGH 的回归守卫：该公式改前能过，只看 `FROM` 的版本会误拒并给出不实提示）
- `SUM(TRIM(BOTH ' ' FROM X))` 类 → 不进语句结构分支，落回逐 token 校验（与改前一致）
- `CASE WHEN BPSNUM = 'FROM' THEN QTY ELSE 0 END` 类公式 → 字面量里的 `FROM` 不算语句结构，公式照常按属性校验且通过
- `SUM(FROMX)` → 词边界，`FROMX` 不触发语句结构判定；`(SELECT SUM(x) FROM T)` → 仍判 True（双向）
- CTE 形态（含 `WITH\n`）→ 仍豁免

### 提示语真能到达模型（code review MEDIUM 的判别器）

`test_hint_survives_snippet_truncation_alongside_other_issues` 用一个**同时**含幻觉属性与语句公式的计划，断言生产构造器 `_buildPlanUserPrompt` 的输出里含完整提示语（不重写拼接逻辑，避免与实现漂移）。

实测判别力：把提示语从「置首」改回「追加」，`_ERROR_SNIPPET_LIMIT=200` 会把它砍成 `… 或 ② CTE 形式 WITH a AS (SELECT ...) SELEC`，断言即失败。故该测试确实在钉「引导没被截断」。

### 端到端（证明反馈真的进入了下一次尝试）

`app/tests/integration/test_nl2sql_structural_formula_retry.py`（真实 PostgreSQL）：假 LLM 第 1 次回语句形态计划、第 2 次回 CTE 计划。断言：恰好重试 1 次；**第 2 次调用的 prompt 含 `formula 不能是整条 SQL 语句` / `窗口函数` / `CTE 形式` 三段**（第三段在提示语末尾，同时证明 200 字符截断没有砍掉引导）；最终拿到的是 CTE 计划而非抛 `Nl2SqlError`。

该断言是**真判别器**：`formula 不能是整条 SQL 语句` 这个串由本次改动引入（`git show HEAD:backend/app/services/nl2sql_plan.py` 命中 0，工作区命中 1），修前不可能通过。

### 回归

- 受影响面（含 `nl2sql_plan` / `nl2sql_refs` / `formula_parser` / `nl2sql_service` 的 12 个 unit 文件）：**467 passed, 1 failed**。
- 那 1 个失败（`test_query_plan_generation.py::TestGenerateQueryPlan::test_missing_fields_use_defaults`）经 **HEAD 基线 worktree 对照复现为既有红**（同样的 `PLAN_EMPTY` → `IndexError: pop from empty list`），**非本次引入**。
- 按路径显式跑（不在 `testpaths` 里，会静默漏跑）：`tests/unit/test_nl2sql_validate_plan_cte.py` + `tests/unit/test_formula_parser_cte.py` → **29 passed**。

## 6. 已知限制与非目标

- **提示语的截断预算**（code review MEDIUM，**已修**）：`_STRUCTURAL_FORMULA_HINT` 占 150 字符，`_ERROR_SNIPPET_LIMIT=200` 从**尾部**截断。实测按「追加」顺序时，前面挂一条 `选中的属性 …` 就会把引导砍成 `… 或 ② CTE 形式 WITH a AS (SELECT ...) SELEC`。故改为 **insert 到 issues 首位 + 去重** —— 提示语单独 150 < 200 必然存活，且 N 条语句公式只占一份预算。**未**提高 snippet 上限（nl2sql 占约 94.9% token 成本，不为罕见组合加预算）。
- **不做**：表达式里的限定别名（`SUM(t.QTY)`）与纯表达式内直接出现的表名仍会被报 —— `_extractFormulaProperties` 无上下文感知。这是推测场景、不是本次报障场景，不预先设计。
- **不改** `maxPlanAttempts`（维持 2）。一条带明确引导的反馈已提高第二次成功率；若部署后仍不达标，再议 2→3。

## 7. 残余风险

固定测试只确定性地证明「**反馈现在是对的**」；**LLM 会不会照着写，只有真机跑才知道**。这是本次修复唯一的残余风险，且已被第 4 节的失败日志变成可观测：

部署后重问原问题；若仍失败，`docker logs qa-backend` 现在能看到 attempt + **formula 原文** + issues，据此判断是引导不够还是模型能力问题，再决定是否上调 `maxPlanAttempts`。

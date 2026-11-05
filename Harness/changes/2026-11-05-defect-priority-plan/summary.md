# 变更：2026-11-05-defect-priority-plan（缺陷优先级处置 · 第一批）

- **日期**：2026-11-05
- **作者**：Claude
- **Phase**：Phase 3 NL2SQL / 前端本体治理
- **状态**：done（本批只做 ①②；③ 已由 PR #78 修复；④–⑦ 与第二梯队见 `plan.md`）
- **关联变更**：[2026-10-31-term-drift-date-template](../2026-10-31-term-drift-date-template/summary.md)（自然月窗口修复，本次补齐天级同族）、[2026-10-28-kpi-metric-technical-form](../2026-10-28-kpi-metric-technical-form/summary.md)（measure 结构化）
- **迁移版本**：无（本批零 DDL）
- **计划**：[plan.md](./plan.md)（今日待办计划）
- **MEMORY**：[qa-system-natural-month-window](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-natural-month-window.md)、[qa-system-measure-edit-clears-denominator](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-measure-edit-clears-denominator.md)、[qa-system-merge-gate-rerun-procedure](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-merge-gate-rerun-procedure.md)

---

## 1. 需求

用户 2026-11-05 指示：「统一给一个建议吧，优先解决问题，其次解决工程保障」→「把这个建议写到 Harness 里面当成今天开始代办的一个计划，然后根据建议现在就起 ①②③ 的 TDD 实施」。

本批是那批计划里的**第一梯队真缺陷**：

- **①「最近 N 天」时间窗口多算一天** —— NL2SQL 生成的 SQL 口径错，所有「近 N 天」类问句结果偏大。
- **②MEASURE 指标编辑保存清空历史分母** —— 用户只改了个名字，库里已录入的分母定义连同
  `class_id` / `class_alias` 被静默抹掉。
- **③L4 历史回放不还原 steps** —— 核对发现**已由 PR #78 修复并部署**（`session_message.query_plan` /
  `steps_snapshot` JSONB 快照，迁移 0138），本批不做；MEMORY 索引行过期，已更正。

验收（用户视角）：改完这两个缺陷，NL2SQL 的「近 N 天」结果不多算一天；
在本体里编辑一个度量型指标并保存，库里原有的分母度量**一字不变**。

## 2. 设计评审

### ① 相对时间窗口（近 N 天）

| 候选方案 | 取舍 | 结论 |
|---|---|---|
| A. 只改 prompt 模板（Oracle） | 改动最小，与 PR #89 自然月修复同构；但 PG / MySQL 同族问题留着 | ❌ 不彻底 |
| **B. prompt 模板 + Oracle/PG/MySQL 三方言同族一起修** | 三个方言都是「窗口口径没写清 ⇒ LLM 照 `日期列 - N` 写 ⇒ 多算一天」；一次修完、一次讲清 | ✅ **采纳** |
| C. 在 SQL Guard / `checkSqlDialect` 里加守卫拦 `CURRENT_DATE - N` | 确定性拒绝更硬；但 `CURRENT_DATE - 30` 在 SELECT 里是**合法的「30 天前参考点」**⇒ 误伤 | ❌ 否决 |
| D. 在 `time_scope_resolver.py` 解析「最近 N 天」并注入事实 | 服务端确定性最高；但该模块**刻意不解析滚动窗口**（滚动 vs 自然边界有业务歧义）⇒ 属于改产品语义，不是修缺陷 | ❌ 否决 |

**最终决定**：采纳 B；C / D 写进 `plan.md` 与 wiki 的「边界（勿越界）」，防止后人误加。

### ② MEASURE 编辑清空历史分母

| 候选方案 | 取舍 | 结论 |
|---|---|---|
| **A. 表单没暴露分母行 ⇒ 原样回传 `existing.denominator`** | 不破坏；语义之争（改型是否该清分母）留给业务口径明确时再定 | ✅ **采纳**（后按审查结论细化，见下） |
| B. 禁止 MEASURE 携带分母（后端加约束 + 前端拒绝） | 更彻底，但会拒掉合法历史数据、要求数据迁移 ⇒ 超出本批范围 | ❌ 否决 |
| C. 前端在 MEASURE 下也渲染分母行 | 暴露用户不关心的字段，且改动 UI | ❌ 否决 |

**最终决定**：采纳 A，但**必须区分两种「表单没暴露分母行」**（审查后细化）：

| 情形 | 判据 | 取值 | 理由 |
|---|---|---|---|
| 表单暴露了分母行 | 当前类型 ∈ {RATIO, DIFFERENCE} | 表单值说了算（显式清空 ⇒ `null`） | 用户看得见、碰得着 ⇒ 意图明确 |
| 原来是比率、这次改成别的类型 | 原类型 ∈ {RATIO, DIFFERENCE} 且当前类型 ∉ | **显式清空**（`null`） | **改型本身就是「不要分母」的意图**；不这么做会留下用户看不见也清不掉的残留分母（会把指标卡在发布闸门外，见 §7 M1） |
| 原来也不需要分母 | 原类型 ∉ {RATIO, DIFFERENCE} | 原样回传 `existing`（不破坏） | 这一行**从来没渲染过** ⇒ 用户没碰过 ⇒ 不该被静默抹掉 |

**关键取证**：antd 的 `validateFields` 只返回**已注册**字段；MEASURE 下分母行从未渲染 ⇒ 字段从未注册 ⇒
即使 `openEdit` 用 `setFieldsValue` 写过 store 值，`validateFields` 也不会回传 ⇒ 表单值恒为 `undefined`。
RED 实测确认：`expected null to deeply equal {agg:"COUNT", class_id:8, ...}`。

代码审查进一步取证（`rc-field-form@2.7.1` 源码）：`validateFields()` 返回
`getFieldsValue(namePathList)`，而 `namePathList` 只来自 `getFieldEntities(true)`；
字段卸载时 FieldEntity 被移除 ⇒ RATIO→MEASURE 改型后分母字段同样不在返回值里。

## 3. 数据模型变更

**无**。本批零 DDL、零迁移、零生产写。

涉及字段（仅前端读写语义，库结构不变）：

| 字段 | 说明 |
|---|---|
| `ontology_metric.denominator_measure` (JSONB) | 后端整块替换语义不变；前端改为「表单未暴露时原样回传」，不再提交 `null` |
| `ontology_metric.numerator_measure` (JSONB) | 不变（分子行总是渲染） |

## 4. 接口契约变更

**无 API / DTO 变更**。

- 后端 `PUT/PATCH ontology_metric` 的 `denominator_measure` 语义不变（`null` = 清空，仍是合法的显式清空通道）。
- 前端提交内容的变化是**取值规则**变化，不是契约变化：MEASURE 类型编辑保存时，分母 measure 由 `null` 变为原值。

## 5. 实现要点

### ① `backend/app/services/nl2sql_dialects.py`

`_DATE_ARITHMETIC_RULE_ORACLE` 的「② 最近 N 天」条目：

```python
"② 最近 N 天（含今天共 N 天，**必须带上下界**）："
"   WHERE date_col >= CURRENT_DATE - (N-1)"
"     AND date_col < CURRENT_DATE + 1"
"   例：最近 7 天 = 今天及前 6 天，"
"   WHERE date_col >= CURRENT_DATE - 6 AND date_col < CURRENT_DATE + 1；"
"   下界必须是 -(N-1)：用 -N 作下界会多算一天；"
"   漏掉上界还会把今天之后的未来数据算进来。"
```

（原先：`"② 最近 N 天：WHERE date_col >= CURRENT_DATE - N"` ⇒ 覆盖 N+1 天，且无上界。）

同族补齐（原本只给「加减」写法，没给窗口口径）：

| 方言 | 新增模板 |
|---|---|
| PostgreSQL | `date_col >= CURRENT_DATE - (N-1) * INTERVAL '1 day' AND date_col < CURRENT_DATE + INTERVAL '1 day'` |
| MySQL | `date_col >= DATE_SUB(CURRENT_DATE, INTERVAL (N-1) DAY) AND date_col < DATE_ADD(CURRENT_DATE, INTERVAL 1 DAY)` |

PG / MySQL 的首句同时加了一句显式警告（防 LLM 拿「加减写法」当窗口）：

> **「近 N 天」是窗口（今天及前 N-1 天），必须用下方的窗口模板；上面的加减写法不是窗口，用它会多算一天**

另：PG / MySQL 规则原先沿用 Oracle 的「②」编号却没有「①」⇒ prompt 里出现孤号，已去掉「② 」前缀。

### ② 前端

- **新增** `frontend/src/utils/metricTechnique.ts`：
  - `needsDenominatorMeasure(metricType)` —— 需要分母的类型（`RATIO` / `DIFFERENCE`）单一事实源
    （从 `MetricTechniqueForm.tsx` 挪过来，DRY）；
  - `resolveDenominatorMeasure(values, existing)` —— 分母取值规则（见 §2）；
  - `sanitizeMeasureSpec(spec)` —— 原样回传时**裁剪到后端 schema 允许的 5 个键**并返回**新对象**；
  - `DenominatorMeasureInput` —— 分母表单取值 + `metricType` + `originalMetricType`。
- **改** `frontend/src/components/ontology/MetricTechniqueForm.tsx`：改用 `needsDenominatorMeasure`，
  删掉本地常量。
- **改** `frontend/src/components/ontology/MetricTab.tsx`：
  - `ExistingMeasures` 增加 `metricType`（库里原类型，`openEdit` 写入、`openCreate` 留空）；
  - `buildMetricPayload` 的 `denominatorMeasure` 由 `toMeasureSpec(...)` 改为
    `resolveDenominatorMeasure({ metricType, originalMetricType, ... }, existing.denominator)`。

**依赖注入点**：无（纯函数 + 组件内调用）。

## 6. 测试

### RED 证据（改实现前亲眼看到失败）

| 用例 | 失败信息 |
|---|---|
| `test_日期算术规则_Oracle_最近N天_模板必须用_N减1_且带上界` | `Oracle 日期算术模板仍含 off-by-one 写法「CURRENT_DATE - N」` |
| `test_日期算术规则_PostgreSQL_最近N天_必须带_N减1_下界与上界` | `PG 规则必须覆盖「最近 N 天」窗口` |
| `test_日期算术规则_MySQL_最近N天_必须带_N减1_下界与上界` | `MySQL 规则必须覆盖「最近 N 天」窗口` |
| `MetricTab.test.tsx::度量型指标带历史分母时，编辑保存原样回传分母…` | `expected null to deeply equal { Object (agg, expr, ...) }` |
| `metricTechnique.test.ts::resolveDenominatorMeasure`（首版 4 条） | `TypeError: resolveDenominatorMeasure is not a function` |
| `MetricTab.test.tsx::把比率改成度量型时清空分母…`（审查后新增） | 审查前实现回传残留分母 ⇒ `expected { agg: "COUNT", … } to be null` |

### GREEN 证据（改实现后）

| 命令 | 结果 |
|---|---|
| `pytest app/tests/prompts/test_nl2sql_dialects.py app/tests/prompts/test_nl2sql_date_arithmetic_prompt.py app/tests/prompts/test_nl2sql_dialects_backcompat.py -q` | **42 passed** |
| `pytest app/tests/prompts/ app/tests/unit/test_time_scope_resolver.py -q` | **153 passed** |
| `npx vitest run src/tests/metricTechnique.test.ts src/tests/MetricTab.test.tsx` | **36 passed**（含新回归 + 既有 RATIO 回传用例未倒退） |
| `npx vitest run src/tests/OntologyPage.test.tsx src/tests/KpiCatalogPage.test.tsx src/tests/ontologyFilter.test.ts src/tests/ontologyMetricI18n.test.ts` | **101 passed**（邻近套件，净新增 0） |
| `npx tsc --noEmit` | exit **0** |

### 变异取证（证明守卫不是假绿）

把模板改回 off-by-one / 删掉上界后重算断言：

| 变异 | 断言仍为真？ |
|---|---|
| PG 下界回退成 `CURRENT_DATE - N * INTERVAL '1 day'` | **False** ✅（改前为 True ⇒ 原 PG 用例确实是假绿） |
| PG 删掉上界 `AND date_col < CURRENT_DATE + INTERVAL '1 day'` | **False** ✅ |
| MySQL 下界回退成 `INTERVAL N DAY` | **False** ✅ |
| Oracle 下界回退成 `CURRENT_DATE - N` | **False** ✅ |

### 新增/修改测试清单

| 文件 | 用例 |
|---|---|
| `backend/app/tests/prompts/test_nl2sql_dialects.py` | 3 条天级窗口回归（Oracle / PG / MySQL），断言**整段子句**而非片段；新增 `_normalized()` 辅助 |
| `frontend/src/tests/metricTechnique.test.ts` | `needsDenominatorMeasure`（2）+ `resolveDenominatorMeasure`（8：三条规则各覆盖 + 不可变 + 键裁剪 + 改型清空）+ `sanitizeMeasureSpec`（2） |
| `frontend/src/tests/MetricTab.test.tsx` | 2 条端到端：度量型带历史分母编辑保存 ⇒ 分母逐字段相等；**把比率改成度量型 ⇒ 分母清空** |

**反向守卫**：
- `resolveDenominatorMeasure` 的「比率 + 空表单值 ⇒ `null`」一条保证本修复**不会**变成
  「什么都不敢清空」—— 用户在比率类型下显式清空分母仍是合法意图；
- 「把比率改成度量型 ⇒ `null`」一条保证本修复**不会**变成「什么残留都不清」——
  改型是明确的用户意图，不该留下看不见也清不掉的残留。

**覆盖率**：本批改动为 4 个纯函数 + 2 个模板常量 + 2 处调用点，均被新用例直接覆盖（新增代码行覆盖 100%）。

## 7. 安全审查与代码审查

**触发**：`security-reviewer` 与 `code-reviewer` 两个 agent 均已审完本批 diff（结论如下，非事后补写）。

- 认证 / 授权 / 密钥：**未触及**。
- SQL 注入：**未触及**（本批只改 **prompt 模板文本**，不新增任何 SQL 执行路径；生成的 SQL 仍走既有 SQL Guard）。
- 用户输入 / 外部 API / 文件操作 / 加解密 / 支付：**未触及**。

### 7.1 结论汇总

| 级别 | 数量 | 处置 |
|---|---|---|
| CRITICAL | 0 | — |
| HIGH | 0 | — |
| MEDIUM | 4 | 2 条已修（见 §7.2）、2 条已分析并采纳为「有意取舍 + 显式记录」（见 §7.3） |
| LOW | 8 | 4 条已修、4 条记为已知项/有意取舍 |

### 7.2 已修的审查发现

| # | 来源 | 发现 | 处置 |
|---|---|---|---|
| M1' | security | 回传**原始 JSONB 字典**可能带 schema 之外的键 ⇒ 后端 `MeasureSpecSchema`（`extra="forbid"`）⇒ 整次保存 422（比静默清空更糟） | 新增 `sanitizeMeasureSpec()`：只回传后端允许的 5 个键（`agg`/`expr`/`filter`/`class_id`/`class_alias`），字段类型不对时降级为 `null`。**同时**满足「不可变数据」硬约束（返回新对象，不把 API 响应对象的引用交出去） |
| M2' | code-review | 改型（RATIO→MEASURE）后分母变成「看不见也清不掉」的残留 ⇒ `kpi_metric_reconcile.structuredColumns()` 把残留分母的列算进比对 ⇒ `VERDICT_MISMATCH` ⇒ `degraded=true` ⇒ 发布闸门不过（409），且报错指向用户看不到的字段 | 按 code-review 建议把**原类型**带进判定：原类型 ∈ {RATIO, DIFFERENCE} 且当前类型 ∉ ⇒ 视为显式清空（见 §2 三条规则）。新增端到端用例「把比率改成度量型时清空分母」 |
| L-1 | code-review | PG 回归用例断言过弱（`"N-1" in rule` 会被说明文字满足、`"INTERVAL '1 day'"` 会被示例行满足）⇒ 模板回退成 off-by-one 也测不出来（**假绿**） | 三方言统一改为断言**整段子句**（`_normalized()` 压空白后 `find(...)`）。**已做变异取证**：把下界改回 `- N` / 删掉上界 / 改 MySQL 下界 ⇒ 三条断言全部变红（变异前 Oracle/MySQL 有效、**PG 无效** ⇒ 原用例确实是假绿） |
| L-2 | code-review | `resolveDenominatorMeasure` 把 `existing` 引用直接交出去 | 同 M1'：`sanitizeMeasureSpec` 返回新对象 |
| L-3 | code-review | 两处测试语句被压到同一行（`metricTechnique.test.ts` / `MetricTab.test.tsx`） | 已恢复格式 |
| L-4 | code-review | PG / MySQL 规则沿用 Oracle 的「②」编号但没有「①」⇒ prompt 里出现孤号 | 已去掉「② 」前缀 |
| L-5 | code-review | PG / MySQL 规则首句仍在教易错写法（`日期列 - INTERVAL 'N' DAY`）⇒ LLM 可能拿加减写法当窗口 | 首句加了一句显式警告：「『近 N 天』是窗口（今天及前 N-1 天），必须用下方的窗口模板；上面的加减写法不是窗口，用它会多算一天」 |

### 7.3 已分析、采纳为「有意取舍」的发现（须让后续处理者知道）

**M1（security）保留历史分母会改变发布闸门/对账判定** —— 两方向都分析过，结论是**不构成新缺陷**：

- **偏严方向**（MEASURE 携带残留分母 ⇒ `structuredColumns()` 多算分母列 ⇒ MISMATCH ⇒ 409）：
  这条**不是本批引入的**。残留分母在**编辑之前**就在库里，`kpi_metric_reconcile` 早就把它算进比对、
  早就判 MISMATCH、早就挡住发布。本批修掉的是「编辑时顺手把残留分母清掉」这个**意外的逃生通道**
  —— 即：修复前，一次改名能让指标从「发不出去」变成「发得出去」，代价是**静默删掉用户没碰过的数据**。
  修复后，**编辑不再改变库里的状态** ⇒ 发布闸门的判定只随用户真正改动的内容变化。
- **偏宽方向**（治理侧声明了 `denominator_columns` 时，MEASURE 也比分母 ⇒ 可能从「被拦」变「放行」）：
  同理，闸门判定基于**库里的状态**，而本批不改变它（除了用户显式改型那种情形，且那与修复前一致）。
  「治理侧声明了分母、技术侧也有匹配的分母」本身就是该闸门想放行的状态。
- **仍然存在的坑（须记录，不在本批修）**：
  1. UI 上**无法清理** MEASURE 的残留分母（表单不渲染分母行 ⇒ 没有清空入口）。
     绕行：先改成 RATIO（分母行渲染）→ 清空 → 改回 MEASURE；或直接改库。
  2. 治本需要**产品口径 + 数据清理**：是否禁止 MEASURE 携带分母、是否在服务端做
     「类型 ⇔ 分母」归一、是否要清理历史遗留行。本批**不替产品做这个决定**。
  3. 本批已修的改型路径（RATIO→MEASURE 显式清空）顺带**缩小**了残留的产生面。

**L1（security）UI 无法清空残留分母** —— 与上条同源，**有意**保留（非破坏性取舍），已写入 wiki §15；
产品若要求 UI 可见可清，需另立需求（属第二梯队「产品语义待定」项）。

**L2/L3（security）文档信息泄露面** —— 变更记录里的本机绝对路径、端口号等属仓库既有惯例
（`_template/summary.md` 的 MEMORY 链接即该形态），**不新增**任何凭据 ⇒ 判为可接受，不改。

**L4（security）同源对账 finding** —— 与 M1 同源，已在上面一并处置。

### 7.4 已知项（本批明确不做，须留给下一批）

| # | 已知项 | 处置 |
|---|---|---|
| K1 | **PG / MySQL 仍缺「最近 N 个月」窗口模板**（自然月口径只有 Oracle 有 ⇒ PR #89 只修了 Oracle）。对 PG/MySQL 数据源问「近 6 个月」，LLM 仍会用 `- INTERVAL 'N' MONTH`（滚动月，多算一个月且与 Oracle 的自然月口径不一致） | 本批只做天级同族（计划已定范围）。**且**滚动月 vs 自然月对 PG/MySQL 属**语义变更**（今天 PG/MySQL 是滚动月，Oracle 是自然月）⇒ 需要业务口径确认后再动，不能顺手改。已记入 `plan.md` 下一批候选 |
| K2 | Oracle 月份标签示例仍是 `ADD_MONTHS(CURRENT_DATE, -N)`（PR #89 遗留）⇒ 给的是 N+1 个标签 | 与窗口口径无关（是标签序列，不是窗口），本批不动 |
| K3 | 指标写接口无 `requireAdmin` / 归属校验（`backend/app/api/v1/ontology.py:343-355`） | **既有**问题，与本批无关；不在本批 diff 范围（见第二梯队） |

## 8. 部署验证

**本批不部署**（零 DDL、零生产写；两处都是纯逻辑/prompt 改动，随下次常规发版生效）。

发版源要求（沿用既有规则）：**必须从 fresh worktree 部署**，且前端 build 时显式置空
`VITE_API_BASE_URL`；`docker compose stop` 而非 `down`；`docker exec` 带 `-i`。

冒烟项（发版时执行，本批不执行）：

1. 后端：`python3 -m app.infrastructure.schema_drift --strict`（本批零 DDL ⇒ 应与发版前一致）；
2. 后端：问一句「近 30 天的供货量」⇒ 看生成的 SQL 的 WHERE 是否为
   `date_col >= CURRENT_DATE - 29 AND date_col < CURRENT_DATE + 1` 形态；
3. 前端：编辑一个度量型指标（库里带分母）⇒ 只改名字保存 ⇒ 复查 `ontology_metric.denominator_measure` 未变。

## 9. 关联

- 计划（本批的上游）：[plan.md](./plan.md)
- Wiki：
  - [`Harness/wiki/nl2sql-engine.md`](../../wiki/nl2sql-engine.md) —— 「相对时间窗口契约（近 N 个月 / 近 N 天）」
  - [`Harness/wiki/kpi-metric-technical-form.md`](../../wiki/kpi-metric-technical-form.md) —— §15「前端编辑契约：measure 的「不破坏」规则」
- Rules：[`Harness/rules/开发流程规范.md`](../../rules/开发流程规范.md)、[`Harness/rules/测试规范.md`](../../rules/测试规范.md)
- Memory：`qa-system-natural-month-window.md`、`qa-system-measure-edit-clears-denominator.md`（均已更正为「已修」）
- 关联变更（predecessor）：[2026-10-31-term-drift-date-template](../2026-10-31-term-drift-date-template/summary.md)、
  [2026-10-28-kpi-metric-technical-form](../2026-10-28-kpi-metric-technical-form/summary.md)
- 关联变更（successor）：待 ④–⑦ 与第二梯队立项

## 10. CI 闸门实操记录（2026-11-05，本批合并过程）

本批合并判据走 `merge-gate / failure-set-diff`（main 已启用分支保护、必需检查含它、
`enforce_admins=true` ⇒ 绕过必须是显式越权，本批不走这条路）。过程本身暴露了闸门链路的
三个事实，全部记在这里，避免下次重复踩。

### 10.1 第一次判定：UNUSABLE_JOBS（净新增 0，但不可用）

- 现象：PR run `42034609557`（attempt 1）判 `UNUSABLE_JOBS`；报告同时给出「净新增 0 条 / 净丢失 36 条」。
- 根因：`Backend / integration shard 3` 结论是 `cancelled` —— 它撞了 job 级 `timeout-minutes: 90`
  被杀（13:19:54Z 起跑，14:50:08Z 被杀）。**cancelled ⇒ 一个用例都没跑完 ⇒ 失败集合是空的**
  ⇒ 不能拿它跟基线对差。
- 判为 flake 的依据：基线 run（41961157752）里同一分片只用了 23 min；且本批 diff
  **一个 integration 测试文件都没碰**（后端只改 `nl2sql_dialects.py` 的 prompt 文本，
  测试只动 `app/tests/prompts/`）⇒ 与本批改动无关。
- 「净丢失 36 条」是 cancelled 的**必然假象**：基线里 shard 3 的 36 条失败在 PR 侧是空集，
  于是全被算成「丢失」。⇒ **cancelled 情形下「净丢失」那一栏是噪声，不是回归信号。**

### 10.2 复跑后的两次判定：闸门链路的三个事实

复跑方式：`gh run rerun 42034609557`（**整轮**，attempt 2）。复跑后闸门**触发了两次**（相隔 29 秒）：

| 判定 | 时刻(UTC) | 读到的 job 列表 | 结论 |
|---|---|---|---|
| #1 | 15:55:34 | **上一 attempt**（`shard 3` 仍 `cancelled`，job=127270071084） | UNUSABLE_JOBS（**假不可用**） |
| #2 | 15:56:03 | attempt 2（`shard 3` = failure 36 条；PR 合计 **140 条 = 基线 140 条**） | COVERAGE_MISMATCH（**假净新增**） |

三个事实：

1. **run 级 `completed` 早于 job 列表切到新 attempt**（API 滞后）⇒ 判定 #1 读到的仍是上一 attempt
   的 job ⇒ 又一次假「不可用」。
2. **闸门自己的 check run 会留在 head sha 上**：每次判定都用 `POST /check-runs` 在 head sha 上新建
   一个名为 `merge-gate / failure-set-diff` 的 check run。判定 #2 把它当成「PR 多跑的一个 job」
   ⇒ `COVERAGE_MISMATCH` + 假净新增 `job:merge-gate / failure-set-diff`。
   ⇒ **同一个 sha 上只有「第一次判定」可能是干净的**；复跑后的再判定必然被自己的残留污染。
3. **`DELETE /repos/{o}/{r}/check-runs/{id}` 对已完成的 check run 返回 404**（只能删 queued/in_progress）
   ⇒ 残留清不掉 ⇒ 只能靠「换 sha + 全新 run」绕过。

### 10.3 恢复流程（下次照做）

1. 先看每个 job 的 `conclusion`：`cancelled` ⇒ 失败集合为空 ⇒ 该 job 必须复跑；
   `failure` 但耗时异常（撞 `timeout-minutes`）⇒ 同样复跑。
2. **整轮复跑**：`gh run rerun <run_id>`。**不要**只复跑单个 job ——
   `ci_failure_set` 取 job 列表用 `runs/{id}/jobs`（默认只给最新 attempt），
   单 job 复跑会让最新 attempt 里只剩那一个 job ⇒ 与基线的 job 名集合不等价 ⇒ COVERAGE_MISMATCH。
3. **复跑后仍被自己的残留污染时**：换 sha（推一个新提交）换一个新 run ⇒ 新 sha 上没有残留 check run
   ⇒ 第一次判定就是干净的。本批据此再推一提交。
4. 抖动：工具**不做抖动容忍**（单条失败翻转也判不可合并）。本批实测到一处翻转
   （`test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent`：基线红、PR attempt 2 绿），
   属 Milvus 侧 flake —— 本批 diff 与 Milvus 无关。

### 10.4 净丢失 1 条的人工仲裁（2026-11-06）

换 sha 后的第一次判定（run `42061730479`，head `7f5faac8`）结论是
**净新增 0 条 / 净丢失 1 条 ⇒ `NEEDS_REVIEW`（退出码 5，闸门判 failure）**。
按闸门设计，退出码 5 是「须人工确认不是把失败藏起来了」，不是回归。
本节记录该条的人工仲裁结论与证据。

**净丢失的那一条**：`app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent`

| 侧 | 结论 | 证据 |
|---|---|---|
| 基线（main，run `41961157752`，job `127027985627`，shard 3） | **失败** | `assert written2 == 0` 实得 `1`（`test_reconcile_milvus_id_mapping.py:187`），日志时间 `2026-11-05T03:46:28Z` |
| PR（head `7f5faac8`，run `42061730479`，job `127364941043`，shard 3） | **通过** | 同一测试不在该 job 的 `FAILED` / `ERROR` / `SKIPPED` 清单里（只出现在 warnings 汇总段） |

**它为什么是 flake（机制）**：`backend/scripts/backfill_milvus_external_id.py` 的 backfill 动作是
「删掉旧 Milvus 行（auto-id 主键）→ 带 `external_id` 重新插入」。第二次 backfill 若在
Milvus 的可见性收敛前又查到了那条 `external_id` 为空的旧行，就会再写一次 ⇒ `written2 == 1`。
该竞态由 Milvus 的可见性/延迟决定，与代码改动无关。

**为什么本批不可能是「把失败藏起来」**：

1. 本批 diff 与 Milvus、ontology 向量同步、`id_mapping` **完全无关**
   （后端只改了 `backend/app/services/nl2sql_dialects.py` 的 prompt 模板；
   其余是前端 `metricTechnique.ts` 等 4 个 UI 文件 + 文档）；
2. 本批 diff **一个 integration 测试文件都没碰** ⇒ 失败用例的断言、夹具、执行顺序均未变；
3. 两次 run 的该 job 日志里**同样出现 2 条** `Milvus 类向量自动同步失败 … Embedding 缺少 API Key`
   ⇒ 环境等价，差异只在竞态落点；
4. 净新增为 **0** ⇒ 本批没有把任何原本绿的用例弄红。

⇒ **仲裁结论：净丢失 1 条 = 基线侧的 Milvus 可见性竞态（flake），不是回归，也不是本批隐藏失败。**

**闸门产物 vs 人工结论的关系**：闸门给出的是「机器可复算的事实」（净新增 0、净丢失 1）；
「净丢失是否可接受」按设计就是人工判断项（退出码 5 的分支）⇒ 以上仲裁即为该判断的正式记录。

### 10.5 最终判定

见本节下方的合并回填（合并后由 main 上的回填提交补齐）。

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（① 4 个候选、② 3 个候选）
- [x] 第 3 段迁移文件名 ≤ 32 字符（本批**无迁移**，已注明零 DDL）
- [x] 第 7 段触发了 security-reviewer 且填了级别结论（**security-reviewer + code-reviewer 双审**；
      CRITICAL 0 / HIGH 0 / MEDIUM 4（2 已修、2 已分析并采纳为有意取舍）/ LOW 8（4 已修、4 记为已知项）；
      另附**变异取证**证明守卫不是假绿）
- [x] 第 8 段 docker compose 冒烟命令 + 输出贴出 —— **本批不部署**，已写明理由与发版时的冒烟项
- [x] 第 9 段 ≥ 3 个跨文件链接（plan.md / 2 个 wiki / 2 个 rules / 2 个 memory / 2 个关联变更）
- [x] 相关 wiki 文档已更新（nl2sql-engine.md + kpi-metric-technical-form.md）
- [x] 至少 1 条 MEMORY 索引已在 `MEMORY.md` 更新（3 条：natural-month-window / measure-edit-clears-denominator / l4-hijacks-multistep 索引行更正）
- [ ] 涉及真实 SQL/DB 改动时 `scripts/<feature>_realdata.py` 已跑通 —— **不适用**（本批零 DDL、零生产写、零生产读）

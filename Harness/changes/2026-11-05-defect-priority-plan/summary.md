# 变更：2026-11-05-defect-priority-plan（缺陷优先级处置 · 第一批）

- **日期**：2026-11-05
- **作者**：Claude
- **Phase**：Phase 3 NL2SQL / 前端本体治理
- **状态**：done（本批只做 ①②；③ 已由 PR #78 修复；④–⑦ 与第二梯队见 `plan.md`）
- **关联变更**：[2026-10-31-term-drift-date-template](../2026-10-31-term-drift-date-template/summary.md)（自然月窗口修复，本次补齐天级同族）、[2026-10-28-kpi-metric-technical-form](../2026-10-28-kpi-metric-technical-form/summary.md)（measure 结构化）
- **迁移版本**：无（本批零 DDL）
- **计划**：[plan.md](./plan.md)（今日待办计划）
- **MEMORY**：[qa-system-natural-month-window](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-natural-month-window.md)、[qa-system-measure-edit-clears-denominator](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-measure-edit-clears-denominator.md)

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
| **A. 表单没暴露分母行 ⇒ 原样回传 `existing.denominator`** | 不破坏；语义之争（改型是否该清分母）留给业务口径明确时再定 | ✅ **采纳** |
| B. 禁止 MEASURE 携带分母（后端加约束 + 前端拒绝） | 更彻底，但会拒掉合法历史数据、要求数据迁移 ⇒ 超出本批范围 | ❌ 否决 |
| C. 前端在 MEASURE 下也渲染分母行 | 暴露用户不关心的字段，且改动 UI | ❌ 否决 |

**最终决定**：采纳 A。分两条规则实现：
表单**暴露**了分母行 ⇒ 表单值说了算（用户显式清空 ⇒ `null`，是合法意图）；
表单**没暴露** ⇒ 原样回传 `existing`（不破坏）。

**关键取证**：antd 的 `validateFields` 只返回**已注册**字段；MEASURE 下分母行从未渲染 ⇒ 字段从未注册 ⇒
即使 `openEdit` 用 `setFieldsValue` 写过 store 值，`validateFields` 也不会回传 ⇒ 表单值恒为 `undefined`。
RED 实测确认：`expected null to deeply equal {agg:"COUNT", class_id:8, ...}`。

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

### ② 前端

- **新增** `frontend/src/utils/metricTechnique.ts`：
  - `needsDenominatorMeasure(metricType)` —— 需要分母的类型（`RATIO` / `DIFFERENCE`）单一事实源
    （从 `MetricTechniqueForm.tsx` 挪过来，DRY）；
  - `resolveDenominatorMeasure(values, existing)` —— 分母取值规则（见 §2）；
  - `DenominatorMeasureInput` —— 分母表单取值 + `metricType`。
- **改** `frontend/src/components/ontology/MetricTechniqueForm.tsx`：改用 `needsDenominatorMeasure`，
  删掉本地常量。
- **改** `frontend/src/components/ontology/MetricTab.tsx`：`buildMetricPayload` 的
  `denominatorMeasure` 由 `toMeasureSpec(...)` 改为 `resolveDenominatorMeasure(...)`。

**依赖注入点**：无（纯函数 + 组件内调用）。

## 6. 测试

### RED 证据（改实现前亲眼看到失败）

| 用例 | 失败信息 |
|---|---|
| `test_日期算术规则_Oracle_最近N天_模板必须用_N减1_且带上界` | `Oracle 日期算术模板仍含 off-by-one 写法「CURRENT_DATE - N」` |
| `test_日期算术规则_PostgreSQL_最近N天_必须带_N减1_下界与上界` | `PG 规则必须覆盖「最近 N 天」窗口` |
| `test_日期算术规则_MySQL_最近N天_必须带_N减1_下界与上界` | `MySQL 规则必须覆盖「最近 N 天」窗口` |
| `MetricTab.test.tsx::度量型指标带历史分母时，编辑保存原样回传分母…` | `expected null to deeply equal { Object (agg, expr, ...) }` |
| `metricTechnique.test.ts::resolveDenominatorMeasure`（4 条） | `TypeError: resolveDenominatorMeasure is not a function` |

### GREEN 证据（改实现后）

| 命令 | 结果 |
|---|---|
| `pytest app/tests/prompts/test_nl2sql_dialects.py app/tests/prompts/test_nl2sql_date_arithmetic_prompt.py app/tests/prompts/test_nl2sql_dialects_backcompat.py -q` | **42 passed** |
| `pytest app/tests/prompts/ app/tests/unit/test_time_scope_resolver.py -q` | **153 passed** |
| `npx vitest run src/tests/metricTechnique.test.ts src/tests/MetricTab.test.tsx` | **28 passed**（含新回归 + 既有 RATIO 回传用例未倒退） |
| `npx vitest run src/tests/OntologyPage.test.tsx src/tests/KpiCatalogPage.test.tsx src/tests/ontologyFilter.test.ts` | **85 passed**（邻近套件，净新增 0） |
| `npx tsc --noEmit` | exit **0** |

### 新增/修改测试清单

| 文件 | 用例 |
|---|---|
| `backend/app/tests/prompts/test_nl2sql_dialects.py` | 新增 3 条（Oracle 天级上下界；PG 天级；MySQL 天级） |
| `frontend/src/tests/metricTechnique.test.ts` | 新增 `needsDenominatorMeasure`（2 条）+ `resolveDenominatorMeasure`（4 条）纯函数契约 |
| `frontend/src/tests/MetricTab.test.tsx` | 新增 1 条端到端（度量型带历史分母编辑保存 ⇒ 分母逐字段相等） |

**反向守卫**：`resolveDenominatorMeasure` 的「比率 + 空表单值 ⇒ `null`」一条保证本修复**不会**变成
「什么都不敢清空」—— 用户在比率类型下显式清空分母仍是合法意图。

**覆盖率**：本批改动为 3 个纯函数 + 1 个模板常量 + 1 处调用点，均被新用例直接覆盖（新增代码行覆盖 100%）。

## 7. 安全审查

**触发**：`security-reviewer` 已被派发并审完本批 diff。

- 认证 / 授权 / 密钥：**未触及**。
- SQL 注入：**未触及**（本批只改 **prompt 模板文本**，不新增任何 SQL 执行路径；生成的 SQL 仍走既有 SQL Guard）。
- 用户输入 / 外部 API / 文件操作 / 加解密 / 支付：**未触及**。

| 级别 | 结论 |
|---|---|
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 0 |
| LOW | 1（NOTE）：前端「表单没暴露 ⇒ 原样回传」意味着 MEASURE 类型的分母在 UI 上**无法清空**（只能靠改类型或后端）。这是**有意**的非破坏性取舍，已写入 wiki §15；产品若要求可清空，需另立需求。 |

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

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（① 4 个候选、② 3 个候选）
- [x] 第 3 段迁移文件名 ≤ 32 字符（本批**无迁移**，已注明零 DDL）
- [x] 第 7 段触发了 security-reviewer 且填了级别结论（LOW 1 条 NOTE）
- [x] 第 8 段 docker compose 冒烟命令 + 输出贴出 —— **本批不部署**，已写明理由与发版时的冒烟项
- [x] 第 9 段 ≥ 3 个跨文件链接（plan.md / 2 个 wiki / 2 个 rules / 2 个 memory / 2 个关联变更）
- [x] 相关 wiki 文档已更新（nl2sql-engine.md + kpi-metric-technical-form.md）
- [x] 至少 1 条 MEMORY 索引已在 `MEMORY.md` 更新（3 条：natural-month-window / measure-edit-clears-denominator / l4-hijacks-multistep 索引行更正）
- [ ] 涉及真实 SQL/DB 改动时 `scripts/<feature>_realdata.py` 已跑通 —— **不适用**（本批零 DDL、零生产写、零生产读）

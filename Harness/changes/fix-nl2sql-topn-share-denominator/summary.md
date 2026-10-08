# 变更：fix-nl2sql-topn-share-denominator

- **日期**：2026-10-02
- **作者**：Claude / 启琳
- **Phase**：Phase 12 NL2SQL 引擎（prompt 规则）
- **状态**：done
- **关联变更**：[../fix-oracle-aggregate-guard/summary.md](../fix-oracle-aggregate-guard/summary.md)（同一 Top-N 占比故障家族的前一环）；[../2026-10-01-nl2sql-structural-formula-guard/summary.md](../2026-10-01-nl2sql-structural-formula-guard/summary.md)（首发环节）
- **迁移版本**：无
- **MEMORY**：[qa-system-nl2sql-topn-share-denominator.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-nl2sql-topn-share-denominator.md)

---

## 1. 需求

**背景**：用户连续问同一问题「B019 圣特、B125 浙江力航、B153 天津市精一 这三家供应商4月供货量最多的三种物料在4月份总的供货量的占比分别是多少？」，结果**时对时错**：

| 时间（UTC） | session_message | 结果 |
|---|---|---|
| 10-01 12:38 | 1219 | ✅ 65.2% / 44.3% / 28.4% |
| 10-01 13:32 | 1229 | ✅ 同上 |
| 10-01 22:28 | 1239 | ❌ **三家全部 100%**，且模型编造「实际物料不超过 3 种」 |
| 10-01 22:33 / 22:35 | 1243 / 1249 | ✅ 同上 |

100% 那次直接导致用户以为与另一问（「都供了哪些物料」→ 165 种）矛盾。验收标准：同一问题反复问，不再出现恒 100%。

## 2. 设计评审

| # | 候选方案 | 取舍 | 决定 |
|---|---|---|---|
| A | **prompt 否定性约束**：在 SQL 阶段规则 8 与计划阶段规则 4 补「分母不得与过滤同块」 | 与既有修复同款机制；禁令比正例更可迁移（fix-oracle-aggregate-guard 真机观察：模型采纳的是禁止而非范例）；成本一次 | ✅ 采用 |
| B | **执行期检测**：占比列恒等于 1 时警告/重试 | 能兜底但误报面大（存在合法的 100%），且治标 | ❌ 不做 |
| C | 不修（非确定性，多数运行正确） | 「多数时候对、偶尔错且错得理直气壮」比每次都错更毁信任 | ❌ 拒绝 |

**根因（已实证）**：错误运行的 SQL 为
`SELECT SUM(ITEM_QTY) / SUM(SUM(ITEM_QTY)) OVER (PARTITION BY ...) FROM ranked WHERE RNK <= 3`。
SQL 逻辑执行顺序 `WHERE → GROUP BY → 窗口函数` ⇒ 分母只剩 Top-3 行自己 ⇒ 占比恒为 1。
已把该 SQL 原样在生产 Oracle 复跑，输出 `top3_share = 1` ×3 行，与用户所见一致。
危险形态正是 prompt 规则教出来的：规则 4/8 处方「占比用 `SUM(x)/SUM(SUM(x)) OVER ()`」
在无过滤时正确、与 Top-N 过滤同块时必错。

## 3. 数据模型变更

无。

## 4. 接口契约变更

无对外 API 变更。仅 System Prompt 文本（`nl2sql_prompts.py`）。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `app/services/nl2sql_prompts.py` | **SQL 阶段规则 8** 追加：Top-N 与占比/总数同算时，分母必须来自**未被 Top-N 过滤**的结果集（单独建 CTE 汇总总量再 JOIN，或在 `WHERE RN<=N` 之前用窗口函数算好总数）；同块过滤 ⇒ 占比恒为 1，注明真实事故。**计划阶段规则 4「简单形式」**补适用范围限定：只适合无 Top-N 过滤的场景，含 Top-N 的占比必须用 CTE 复杂形式 |
| `app/tests/unit/test_nl2sql_service.py` | 新增 `TestTopnShareDenominatorRule` 3 例 |

约束放在**基础规则**而非方言规则：WHERE 先于窗口函数是所有 SQL 方言的共性。

## 6. 测试

| 用例 | 断言 | 修前 |
|---|---|---|
| `test_sql_prompt_warns_denominator_must_not_share_filter_block` | SQL prompt 含「未被 Top-N 过滤」「占比恒为 1」「单独建 CTE」 | 红 ✅ |
| `test_plan_prompt_simple_formula_form_carries_topn_caveat` | 计划 prompt 含「只适合无 Top-N 过滤的场景」 | 红 ✅ |
| `test_rule_applies_to_non_oracle_dialects_too` | PG prompt 同样含警告（非方言规则） | 红 ✅ |

回归：`test_nl2sql_service.py` **171 passed**；prompt/dialect 相关 64 passed；
渲染实测规则编号 1–13 连续、Oracle 与 PG 均含警告。

## 7. 安全审查

未触发 security-reviewer：纯 prompt 常量文本，不触及认证/输入处理/SQL Guard 等任何触发条件。

## 8. 部署验证

`./scripts/deploy_backend.sh` 启动成功；容器内 `_buildSystemPrompt` 含新警告（实测 True）。

**真机回归（非确定性故障必须采样多次）**：同一问题连问 **4 次**，4/4 正确形态
（分母独立 CTE `sup_total`），占比 65.21% / 44.26% / 28.40%，无一次恒 100%，
无 error 事件。数值与历史正确运行逐位一致。

## 9. 关联

- **Wiki**：[Harness/wiki/nl2sql-engine.md](../../wiki/nl2sql-engine.md) §多数据源（方言/规则表）
- **关联变更**：`../fix-oracle-aggregate-guard/summary.md`（同族前一环）
- **提交**：`eeab52b`（代码）

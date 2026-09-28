# 提案：查询计划的「可查询性」闸门（半空计划仍可直达 SQL 生成）

- **日期**：2026-09-26
- **状态**：proposal（未排期）
- **来源**：`fix-plan-drop-observability`（M3）批次的 security-reviewer **MEDIUM-1** 与 code-reviewer **LOW-6**——两位审查独立命中同一条
- **关联**：[fix-plan-drop-observability](fix-plan-drop-observability/summary.md)、`Harness/wiki/nl2sql-engine.md`（计划解析契约段）、`Harness/wiki/chat-service-assessment.md` §2.3 M3

## 问题

M3 已把「**全空**计划」判为解析失败（走重试 → 用尽后「无法回答」）。但 `validatePlan` 的语义是**只校验已存在的引用**：

```
backend/app/services/nl2sql_service.py:1822  def validatePlan(plan, classes) -> list[str]
```

它遍历 `selectedClasses` / `selectedProperties` / `aggregations` / `groupBy` / `joins` / `sortBy` 并逐项比对本体 schema；**没有任何一项时 `issues == []`，判定为通过**。于是下列形态在 M3 之后仍然一路可通，直达 `generateSql`：

| 形态 | 为何通过解析 | 为何通过校验 | 后果 |
|---|---|---|---|
| `{"target": "查询采购情况"}` | target 非空 ⇒ 非「全空」 | 无引用可校验 | 模型在零选表约束下自由选表/编造表名 |
| `{"conditions": ["供应商=XX"]}` | conditions 非空 ⇒ 非「全空」 | conditions **全链路无任何存在性校验** | 同上；且 `_hasQueryScope` 还据此判定「查询已被收窄」 |
| `{"rowLimit": 100}` | `rowLimit is not None` ⇒ 非「全空」 | 无引用可校验 | 同上（一条「限制」被当成了「有内容」） |
| `{"target": "查XX", "aggregations": ["SUM(数量)"]}`（聚合条目是字符串 ⇒ 整体被丢弃，走 `PLAN_DEGRADED` 只记日志不失败） | 同上 | 同上 | 同上 |

**性质澄清（避免高估）**：这**不是**越权 —— 终极闸门 `_assert_read_only`（仅 SELECT/WITH）仍生效，业务查询本身就是只读授权。后果是**可用性与可诊断性**：模型编造的表名会在库侧报错并被包装成「服务内部错误」，而这正是 M3 想堵的失败形态（M3 只堵住了其中最确定的一种）。同理，`_buildSystemPrompt` 会把这类计划当「引用均已被校验」的已确认计划描述给模型，而实际上一个引用都没校验过。

## 为什么没有在本批顺手放宽（两条实测约束）

M3 的口径是**最窄**的（「所有字段都空」才算空），这是刻意取捨。放宽需要先解决两件事，均非「顺手改」的量级：

1. **现有用例把「target 非空的计划视为合法」写死了。** `test_nl2sql_service.py`
   `test_empty_plan_retries_and_succeeds_on_second_attempt` 的第二跳回复是 `'{"target": "查询"}'`
   并断言成功；`test_clean_plan_has_no_drops` 同样以 `{"target": "查询"}` 为干净计划。
   放宽会让这些用例转红 ⇒ 属**口径变更**，需要明确决策，而不是顺手改。
2. **多步 / REFINE 路径尚未核对。** 实测 `prior_state` 是以**文本**注入
   （`chat_service.py:2232` → `generateQueryPlan(prior_state=...)`），全仓**没有任何代码**
   把前序计划的引用来填进当步计划（`grep "replace(plan"` 只有 `rowLimit` 归一与 JOIN
   注入两处）。这一点**支持**放宽（引用必须由当步计划自带），但也意味着放宽后
   「模型没选表」的当步会消耗重试预算，须先确认没有流程依赖该形态。

## 候选方案

| # | 方案 | 范围 | 评价 |
|---|---|---|---|
| A | **最窄子修**：把 `rowLimit` / `perGroupLimit` 从 `_isEmptyPlan` 的「有内容」证据里移除（只认 target / 引用 / conditions / aggregations / groupBy / joins / sortBy / partitionBy） | 堵住 `{"rowLimit": 100}` | 影响面最小、无需动既有用例口径。但 `{"target": ...}` 仍漏 |
| B | **完整修**：判定改为「无可查询引用」——`selectedClasses/selectedProperties/conditions/aggregations/groupBy/joins/sortBy/partitionBy` 全空即失败（不看 target / rowLimit / perGroupLimit） | 堵住上表全部四种 | 需先做下面的前置核对，并同步迁移第 1 条约束里的用例（属口径变更，须先拍板） |
| C | **结构性修（推荐长期）**：闸门放在**合并后**的有效计划上（多步 / REFINE 合并 + 全局过滤之后），而不是解析出口 | 覆盖全部路径 | 只有那时「有引用才能生成 SQL」才是可判定的；解析出口继续只判「全空」这一确定无疑的形态，避免误杀 |

方案 A 与 B 互斥（B 含 A）；C 与 A/B 正交，若做 C 则 B 可退化为 A。

## 前置核对清单

- [ ] 多步场景：当步计划是否可能**合法地**不带引用（引用经 `prior_state` 文本让模型复述而非结构化继承）→ 抓一次真实多步 trace 核对
- [ ] REFINE 场景：`chat_service._statePlan` 的历史计划 + 重写计划，是否存在「重写只改条件、引用沿用历史」的形态
- [ ] 全局过滤（`global_filters` / `[global_constraints]`）是否可能成为唯一的「范围」来源
- [ ] 放宽后：重试预算是否够（`maxPlanAttempts`）—— 把「没选表」也纳入重试，是否会把原本一次成功的问句拖成失败

## 验收标准

1. 上述四种形态**都**进重试；重试反馈可操作（明确指出「未选择任何类/属性」），用尽后落「无法回答」；
2. 合法空计划 `target="无法回答"`（`isUnanswerable` 短路）**不受影响**；
3. 多步 / REFINE / 全局过滤的既有集成用例全绿（含真实 PG + 完整 API 链路）；
4. 解析出口的日志仍按 `reason=` 单点聚合（M3 契约不破）。

## 不修的风险（为何可暂缓）

- 无越权面：只读闸门独立生效（见上「性质澄清」）；
- 影响面是「可用性 + 可诊断性」，且触发依赖模型给出这类残形态（模型通常会给全计划）；
- 但**不建议长期挂着**：它正是 M3 声明要堵的那条路径的残余，且 `_hasQueryScope` 会把
  `conditions`-only 当成「已收窄」，可能顺带让兜底行数限制失效。

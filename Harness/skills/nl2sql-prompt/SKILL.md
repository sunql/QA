---
name: nl2sql-prompt
description: NL2SQL Prompt 工程与 SQL Guard 协同
---

# NL2SQL Prompt 工程技能

## System Prompt 构建
1. 从 Ontology Service 拉取当前会话已定义的 Class/Property/Metric。
2. 拼装 schema 描述：
   ```
   当前数据库有以下本体：
   - 客户(id, name, region)
   - 订单(id, customer_id, amount, order_date)
   指标：
   - 销售总额 = SUM(Order.amount)
   仅允许查询已定义的类与指标。仅生成 SELECT 语句。
   ```
3. 附加 Few-shot 示例（按数据源方言）。
4. ReAct：先列所需表/字段/条件，确认后生成 SQL。

## 约束
- 强制模型仅引用已定义本体。
- 禁止子查询嵌套危险表达式。
- 生成后必经 SQL Guard。

## 派生指标（Ratio/Percent）— CTE formula 支持
- 派生指标（占比/比率/百分比/比例/ratio/percent/share/pct）需用 formula 字段。
- formula 支持两种形式：
  - **简单形式**（单层聚合）：`SUM(x) / SUM(SUM(x)) OVER ()` 等窗口函数结构。
  - **复杂形式**（需 CTE）：使用 `WITH alias AS (SELECT ...) SELECT ... FROM alias` 结构，
    CTE 内部子查询不受窗口函数限制；最终 SELECT 必须是聚合函数
    （AVG / SUM / COUNT / STDDEV / VARIANCE / MEDIAN / PERCENTILE_CONT）且引用 CTE 别名，
    不再受窗口函数结构限制。
- 派生指标别名关键词：ratio / percent / share / pct / 占比 / 比率 / 百分比 / 完成率 等。

## 重试
- 校验失败带错误反馈重新生成，最多 `NL2SQL_MAX_RETRIES`(2) 次。
- 仍失败则返回错误，不执行任何 SQL。

## 方言
- MySQL / PostgreSQL：`date_trunc`、`LIMIT` 语法差异需在 schema 描述中标注方言。

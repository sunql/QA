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

## 重试
- 校验失败带错误反馈重新生成，最多 `NL2SQL_MAX_RETRIES`(2) 次。
- 仍失败则返回错误，不执行任何 SQL。

## 方言
- MySQL / PostgreSQL：`date_trunc`、`LIMIT` 语法差异需在 schema 描述中标注方言。

# 数据与 AI 治理规范

## 本体（Ontology）版本管理

- 每个 `ontology_class` / `ontology_property` / `ontology_metric` 记录带 `version` 与 `valid_from` / `valid_to` 时间范围。
- 修改本体定义（如 `source_table`、`formula`）时，旧版本保留供历史会话查询，新会话用新版本。
- 本体变更需人工审查（HITL），避免破坏既有 NL2SQL 语义。

## 指标（Metric）审查

- 指标公式 `formula` 必须人工审查其业务正确性后才能启用。
- 聚合函数 `agg_function` 与目标类 `target_class_id` 必须一致。
- 禁止在公式中嵌入危险表达式（如子查询）；公式仅限聚合 + 属性引用。

## 模型路由策略

- 模型配置（`llm_config`）的 `cost_threshold`、`weight`、`is_active` 调整需记录原因。
- 会话累计成本超过 `SESSION_BUDGET` 时强制降级到最便宜模型，不可绕过。
- 会话前 N 轮（`SESSION_AFFINITY_TURNS`）沿用同一模型，避免上下文割裂。
- 模型切换决策记录在 Token 流水中（`purpose` 字段标注）。

## Prompt 治理

- NL2SQL 的 System Prompt 模板变更需在 `changes/` 记录并对比效果。
- System Prompt 必须注入本体 schema，强制模型仅查询已定义的 Class/Metric。
- 禁止将完整数据库 schema 或敏感数据放入 Prompt。

## 数据隔离

- 后续多租户通过 `tenant_id` 隔离不同团队的本体与模型配置（Phase 5+）。
- 当前 MVP 单租户，预留字段。

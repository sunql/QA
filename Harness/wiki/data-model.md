# 数据模型

## 元数据库（PostgreSQL）

### llm_config
模型配置：`model_name`(唯一)、`provider`、`api_endpoint`、`api_key_encrypted`(Fernet 加密)、`cost_per_1k_input/output`、`max_input_tokens`、`weight`(0-100)、`cost_threshold`、`is_active`、时间戳。

### session_token_usage
会话 Token 流水：`session_id`、`model_config_id`(FK)、`model_name`、`prompt_tokens`、`completion_tokens`、`total_tokens`、`cost`、`request_time`、`purpose`。索引 `(session_id, request_time)` 与 `(model_config_id, request_time)`。

### 后续表（Phase 2-3）
- `ontology_class`：本体类，`source_table` 映射物理表，支持 `parent_class_id` 继承，`version` + `valid_from/to` 版本。
- `ontology_property`：属性，`data_type`、`is_primary_key`、`is_foreign_key`、`ref_class_id`、`source_column`。
- `ontology_metric`：指标，`formula`、`agg_function`、`target_class_id`、`dimension_defaults`(JSON)。
- `data_source`：数据源，`type`、`connection_url`、`encrypted_password`、`is_read_only`、`is_default`。
- `session_message`（Phase 5）：会话消息持久化，`session_id`、`role`(user/assistant)、`content`、`question`(assistant 回填)、`sql_generated`(assistant 回填)、时间戳。索引 `(session_id, created_time)`。用于 NL2SQL 前注入最近 5 轮上下文；服务端无记录时回退到客户端 `history` 字段。**Migration 0051** 新增字段：`routing_layer`（VARCHAR(10)，L1/L2/L3/L4）、`latency_ms`（INTEGER，毫秒）、`token_cost_usd`（FLOAT，美元）。

## 本体图（Neo4j）

```
(:Class {id, name, alias, description, sourceTable})
(:Property {id, name, alias, dataType, sourceColumn})
(:Metric {id, name, alias, formula, aggFunction})

(:Class)-[:HAS_PROPERTY]->(:Property)
(:Property)-[:REFERENCES]->(:Class)
(:Metric)-[:DERIVED_FROM]->(:Class)
```

## 向量库（Milvus）

- `ontology_embeddings`：类/属性/指标名称与描述的向量，用于语义检索。
- `query_embeddings`：历史查询向量（Phase 5），推荐相似问法。

## 跨库主键兼容

ORM 主键用 `BigInteger().with_variant(Integer, "sqlite")`：生产 PostgreSQL 用 BIGINT 自增，测试 SQLite 用 INTEGER 自增（SQLite 仅 INTEGER PRIMARY KEY 自增）。

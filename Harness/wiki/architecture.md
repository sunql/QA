# 系统架构

## 概述

智能问答系统：用户用自然语言提问，系统经多模型路由调用 LLM，将意图转为 SQL（NL2SQL），在可配置业务库上执行只读查询，并自动渲染 ECharts 图表。知识沉淀为本体（Ontology）。

## 分层

```
用户/前端 ── HTTP ──> API 层 (FastAPI)
                        │
            ┌───────────┼───────────┐
            ▼           ▼           ▼
       模型路由      本体服务     NL2SQL引擎
       (路由+计量)   (CRUD+图)    (Prompt+SQL)
            │           │           │
            ▼           ▼           ▼
       LLM 客户端    Neo4j+PG    SQL Guard + 数据源
       (OpenAI/Ollama)  Milvus    (动态引擎池)
                                    │
                                    ▼
                              业务数据库 (MySQL/PG)
```

## 核心组件

| 组件 | 位置 | 职责 |
|------|------|------|
| 模型路由 | `services/model_router_service.py` | 加权随机 + 成本阈值 + 会话亲和 + 预算降级 |
| Token 计量 | `services/token_usage_service.py` | 记录与聚合每次调用消耗 |
| LLM 客户端 | `infrastructure/llm/` | OpenAI/Azure/代理/Ollama 统一抽象 |
| Token 计数 | `infrastructure/token_counter/` | tiktoken + 启发式 |
| 本体服务 | `services/ontology_service.py`（Phase 2） | 类/属性/指标 CRUD + 图持久化 |
| NL2SQL | `services/nl2sql_service.py`（Phase 3） | ReAct 两阶段（计划→验证→SQL）+ REFINE 捷径 + SQL Guard |
| 图表渲染 | `services/chart_service.py`（Phase 4） | ECharts Option 生成 |
| 数据源 | `services/datasource_service.py`（Phase 3） | 动态引擎池 + 只读执行 |

## 数据流（对话出图，详见 `设计02.md` 15 步时序）

1. 前端 POST `/api/v1/chat`。
2. 意图识别（查询 vs 知识指令）。
3. 加载会话本体上下文。
4. 模型路由选模型（按 Token 估算成本）。
5. NL2SQL 生成 SQL（注入本体 schema），SQL Guard 校验。
6. 业务库执行只读查询。
7. 图表渲染生成 ECharts Option。
8. 记录 Token 消耗，返回回复 + 图表 + SQL。

## 部署

Docker Compose：PostgreSQL（元数据）+ Neo4j（本体图）+ Milvus（向量）+ MySQL（Demo 业务库）+ backend + frontend。详见 `docker/docker-compose.yml`。

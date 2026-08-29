# 智能问答系统（QA System）

基于 Python + FastAPI 的智能问答系统，核心能力：多模型动态路由与 Token 成本计量、Harness 本体（Ontology）管理、自然语言转 SQL（NL2SQL）、自动图表渲染、可配置多数据源查询。

## 架构概览

- **模型路由层**：加权随机选模型 + 成本阈值熔断 + 会话亲和性 + 累计成本降级，支持 OpenAI/Azure、国内 OpenAI 兼容代理（DeepSeek/通义/Qwen）、本地 Ollama。
- **知识引擎**：本体（类/属性/指标）持久化到 PostgreSQL + Neo4j，语义检索走 Milvus。
- **查询层**：基于本体 schema 的 NL2SQL（ReAct 模式），SQL Guard 仅允许只读 SELECT，多数据源动态切换。
- **可视化层**：后端生成 ECharts Option JSON，前端只渲染不处理数据。

## 目录结构

```
qa-system/
├── backend/      # FastAPI 后端
├── frontend/     # React + Vite + ECharts 前端
├── docker/       # Docker Compose 与 Dockerfile
├── Harness/      # 架构治理系统（规则/技能/Wiki/变更）
├── docs/         # 设计文档
└── CLAUDE.md     # 项目入口
```

## 快速开始

```bash
# 1. 复制环境变量
cp docker/.env.example docker/.env

# 2. 启动全部依赖与后端/前端
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d

# 3. 访问
#   前端:  http://localhost:5173
#   后端:  http://localhost:8000/docs
#   Neo4j: http://localhost:7474
```

## 本地开发

```bash
# 后端
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
uv run pytest --cov=app --cov-fail-under=80

# 前端
cd frontend
npm install
npm run dev
npm run test:coverage
```

## 开发阶段

| Phase | 内容 | 状态 | 覆盖率 |
|-------|------|------|--------|
| Phase 1 | 模型路由 + Token 计量 | ✅ 完成 | 89.85% |
| Phase 2 | 本体管理（Neo4j + Milvus） | ✅ 完成 | — |
| Phase 3 | 多数据源 + 单表 NL2SQL | ✅ 完成 | 82.98% |
| Phase 4 | 对话 + 图表渲染 | ✅ 完成 | 85.12% |
| Phase 5 | 多表 JOIN + 向量检索 + 打磨 | ✅ 完成 | 92.87% |

详见 `Harness/wiki/architecture.md` 与 `Harness/changes/`。

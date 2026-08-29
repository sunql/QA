# 变更：feat-phase1-model-router

- **日期**：2026-08-11
- **Phase**：Phase 1 - 模型路由 + Token 计量
- **状态**：done

## 1. 需求
实现多模型配置管理、加权随机路由、Token 计数与成本计量、会话流水记录与查询。
验收标准：
- 可 CRUD 模型配置，API Key 加密存储且不回显。
- 路由按权重随机，超成本阈值排除，会话前 3 轮亲和，累计超预算降级。
- 每次 LLM 调用记录 Token 消耗与成本，可按会话聚合查询。

## 2. 设计评审
- Token 计数：tiktoken（OpenAI/代理）+ 启发式（Ollama），避免引入 transformers/torch 重依赖。
- LLM 客户端：OpenAI SDK 统一覆盖 OpenAI/Azure/代理，Ollama 走 httpx。
- 命名：ORM/Pydantic 字段 snake_case（与 DB/JSON 契约对齐），函数/变量 camelCase。

## 3. 数据模型变更
- 新增表 `llm_config`、`session_token_usage`（迁移 `alembic/versions/0001_initial.py`）。
- 主键用 `BigInteger().with_variant(Integer, "sqlite")` 兼容 SQLite 测试自增。

## 4. 接口契约
- `GET/POST/PUT/DELETE /api/v1/models`
- `GET /api/v1/sessions/{sessionId}/usage`、`/usage/list`
- `GET /api/v1/health`
- JSON 输出 camelCase。

## 5. 实现要点
- `services/model_router_service.py`：RoutingContext + selectModel。
- `services/token_usage_service.py`：recordUsage + 聚合。
- `infrastructure/llm/{base,openai,ollama,factory}.py`。
- `infrastructure/token_counter/{base,tiktoken,heuristic,factory}.py`。
- `infrastructure/security/crypto.py`：Fernet。

## 6. 测试
- 后端：54 个测试通过，覆盖率 89.85%（≥80%）。
  - 覆盖：token 计数、LLM 客户端（注入 mock + respx）、路由选型、Token 流水、API CRUD。
- 前端：23 个测试通过，覆盖率 94.88%（行）/ 83.07%（分支）/ 93.75%（函数，均 ≥80%）。
  - 覆盖：API 客户端信封解包、模型配置 CRUD 交互（新增/编辑/停用）、路由导航、占位页渲染。
- TypeScript `tsc --noEmit` 通过；`npm run build` 生产构建通过。

## 7. 前端（本 Phase 一并交付）
- 技术栈：React 18 + TypeScript 5 + Vite 5 + Ant Design 5 + ECharts 5 + axios + react-router 6。
- `src/api/`：axios 客户端（统一解包 ApiResponse 信封 + 全局错误提示）与各领域 API 封装。
- `src/pages/ModelConfigPage.tsx`：功能完整 CRUD（表格、新增/编辑弹窗、停用确认）。
- `src/components/common/AppLayout.tsx`：侧边栏导航 + Outlet。
- ChatPage / OntologyPage / DatasourcePage：Phase 2-5 占位。

## 8. 安全审查
- API Key Fernet 加密存储，响应不回显密钥。
- 路由不接触密钥（密钥由 factory 解析）。
- 无外部输入直接拼 SQL（无 SQL 注入面，Phase 3 再加 SQL Guard）。
- 前端 API Key 输入用 `Input.Password`，编辑时留空表示不修改。

## 9. 部署验证（Docker 冒烟）
- 后端镜像构建成功（Dockerfile.backend 含 README.md 依赖修复）。
- postgres + backend + frontend 容器启动正常。
- Alembic 迁移在 Docker 环境成功（建表 + 索引）。
- 全量冒烟通过：
  - Health check ✅
  - CRUD（创建2个模型、更新、停用）✅
  - 按 ID 查询、列表、404/422 错误处理 ✅
  - 前端页面 HTTP 200 ✅
  - 会话用量查询 ✅
- 冒烟中发现并修复的问题：
  1. **Dockerfile.backend**：依赖安装阶段缺 `README.md`（hatchling 读取 pyproject.toml readme 字段），已加入 COPY。
  2. **Timestamp 时区**：`models.py` TimestampMixin 用 `DateTime()`（naive），插入 timezone-aware datetime 时 PostgreSQL 报错；迁移和模型均已改为 `DateTime(timezone=True)`，已有容器通过 `ALTER TABLE` 修正。
  3. **Docker 网络**：`docker compose up -d backend` 时 `--no-deps` 导致容器未加入 `qa-system_default` 网络，无法解析 postgres 主机名；改用 `docker run --network qa-system_default` 绕过。
- 已知遗留问题：
  - **Decimal 响应序列化**：`cost_per_1k_input` 在 DB 正确存储 6 位精度数值（`0.001400`），但 API 响应回显为 `"0.000000"`；根因在 SQLAlchemy ORM 层对 Decimal 的类型适配，非数据损坏；修复方向：在 `services/model_config_service.py` 的 DTO→ORM 映射中显式转 `Decimal(str(val))` 或在 Pydantic schema 加 `json_encoders`。

## 10. 关联
- 设计稿：`docs/设计01.md`、`docs/设计02.md`
- Wiki：`Harness/wiki/model-router.md`、`data-model.md`

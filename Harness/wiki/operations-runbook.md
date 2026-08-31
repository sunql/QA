# 运维手册

## 启动

```bash
cp docker/.env.example docker/.env  # 编辑密钥
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d
```

服务端口：
- 前端 5173、后端 8000(/docs)、PostgreSQL **5433**（容器 5432 映射到宿主 5433，避 OpenMetadata 占用；详见下方「迁移与端口映射」）、Neo4j 7474/7687、Milvus 19530、MySQL 3306。

## 本地开发

```bash
cd backend
uv sync --extra dev
uv run alembic upgrade head       # 迁移

# 必须带 docker/.env 的 SECRET_KEY！否则 datasource 解密失败（见下方「SECRET_KEY 必带」）
export SECRET_KEY="$(grep '^SECRET_KEY=' ../docker/.env | cut -d= -f2)"
uv run uvicorn app.main:app --reload --port 8000
uv run pytest --cov=app --cov-fail-under=80

cd frontend
npm install && npm run dev
npm run test:coverage
```

> **SECRET_KEY 必带（2026-08-18 踩坑）**：DB 里 `data_source.password_encrypted`（ZJTH-Oracle 等）是用
> `docker/.env` 的 `SECRET_KEY` 加密的。启动后端**必须**导出同一密钥，否则 datasource 读取时
> `Fernet` 解密抛 `ConfigError: SECRET_KEY 不是合法的 Fernet 密钥`，整个 NL2SQL 链路失败。
> 代码默认值 `development-insecure-key-change-me`（33 字符）不是合法 Fernet key，**不要依赖默认值**。
> `uv run uvicorn` 不会自动读取 `docker/.env`，需显式 export。

## 数据库迁移

```bash
uv run alembic upgrade head      # 应用
uv run alembic revision --autogenerate -m "desc"  # 生成新迁移
uv run alembic downgrade -1     # 回滚一步
```

## 日志

- 后端日志级别由 `LOG_LEVEL` 控制。
- Token 消耗、模型路由决策、SQL 执行审计均记录日志。

## 备份

- PostgreSQL 元数据：`pg_dump`。
- Neo4j：`neo4j-admin dump`。
- Milvus：MinIO 数据卷备份。

## 常见问题

| 现象 | 排查 |
|------|------|
| 后端启动 SECRET_KEY 报错 | 生成合法 Fernet key 填入 |
| 查询报「SECRET_KEY 不是合法的 Fernet 密钥」 | 重启时漏了 `SECRET_KEY` env。`export SECRET_KEY="$(grep '^SECRET_KEY=' ../docker/.env \| cut -d= -f2)"` 后重启（必须与加密 datasource 时同一密钥，不能用新生成的） |
| NL2SQL 生成错误 SQL | 检查本体是否定义、SQL Guard 日志 |
| 模型调用失败 | 检查 API Key、网络、provider 配置 |
| Ollama Token 为 0 | 旧版无 eval_count，标记近似 |
| SQLite 测试自增失败 | 主键须用 BigIntPk（with_variant Integer） |
| 后端 500 `UndefinedTableError: relation "xxx" does not exist` | DB 落后于 ORM（迁移未应用）。`cd backend && DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata alembic upgrade head` |
| 后端 500 `ConnectionRefusedError [Errno 61] Connection refused` | 连了 5432，但 Docker 把宿主 5433 映射到容器 5432（`docker-compose.yml` 注释「避开宿主 5432（被 openmetadata 占用）」）。本机连 PG 全部走 5433 |

## 迁移与端口映射（2026-08-30 踩坑）

### 端口：DB 永远在 5433，不是 5432

`docker-compose.yml` 里 postgres 服务的端口映射是 `5433:5432` —— 宿主 5433 → 容器 5432。原因：宿主 5432 被 OpenMetadata 占用。所以：

```bash
# 元数据库（开发）
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata

# 测试库
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test
```

连 5432 = ConnectionRefused；连 5433 = 正常。

### 迁移：`git pull` 后必须 alembic upgrade head

ORM 模型一旦新增表/列，必须有对应迁移文件。DB 与 ORM 不一致会出现两类故障：

1. **`alembic current` 落后于 head** —— 启动后端任何读取新表的请求 → `UndefinedTableError`，整 500。
2. **`SKIP_SCHEMA_CHECK=1` 静默放行** —— 启动校验被跳过，运行期才暴露，生产事故更难排。

**强制流程（每次 `git pull` 后 / 每次新增迁移文件后）：**

```bash
cd backend
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  alembic upgrade head
```

**CI / 本地启动前置**（建议加进 pre-commit 或 docker-compose 的 backend 启动入口）：

```bash
if [ "$(alembic current 2>/dev/null | head -1)" != "0025_audit_immutability (head)" ]; then
  alembic upgrade head
fi
```

**新增迁移文件 checklist**（开发自检）：

- [ ] `alembic upgrade head` 在干净 DB 上能跑通
- [ ] `alembic downgrade -1` 能回滚（验证对称性）
- [ ] 集成测试用真实 PG（`localhost:5433/qa_metadata_test`）跑迁移 — 见 `Harness/rules/测试规范.md`
- [ ] 更新本 runbook 的「迁移与端口映射」段（如端口或流程变更）

### 真实事故回放（2026-08-30）

| 时间 | 现象 | 根因 |
|---|---|---|
| T+0 | 前端 F12 报 `/kpi-catalog` 404、`/entity-mappings` 500 | 上一台 uvicorn 进程（PID 34319）启动时间早于 `kpi_catalog.router` 注册，是个**陈旧进程** |
| T+5min | 杀掉陈旧进程并重启 → 两个端点都 500 | 重启用了 `localhost:5432`，Docker 没在那开端口 |
| T+10min | 端口改回 5433 → `UndefinedTableError: relation "kpi_catalog" does not exist` | DB 停在 Alembic 0020；后续 0021-0025（entity_mapping、kpi_catalog、audit_log、history、immutability trigger）从未应用 |
| T+12min | `alembic upgrade head` | 5 条迁移跑完，两个端点恢复 200 |

**事后动作（已落地）**：

1. 写本节「迁移与端口映射」，避免下次重复踩。
2. 启动 backend 之前必跑 `alembic upgrade head`（见上文强制流程）。
3. 排查端点 500 时，先看 `uvicorn` 日志的首个 traceback，再换端点 —— **不要只换端点不查日志**。

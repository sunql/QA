# 运维手册

## 启动

```bash
cp docker/.env.example docker/.env  # 编辑密钥
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d
```

服务端口：
- 前端 5173、后端 8000(/docs)、PostgreSQL 5432、Neo4j 7474/7687、Milvus 19530、MySQL 3306。

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

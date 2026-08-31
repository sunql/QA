---
name: deploy-verify
description: Docker Compose 部署冒烟验证
---

# 部署验证技能

## 步骤
```bash
cp docker/.env.example docker/.env  # 填密钥
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d
docker compose -f docker/docker-compose.yml ps  # 全部 healthy
```

## 0. 数据库迁移同步（必查）

对**每个**运行环境（测试库 / live 库 / 生产库）校验迁移版本，不一致即阻塞：

```bash
# 每个环境都要跑，不允许只跑一个
DATABASE_URL=postgresql+asyncpg://<user>:<pw>@<host>:<port>/<db> alembic upgrade head
# 校验版本号一致且为当前 head revision（各环境必须相同）：
SELECT version_num FROM alembic_version;
```

> docker compose 部署容器启动会自动 `upgrade head`，但本地/非容器化 DB 不会。
> 版本不一致按 [开发流程规范](../rules/开发流程规范.md)「数据库迁移同步（部署门禁）」
> 处理后再继续冒烟。

## 冒烟检查
1. `curl http://localhost:8000/api/v1/health` -> `{"status":"ok",...}`
2. `POST /api/v1/models` 注册 OpenAI + Ollama 模型 -> 201
3. `POST /api/v1/datasource/register`（Phase 3）注册 demo MySQL -> 测试连接通过
4. `POST /api/v1/knowledge/define`（Phase 2）定义 `库存` 类
5. `POST /api/v1/chat`（Phase 4）提问 -> 返回回复 + SQL + 图表 + Token
6. `GET /api/v1/data-quality/rules`（若含数据质量特性）-> 200（非 5xx；表缺失会 500）

## 日志
```bash
docker compose logs -f backend
docker compose logs -f frontend
```

## 清理
```bash
docker compose -f docker/docker-compose.yml down -v  # 含卷
```

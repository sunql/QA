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

## 冒烟检查
1. `curl http://localhost:8000/api/v1/health` -> `{"status":"ok",...}`
2. `POST /api/v1/models` 注册 OpenAI + Ollama 模型 -> 201
3. `POST /api/v1/datasource/register`（Phase 3）注册 demo MySQL -> 测试连接通过
4. `POST /api/v1/knowledge/define`（Phase 2）定义 `库存` 类
5. `POST /api/v1/chat`（Phase 4）提问 -> 返回回复 + SQL + 图表 + Token

## 日志
```bash
docker compose logs -f backend
docker compose logs -f frontend
```

## 清理
```bash
docker compose -f docker/docker-compose.yml down -v  # 含卷
```

# 部署概览

QA System 是一套**多服务全栈应用**，由前端（React + Vite + Nginx）、后端（FastAPI）、本体图（Neo4j）、向量库（Milvus）、关系型元数据库（PostgreSQL）和演示业务库（MySQL，wms_demo）组成。

部署形态分两种：

| 形态 | 适用场景 | 关键差异 |
|---|---|---|
| **Docker Compose 单机全栈**（默认）| PoC、内网试点、单机 demo、≤50 并发 | 一条 `docker compose up -d` 拉起全部；端口直接暴露 |
| **分布式部署**（多节点 / K8s）| 生产、≥50 并发、需要高可用 | 各中间件拆主机部署；后端多副本 + 反向代理；向量库走 Raft / 外部服务 |

本文档先讲**Docker Compose 单机部署**（最快可用、最常用），再讲分布式部署的关键变更点。

---

## 部署后产物

```
┌────────────────────────────────────────────────────────────┐
│                       Nginx (5173 → 80)                    │
│                       静态资源 / 反代 /api 到后端           │
└──────────────────────────┬─────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────┐
│                  FastAPI 后端 (8000)                        │
│  - 模型路由  - Token 计量  - NL2SQL  - 本体管理            │
└──┬────────────┬────────────┬────────────┬─────────────┬───┘
   ▼            ▼            ▼            ▼             ▼
postgres     neo4j        milvus      embedding       business
(元数据)   (本体图)      (向量)        provider       datasource
                          +etcd/minio  (OpenAI/Ollama/ (MySQL/Oracle
                                       oMLX 等)        /Postgres)
```

---

## 一图速查部署清单

```bash
# 1. 准备服务器（Ubuntu 22.04+, 4C8G 起步）
sudo apt install -y docker.io docker-compose-v2

# 2. 拉取代码
git clone <repo-url> qa-system && cd qa-system

# 3. 配置环境变量
cp docker/.env.example docker/.env
python -c "from cryptography.fernet import Fernet; print('SECRET_KEY='+Fernet.generate_key().decode())" >> docker/.env

# 4. 启动全部容器
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d

# 5. 数据库迁移 + 种子数据
docker compose exec backend uv run alembic upgrade head
docker compose exec backend uv run python seed_models.py
docker compose exec backend uv run python seed_ontology.py

# 6. 反向代理 / HTTPS（Caddy/Nginx）

# 7. 完成，访问 https://your-domain/
```

> 详细的步骤、密钥生成、坑位、运维，参考同目录下其他文档。

---

## 文档目录

| 文档 | 内容 |
|---|---|
| [01-prerequisites.md](01-prerequisites.md) | 服务器要求、网络端口、依赖版本 |
| [02-environment.md](02-environment.md) | `.env` 配置详解、密钥生成、模型密钥 |
| [03-docker-compose.md](03-docker-compose.md) | Docker Compose 全栈启动、镜像变体 |
| [04-initialization.md](04-initialization.md) | Alembic 迁移、种子数据、回填向量 |
| [05-reverse-proxy-https.md](05-reverse-proxy-https.md) | Caddy / Nginx 反代、TLS、域名 |
| [06-distributed.md](06-distributed.md) | 多节点 / K8s 改造点 |
| [07-operations.md](07-operations.md) | 日常运维、故障排查、备份 |
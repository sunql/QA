# 3. Docker Compose 全栈启动

QA System 把全部中间件和后端打包在同一份 `docker/docker-compose.yml`，最简部署就是一条命令拉起全部。

---

## 3.1 服务清单

`docker-compose.yml` 包含 9 个服务：

| 服务 | 镜像 | 暴露端口 | 数据卷 | 作用 |
|---|---|---|---|---|
| `postgres` | `postgres:16-alpine` | 5432 | `pg_data` | 元数据 |
| `neo4j` | `neo4j:5.23-community` | 7474, 7687 | `neo4j_data` | 本体图 |
| `milvus-etcd` | `quay.io/coreos/etcd:v3.5.5` | - | `etcd_data` | Milvus 元数据 |
| `milvus-minio` | `minio/minio:RELEASE.2023-09` | 9000, 9001 | `minio_data` | Milvus 对象存储 |
| `milvus-standalone` | `milvusdb/milvus:v2.4.6` | 19530, 9091 | `milvus_data` | 向量库 |
| `mysql-demo` | `mysql:8.0` | 3306 | `mysql_demo_data` | 演示业务库 |
| `backend` | 本仓库 Dockerfile | 8000 | - | FastAPI |
| `frontend` | 本仓库 Dockerfile | 5173 | - | Vite + Nginx |
| `embedding` | （可选） | 8888 | - | bge-m3 等 |

> 端口说明：容器内 5432/7687/19530 都在 docker bridge 网络内**也可互相访问**；对外**只暴露 5173（前端）和 8000（后端）**。生产建议把这两个端口都关掉，前端用 Nginx 80/443。

---

## 3.2 启动步骤

```bash
cd qa-system

# 1. 准备 .env（详见 02-environment.md）
cp docker/.env.example docker/.env
# 编辑后填入 SECRET_KEY 和强密码

# 2. 拉镜像并后台启动
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d

# 3. 查看状态
docker compose -f docker/docker-compose.yml ps

# 4. 跟踪日志（首次启动看 30 秒）
docker compose -f docker/docker-compose.yml logs -f --tail=100
```

首次启动需要拉镜像，按网络情况 5–15 分钟。MinIO、Milvus、Neo4j 体积较大。

### 验证容器健康

```bash
# Postgres
docker compose exec postgres pg_isready -U qa_user -d qa_metadata

# Neo4j
docker compose exec neo4j cypher-shell -u neo4j -p $NEO4J_PASSWORD "RETURN 1"

# Milvus
curl -sf http://localhost:9091/healthz | jq

# MySQL demo
docker compose exec mysql-demo mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SHOW DATABASES"

# 后端
curl -sf http://localhost:8000/health

# 前端
curl -sf http://localhost:5173/ -o /dev/null -w '%{http_code}\n'
```

---

## 3.3 镜像变体

### 3.3.1 后端镜像

`docker/Dockerfile.backend` 是**多阶段**构建（builder + runtime），基于 `python:3.12-slim` + `uv` 静态二进制：

```dockerfile
# -------- builder --------
FROM python:3.12-slim AS builder
RUN apt-get install -y build-essential curl ca-certificates   # 含 gcc，供偶发无 wheel 的 C 扩展
RUN ... curl https://github.com/astral-sh/uv/releases/latest/download/uv-${UV_ARCH}-unknown-linux-gnu.tar.gz ...
RUN uv sync --frozen --no-dev || uv sync --no-dev   # 装依赖到 /app/.venv

# -------- runtime --------
FROM python:3.12-slim
RUN apt-get install -y curl ca-certificates   # 不再装 build-essential
COPY --from=builder /app /app
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn app.main:app --host 0.0.0.0 --port 8000"]
```

实测大小：

| 方案 | 镜像大小 |
|---|---|
| pip 装 uv（单阶段） | 2.01 GB / 408 MB |
| 静态二进制（单阶段） | 2.01 GB / 408 MB |
| **静态二进制 + 多阶段（当前）** | **1.5 GB / 287 MB**（-25%） |

- 启动时会先 `alembic upgrade head` 自动跑迁移，再起 uvicorn
- runtime 阶段没有 gcc / make / g++ 等编译工具链，攻击面更小
- 镜像构建自动识别 host 架构（`uname -m`）下载对应 uv 二进制（x86_64 / aarch64）

⚠️ **坑位**：
- **ghcr.io 不可达**：原版 Dockerfile 用了 `COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv`，build 时 docker daemon 必须从 ghcr.io 拉 manifest / blob。在受限网络（公司 VPN、海外代理、防火墙拦截 `pkg-containers.githubusercontent.com`）下拉不到，build 卡在 EOF。**当前方案绕过 ghcr.io**：用 curl 直接从 github release tarball 下载 uv 静态二进制。docker daemon 的 libnetwork 内部网络可以正常 TLS 到 github.com（host 上的 curl 可能被 SSL inspection 拦截，但 docker daemon 走的是 docker bridge，不会触发 host 的代理）。
- **host 上的 curl 与 docker daemon 内 curl 行为不一致**：本机测试时 `curl https://github.com` 报 `Connection reset by peer`，但 `docker run alpine curl https://github.com` 返回 200。这是 docker daemon 在 macOS 上走自己的网络栈，没经过 host 的 SSL inspection。
- **镜像内层网络也需要可达**：build 时 docker 还要拉 `python:3.12-slim`（docker.io）和 `deb.debian.org` 的 apt 源。如果 docker daemon 内连不到 docker.io 或 deb.debian.org，需要配 docker daemon 代理（`~/.docker/config.json` 的 `proxies`）。
- **重 build 慢**：后端代码变更后 `docker compose build backend` 平均 1–2 分钟（依赖缓存命中），首次 5–10 分钟。

### 3.3.2 前端镜像

`frontend/Dockerfile` 也是多阶段：

```dockerfile
FROM node:20-alpine AS builder
# Vite build
FROM nginx:1.25-alpine
# 把 dist/ 拷到 nginx html，附带 nginx.conf 路由 /api/* 到 backend
```

- 镜像大小约 50 MB
- 启动容器前端的 nginx 把 `/api/*` 反代到 `backend:8000`

### 3.3.3 镜像变体（arm64 vs amd64）

| 镜像 | amd64 | arm64 |
|---|---|---|
| `postgres:16-alpine` | ✅ | ✅ |
| `neo4j:5.23-community` | ✅ | ✅ |
| `milvusdb/milvus:v2.4.6` | ✅ | ✅ |
| `minio/minio:RELEASE.2023-09` | ✅ | ✅ |
| `mysql:8.0` | ✅ | ✅ |
| `mysql:8.0-debian` | ✅ | ❌ **manifest 不含 arm64** |

⚠️ **坑位**：Mac M1/M2/M3（arm64）如果按惯例写 `mysql:8.0-debian`，启动会报 `no matching manifest for linux/arm64/v8`。**改用 `mysql:8.0`**。这就是当前 `docker-compose.yml` 默认写法。

### 3.3.4 平台指定

如果服务器是 arm64（如 Graviton）但镜像没有 arm64 标签：

```yaml
mysql-demo:
  image: mysql:8.0
  platform: linux/arm64   # 显式指定
```

如果只有 amd64 镜像、宿主机是 arm64，需要开启 `binfmt` + qemu 模拟：

```bash
docker run --privileged --rm tonistiigi/binfmt --install all
```

性能下降 30–50%，不推荐生产。

---

## 3.4 关键配置开关

### 3.4.1 资源限制

`docker-compose.yml` 可以加 `deploy.resources`：

```yaml
backend:
  deploy:
    resources:
      limits:
        cpus: '2.0'
        memory: 2G
      reservations:
        cpus: '1.0'
        memory: 1G
```

Milvus 内存占用大，至少给 4 GB：

```yaml
milvus-standalone:
  deploy:
    resources:
      limits:
        memory: 6G
```

### 3.4.2 数据卷持久化

容器删除后数据靠 named volume 保留：

```bash
docker volume ls | grep qa
# 期望：pg_data / neo4j_data / milvus_data / minio_data / etcd_data / mysql_demo_data

# 备份整个卷（停机后）
docker run --rm -v qa_pg_data:/from -v /tmp/pg-backup:/to \
  alpine tar -czf /to/pg.tar.gz -C /from .
```

### 3.4.3 网络隔离

默认 `docker compose` 创建的网络是 bridge，所有服务互通。生产建议把数据库与前端解耦：

```yaml
networks:
  frontend:
  backend:
  data:
services:
  postgres:
    networks: [data]
  neo4j:
    networks: [data]
  backend:
    networks: [backend, data]
  frontend:
    networks: [frontend, backend]
```

这样前端只能访问 backend（再到 data），外部攻击不能直接打数据库。

---

## 3.5 启动顺序与依赖

`docker-compose.yml` 已用 `depends_on` + `condition: service_healthy` 标注：

```yaml
backend:
  depends_on:
    postgres:
      condition: service_healthy
    neo4j:
      condition: service_healthy
    milvus-standalone:
      condition: service_started
    mysql-demo:
      condition: service_healthy
```

具体的 `healthcheck` 配置：

```yaml
postgres:
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
    interval: 5s
    timeout: 5s
    retries: 10

neo4j:
  healthcheck:
    test: ["CMD-SHELL", "wget -q --spider http://localhost:7474 || exit 1"]
    interval: 10s
    timeout: 10s
    retries: 15
```

> ⚠️ 注意：Milvus standalone 没有自带 healthcheck，可以用 `curl -sf http://localhost:9091/healthz` 自己做，或干脆 `service_started`（不强等）。

---

## 3.6 常见启动失败

| 现象 | 原因 | 修法 |
|---|---|---|
| `bind: address already in use` 端口 5432/3306 | 宿主机已有 PG / MySQL | 停掉冲突进程，或改 `ports: 5433:5432` |
| `no matching manifest for linux/arm64/v8` | 镜像无 arm64 | 改用 `mysql:8.0` 或加 `platform: linux/amd64` |
| 数据库 container 反复重启 | 密码不一致（`.env` 改过但 volume 旧数据用了旧密码） | `docker compose down -v` 清卷重来 |
| `lightdb unhealthy` / Neo4j 卡在 `Starting` | 内存不足被 OOM killer | 加 swap：`fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile` |
| `Backend can't connect to postgres` | 后端启动比 PG 健康快，或 `DATABASE_URL` 用了 `postgres` 服务名但宿主机直跑 | 改 `localhost` 或 `depends_on` + healthcheck |

---

## 3.7 关闭 / 重启

```bash
# 优雅关闭
docker compose -f docker/docker-compose.yml down

# 关闭并删除卷（数据丢失，仅 PoC）
docker compose -f docker/docker-compose.yml down -v

# 重启单服务
docker compose -f docker/docker-compose.yml restart backend

# 滚动重启（不影响其他服务）
docker compose -f docker/docker-compose.yml up -d --no-deps --build backend
```

进入下一步：[04-initialization.md](04-initialization.md)。
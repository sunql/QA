# 6. 分布式部署

单机 Compose 适合 PoC / 试点。当并发 ≥ 50、要求高可用、或数据量爆炸时，必须做拆分。

---

## 6.1 何时升级

| 触发条件 | 拆什么 |
|---|---|
| 后端 CPU 持续 > 70% | 后端多副本（stateless） |
| Postgres 写入主库瓶颈 | 主从 + 读写分离 |
| Milvus 查询 > 200ms | Milvus 集群（Raft 3 节点） |
| Neo4j 单图节点 > 1 亿 | Neo4j causal cluster（3 节点） |
| Embedding 推理慢 | 独立 GPU 节点 |

---

## 6.2 整体拓扑

```
                          ┌─────────────────────────┐
                          │  LB / Nginx / Caddy     │
                          │  443 HSTS / 限流         │
                          └────────────┬────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
       ┌──────▼──────┐         ┌──────▼──────┐         ┌───────▼──────┐
       │ backend-1   │         │ backend-2   │         │ backend-N    │
       │ FastAPI     │         │ FastAPI     │         │ FastAPI      │
       └──────┬──────┘         └──────┬──────┘         └──────┬───────┘
              │                        │                        │
   ┌──────────┴──────────┐    ┌────────┴────────┐    ┌─────────┴─────────┐
   │                     │    │                 │    │                   │
   ▼                     ▼    ▼                 ▼    ▼                   ▼
Postgres          Redis Cluster    Qdrant / Milvus Cluster    Embedding
(read-write)      (session/cache)  (sharded / replica)        (GPU node)
   ▲
   │
Postgres
(read-replica)
```

---

## 6.3 Postgres 拆分

### 6.3.1 主从

Postgres 14+ 自带流复制：

```bash
# 主库 (postgresql.conf)
wal_level = replica
max_wal_senders = 5
listen_addresses = '*'

# 备库 (recovery.conf or postgresql.auto.conf)
primary_conninfo = 'host=postgres-primary port=5432 user=replicator password=...'
```

应用层读写分离：

```python
# 同步读主
WRITE_URL = "postgresql+asyncpg://qa_user:pass@pg-primary:5432/qa_metadata"
# 异步读从
READ_URL = "postgresql+asyncpg://qa_user:pass@pg-replica:5432/qa_metadata"

# SQLAlchemy 用两个 engine
WriteEngine = create_async_engine(WRITE_URL)
ReadEngine = create_async_engine(READ_URL)
```

### 6.3.2 托管服务

- AWS RDS / Aurora
- 阿里云 RDS for PostgreSQL
- 腾讯云 TencentDB for PostgreSQL

把这些替换 docker-compose 里的 `postgres` 服务，`DATABASE_URL` 指向云 RDS。

### 6.3.3 PgBouncer

后端多副本时连接数爆，每个 uvicorn worker 默认起 5+ 连接。**必须**用 PgBouncer 池化：

```yaml
pgbouncer:
  image: bitnami/pgbouncer:1.22
  environment:
    PGBOUNCER_DBUSER: qa_user
    PGBOUNCER_DBPASS: ...
    PGBOUNCER_DBNAME: qa_metadata
    PGBOUNCER_MAX_CLIENT_CONN: 1000
    PGBOUNCER_DEFAULT_POOL_SIZE: 20
  ports:
    - "6432:5432"
```

`DATABASE_URL` 改为 `postgresql+asyncpg://...@pgbouncer:6432/qa_metadata`。

---

## 6.4 Neo4j 集群

### 6.4.1 单实例 → 集群

```yaml
# 把单实例换成 3 节点
neo4j-core-1:
  image: neo4j:5.23-enterprise
  environment:
    NEO4J_dbms_mode: CORE
    NEO4J_causal_clustering_initial_discovery_members: neo4j-core-1:5000,neo4j-core-2:5000,neo4j-core-3:5000
    NEO4J_dbms_security_auth__enabled: "true"
    NEO4J_ACCEPT_LICENSE_AGREEMENT: "yes"
  volumes:
    - neo4j_core1_data:/data
```

⚠️ **注意**：Neo4j 集群版**要企业 License**，社区版只能单实例。如不能上 enterprise 就：

- Federation：按业务域拆图（订单图 / 库存图 / 客户图），各库独立
- 缓存：把高频读路径走 Redis

### 6.4.2 Aura（托管）

不用自建，Neo4j 官方托管：

- https://console.neo4j.io
- `NEO4J_URI=neo4j+s://xxxxx.databases.neo4j.io`

---

## 6.5 Milvus 集群

### 6.5.1 standalone → distributed

```yaml
# 至少 3 节点
milvus-proxy:
  image: milvusdb/milvus:v2.4.6
  command: ["milvus", "run", "proxy"]
  depends_on:
    - milvus-rootcoord
    - milvus-datacoord
    - milvus-querycoord

milvus-rootcoord: ...
milvus-datacoord: ...
milvus-querycoord: ...
milvus-datanode-1: ...
milvus-datanode-2: ...
milvus-datanode-3: ...
milvus-indexnode-1: ...
milvus-querynode-1: ...
milvus-querynode-2: ...
```

详细 docker-compose 见 [Milvus 官方 distributed playbook](https://milvus.io/docs/install_cluster-milvus.md)。

### 6.5.2 替代方案 Zilliz Cloud

- https://cloud.zilliz.com
- 免运维，全托管
- `MILVUS_URI=https://in01-xxxxx.api.gcp-us-west1.zillizcloud.com`
- `MILVUS_USER=xxx`
- `MILVUS_PASSWORD=xxx`

---

## 6.6 Embedding 独立化

embedding 推理通常是性能瓶颈，**必须**独立：

```yaml
embedding:
  image: your-bge-m3-image
  ports:
    - "8888:8080"
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            capabilities: [gpu]
            count: 1
```

后端 `BACKEND_EMBEDDING_ENDPOINT=http://embedding:8080/v1`。

GPU 节点用 Kubernetes 调度 + GPU time-slicing：

```yaml
# k8s deployment
resources:
  limits:
    nvidia.com/gpu: 1
```

---

## 6.7 后端多副本

后端是**无状态**的（session 状态在 Postgres / Redis），可以水平扩展：

```yaml
backend:
  deploy:
    replicas: 3
  environment:
    DATABASE_URL: ...
    REDIS_URL: redis://redis:6379/0   # 引入 Redis 做共享 session
```

### 6.7.1 Session 共享

单机模式 session 在 PG；多副本时把 hot path 缓存到 Redis：

```python
# app/services/session_cache.py
async def get_session(sid: str) -> Session:
    cached = await redis.get(f"session:{sid}")
    if cached:
        return Session.parse_raw(cached)
    s = await db.get(Session, sid)
    await redis.set(f"session:{sid}", s.json(), ex=300)
    return s
```

### 6.7.2 健康检查 / 滚动

```yaml
backend:
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
    interval: 10s
    timeout: 5s
    retries: 3
```

Nginx upstream：

```nginx
upstream backend_pool {
    server backend-1:8000 max_fails=3 fail_timeout=30s;
    server backend-2:8000 max_fails=3 fail_timeout=30s;
    server backend-3:8000 max_fails=3 fail_timeout=30s;
    keepalive 32;
}
```

---

## 6.8 Kubernetes 化

### 6.8.1 核心 workload

| K8s 资源 | 对应 |
|---|---|
| `Deployment` | backend |
| `Deployment` | frontend |
| `Deployment` | embedding |
| `StatefulSet` | postgres, neo4j, milvus（持久卷） |
| `Service` (ClusterIP) | backend, frontend |
| `Service` (LoadBalancer) | 一台 LB → Ingress → frontend/backend |
| `Ingress` | nginx-ingress + 证书 |
| `HorizontalPodAutoscaler` | backend pods (CPU > 70%) |

### 6.8.2 Helm chart 拆分要点

- `chart/qa-postgres` — StatefulSet + PVC + Service
- `chart/qa-neo4j` — StatefulSet + headless service
- `chart/qa-milvus` — 多 StatefulSet（rootcoord / datanode / querynode）
- `chart/qa-backend` — Deployment + HPA + ConfigMap（.env）
- `chart/qa-frontend` — Deployment + ConfigMap (nginx.conf)
- `chart/qa-embedding` — Deployment + GPU resource

### 6.8.3 镜像仓库

私有 Harbor / ACR / ECR：

```bash
# 推送
docker tag qa-backend:1.0.0 harbor.example.com/qa/qa-backend:1.0.0
docker push harbor.example.com/qa/qa-backend:1.0.0
```

### 6.8.4 ConfigMap / Secret

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: qa-backend-config
data:
  DATABASE_URL: postgresql+asyncpg://qa_user:$(PG_PASS)@qa-postgres:5432/qa_metadata
  NEO4J_URI: bolt://qa-neo4j:7687
  MILVUS_URI: http://qa-milvus-proxy:19530
  USE_ES: "true"
  SESSION_BUDGET: "10.0"
---
apiVersion: v1
kind: Secret
metadata:
  name: qa-backend-secret
type: Opaque
stringData:
  SECRET_KEY: KO9d4_ynyXQEHhg2_NB9QPdc9IG5ACnvrdUB9s4LYeM=
  POSTGRES_PASSWORD: xxxxx
  NEO4J_PASSWORD: xxxxx
```

---

## 6.9 监控 / 可观测

### Prometheus

```yaml
scrape_configs:
  - job_name: 'qa-backend'
    static_configs:
      - targets: ['backend:8000']
    metrics_path: /metrics

  - job_name: 'milvus'
    static_configs:
      - targets: ['milvus-standalone:9091']

  - job_name: 'neo4j'
    static_configs:
      - targets: ['neo4j:2004']
```

### Grafana

推荐的看板：
- 后端 QPS / P50 / P99 / 错误率
- LLM token 用量 / 成本
- Milvus 矢量数 / 索引大小 / QPS
- Postgres 连接数 / 慢查询 / 锁

### 告警

| 指标 | 阈值 |
|---|---|
| 后端 5xx 错误率 | > 1% for 5min |
| Postgres 连接数 | > 80% pool |
| Milvus 索引重建失败 | any |
| 磁盘剩余 | < 20% |
| 证书过期 | < 14 days |

---

## 6.10 升级路径建议

1. **第 1 阶段**：单机 Compose + 独立 MySQL（如果业务库要稳定）
2. **第 2 阶段**：数据库换托管 RDS / 阿里云
3. **第 3 阶段**：后端上 K8s，状态无关副本
4. **第 4 阶段**：向量库 / Neo4j 集群化
5. **第 5 阶段**：多 region，跨可用区灾备

按阶段推进，**不要一步到位**——分布式带来的复杂度（事务、缓存、数据一致性）远超单机。

下一步：[07-operations.md](07-operations.md)。
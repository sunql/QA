# 7. 日常运维与故障排查

部署完成后需要持续维护。本文档覆盖日志、监控、备份、常见故障。

---

## 7.1 日志

### 7.1.1 docker compose 日志

```bash
# 全部服务
docker compose -f docker/docker-compose.yml logs --tail=200 -f

# 单服务
docker compose -f docker/docker-compose.yml logs -f backend

# 时间范围
docker compose -f docker/docker-compose.yml logs --since 1h backend

# 过滤关键字
docker compose -f docker/docker-compose.yml logs backend | grep -i error
```

### 7.1.2 日志轮转

默认 docker json 日志会无限增长。加 `logging` 块：

```yaml
backend:
  logging:
    driver: json-file
    options:
      max-size: "50m"
      max-file: "10"
```

或者全局写到 `/etc/docker/daemon.json`：

```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "10"
  }
}
```

重启生效：`sudo systemctl restart docker`。

### 7.1.3 集中日志（可选）

- Loki + Promtail
- ELK（Filebeat → ES → Kibana）
- Datadog / 阿里云 SLS

---

## 7.2 监控

### 7.2.1 基础健康

放在 `cron` 里每 5 分钟一次：

```bash
#!/bin/bash
# /opt/qa-system/healthcheck.sh
set -e

curl -fsS http://localhost:8000/health > /dev/null || echo "BACKEND DOWN"
curl -fsS http://localhost:5173/ -o /dev/null || echo "FRONTEND DOWN"
docker compose -f /opt/qa-system/docker/docker-compose.yml ps | grep -q 'unhealthy' && echo "SERVICE UNHEALTHY"
```

```bash
chmod +x /opt/qa-system/healthcheck.sh
*/5 * * * * /opt/qa-system/healthcheck.sh | mail -s "QA System Alert" you@example.com
```

### 7.2.2 指标

后端 `/metrics` 输出 Prometheus 格式：

```python
# 已包含 prometheus-fastapi-instrumentator
# 暴露在 /metrics
```

需要部署 Prometheus + Grafana 才能可视化，详见 [06-distributed.md §6.9](06-distributed.md#69-监控--可观测)。

---

## 7.3 备份

### 7.3.1 Postgres

```bash
# 全量
docker compose exec postgres pg_dump -U qa_user -d qa_metadata -F c -f /tmp/qa.dump

# 拷贝到主机
docker compose exec postgres cat /tmp/qa.dump > /backup/qa-$(date +%Y%m%d).dump

# 恢复
docker compose exec -T postgres pg_restore -U qa_user -d qa_metadata --clean --if-exists < /backup/qa-20260818.dump
```

**自动备份**（cron 每天 3 点）：

```bash
# /etc/cron.d/qa-pg-backup
0 3 * * * root /usr/local/bin/qa-pg-backup.sh
```

`qa-pg-backup.sh` 保留 30 天 + 传到 S3：

```bash
#!/bin/bash
set -e
BACKUP_DIR=/backup/pg
DATE=$(date +%Y%m%d)
mkdir -p $BACKUP_DIR

docker compose -f /opt/qa-system/docker/docker-compose.yml exec -T postgres \
  pg_dump -U qa_user -d qa_metadata -F c > $BACKUP_DIR/qa-$DATE.dump

# 清理 30 天前
find $BACKUP_DIR -name 'qa-*.dump' -mtime +30 -delete

# S3（可选）
aws s3 cp $BACKUP_DIR/qa-$DATE.dump s3://qa-backups/pg/
```

### 7.3.2 Neo4j

```bash
# 在线备份（社区版无，必须停机）
docker compose stop backend  # 停后端避免写入
docker compose exec neo4j neo4j-admin dump --database=neo4j --to=/tmp/neo4j.dump
docker compose exec neo4j cat /tmp/neo4j.dump > /backup/neo4j-$(date +%Y%m%d).dump
docker compose start backend

# 恢复
docker compose exec neo4j neo4j-admin load --from=/tmp/neo4j.dump --database=neo4j --force
```

企业版（5.x）支持 `neo4j-admin backup` 在线备份。

### 7.3.3 Milvus

```bash
# 用 milvus_cli 导出（推荐）
docker compose exec milvus-standalone \
  milvus_cli --host localhost \
  collection list

# 导出全 collection
docker compose exec milvus-standalone \
  bash -c "for c in ontology_class ontology_property; do
    milvus_cli --host localhost collection export \$c /tmp/\$c.json
  done"
```

也可用 `milvus-backup`（官方工具）：

```yaml
milvus-backup:
  image: milvusdb/milvus-backup:latest
  volumes:
    - milvus_backup:/backup
  command: ["backup", "create", "-n", "scheduled"]
```

### 7.3.4 MySQL demo

```bash
docker compose exec mysql-demo \
  mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" wms_demo | gzip > /backup/mysql-$(date +%Y%m%d).sql.gz
```

### 7.3.5 备份验证

⚠️ **关键**：备份不等于可恢复。每月一次 fire drill：

```bash
# 在测试环境恢复最近一个备份
rsync -avz backup-server:/backup/pg/qa-20260818.dump /tmp/
docker compose -f docker-compose.test.yml up -d postgres
docker compose -f docker-compose.test.yml exec -T postgres pg_restore -U qa_user -d qa_metadata -c < /tmp/qa-20260818.dump
# 起 backend，造一个测试 SQL 走全链路
```

---

## 7.4 升级

### 7.4.1 应用代码

```bash
cd /opt/qa-system
git pull

# 重 build 后端
docker compose -f docker/docker-compose.yml build backend
docker compose -f docker/docker-compose.yml up -d --no-deps backend

# 前端同理
docker compose -f docker/docker-compose.yml build frontend
docker compose -f docker/docker-compose.yml up -d --no-deps frontend
```

### 7.4.2 数据库迁移

```bash
# 升级前一定先备份
docker compose exec postgres pg_dump -U qa_user -d qa_metadata > /backup/pre-migration.dump

# 升级
docker compose exec backend uv run alembic upgrade head

# 失败回滚（alembic 单步回退）
docker compose exec backend uv run alembic downgrade -1
```

⚠️ **重要原则**：
- **新增列**：写 nullable + default，旧代码不会挂
- **删除列**：先发新代码（不再读）→ 后续迁移单独删
- **重命名**：拆为「加新列 + 双写 + 迁读 + 删旧列」三步

### 7.4.3 Neo4j / Milvus 升级

```bash
# 升级镜像版本
docker compose -f docker/docker-compose.yml pull neo4j milvus-standalone
docker compose -f docker/docker-compose.yml up -d --no-deps neo4j

# 验证
docker compose exec neo4j cypher-shell -u neo4j -p $NEO4J_PASSWORD "RETURN 1"
```

⚠️ **Milvus 大版本**：v2.4 → v2.5 必须先导出全部数据，再装新版，再导入。直接换镜像起不来。

### 7.4.4 Agent Scheduler Worker

Agent 定时调度**不是 FastAPI 进程的一部分**，需要独立启动：

```bash
# 前台运行（开发调试）
docker compose exec backend \
  uv run python -m app.workers.agent_scheduler_worker

# 后台运行（systemd）
# /etc/systemd/system/qa-agent-scheduler.service
[Unit]
Description=QA System Agent Scheduler Worker
After=network.target

[Service]
ExecStart=/usr/local/bin/qa-agent-scheduler-start.sh
Restart=always
RestartSec=5

# qa-agent-scheduler-start.sh 内容：
#!/bin/bash
cd /opt/qa-system/backend
uv run python -m app.workers.agent_scheduler_worker
```

**验证**：
```bash
docker compose exec backend uv run python -c "
import asyncio
from app.workers.agent_scheduler_worker import AgentSchedulerWorker
w = AgentSchedulerWorker()
print('poll interval:', w._pollInterval)
"
```

⚠️ **Worker 崩溃后不会自动重启**，需配置 systemd/PM2 进程管理。

---

### 7.5.1 后端 500

```bash
# 1. 看实时日志
docker compose -f docker/docker-compose.yml logs -f --tail=100 backend

# 2. 常见：database 连接失败
docker compose exec backend uv run python -c "
from app.config import settings
import asyncpg, asyncio
async def test():
    c = await asyncpg.connect(settings.DATABASE_URL)
    print('OK')
    await c.close()
asyncio.run(test())
"

# 3. 常见：embedding provider 503
curl -fsS http://localhost:8000/api/v1/models | jq '.[] | select(.isActive==true)'
# 全部 inactive → 没人响应，去 UI 激活一个
```

### 7.5.1b Schema Drift：`UndefinedTableError` 端点 500

**根因**：代码新增了 ORM 模型，但 DB 未执行迁移（`alembic upgrade head` 未跑）。

**表现**：
```
psycopg2.errors.UndefinedTable: relation "agent_schedule" does not exist
```

**修复**：
```bash
# 在后端容器内执行迁移
docker compose exec backend uv run alembic upgrade head

# 验证
docker compose exec backend uv run alembic current
```

**紧急绕过**（新模型完全不用时，可临时跳过 drift 检查）：
```bash
# 在 .env 中加
SKIP_SCHEMA_CHECK=1
docker compose up -d backend
```
⚠️ 生产环境禁止长期使用，跳过检查后 DB 与代码的一致性无保证。

### 7.5.1c PG 端口 5432 vs 5433

**根因**：本机 Docker 将容器的 5432 映射到宿主机 5433（避免占用宿主机的 5432）。

**现象**：`DATABASE_URL` 用了 `localhost:5432` → 连接被拒绝。

**修复**：所有 `DATABASE_URL` / `TEST_DATABASE_URL` 一律使用 **5433**：
```bash
# 正确
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test
```

### 7.5.2 Neo4j 不可达（`GraphTraversalService` 返回空）

**根因**：Neo4j 进程崩溃或端口不可达。

**现象**：`POST /chat` 走 graph 推理路径时返回空结果，不报错。

**修复**：
```bash
# 验证
docker compose logs neo4j | grep -i error
docker compose exec neo4j cypher-shell -u neo4j -p $NEO4J_PASSWORD "RETURN 1"

# 重启
docker compose restart neo4j
```

**降级**：当前端点到 graph 推理时会自动 skip（不影响 NL2SQL 路径）。

### 7.5.3 Milvus 不可达（Document RAG 返回空）

**根因**：Milvus 容器 OOM 或版本不兼容。

**现象**：文档上传成功但语义搜索返回空。

**降级**：Milvus 不可达时 Document Service 返回空结果，不阻断对话。

### 7.5.4 `/ontology/search` 400

```bash
# 关键字 where_in_list 报错
docker compose logs backend | grep -i 'where in'
# 修复：检查请求参数含特殊字符需 URL 编码
```

### 7.5.3 模型停用后无法激活

```bash
# 检查 Fernet decryption
docker compose exec backend python -c "
from app.infrastructure.security.crypto import crypto
encrypted = '<your-encrypted-key>'
print(crypto.decrypt(encrypted))
"
# 抛错说明 SECRET_KEY 变了——所有历史密钥都解不出，需要重新录入
```

⚠️ **SECRET_KEY 失效**：重启后端必须带 docker/.env 的 SECRET_KEY（否则 datasource 解密抛错）；dev 默认 SECRET_KEY 非法；迁移种 key 必须用 NULL。

### 7.5.4 前端报 `Cannot read properties of undefined (reading 'toFixed')`

**根因**：Pydantic `to_camel` 把 `cost_per_1k_input` 序列化为 `costPer1KInput`（**K 大写**），前端用了 `costPer1kInput` 找不到字段。

**修复**：前端 i18n / 表格 / 表单 全部用 `costPer1KInput` / `costPer1KOutput`（K 大写）。

### 7.5.5 Alembic `Can't locate revision identified by '0016_xxx'`

**根因**：镜像的 alembic 目录只打包到 0001，仓库代码已生成 0016。

**修复**：

```bash
# 重建镜像
docker compose -f docker/docker-compose.yml build --no-cache backend
docker compose -f docker/docker-compose.yml up -d --no-deps backend
```

或者：

```bash
# 本地 venv 跑
cd backend
uv sync
uv run alembic upgrade head
```

### 7.5.6 Milvus embedding 重复

**症状**：向量库 `ontology_class` 集合里 id=5 出现两行。

**根因**：批量 delete-then-insert 在重负载下会残留。

**修复**：用 `--cleanup` 删集重建：

```bash
uv run python -m app.scripts.backfill_milvus --cleanup
```

不要逐条增量删。

### 7.5.7 NL2SQL 跨类属性丢失

**症状**：涉及「收货数量」的查询在 3 个类里都查不到。

**根因**：`validatePlan` 按选定类校验属性，全库存在 ≠ 属于该类。

**修复**：在 `property` 元数据补 `business_aliases`：

```python
property = {
  "name": "QTYUOM",
  "business_aliases": ["收货数量", "收货件数", "实收数量"]
}
```

### 7.5.8 docker compose 启动后端口冲突

```bash
# 查谁占用
sudo lsof -iTCP:5432 -sTCP:LISTEN -P

# 停掉
sudo systemctl stop postgresql
brew services stop mysql  # Mac
docker stop openmetadata_postgresql  # 其他容器
```

### 7.5.9 容器反复重启

```bash
# 看上一次日志
docker compose logs --tail=200 backend

# 内存不足（OOM killer）
free -h
dmesg | grep -i 'killed process'

# 解决：加 swap 或调小 deploy.resources.limits
```

### 7.5.10 证书过期

```bash
# Caddy 自带续期
sudo systemctl status caddy

# 手动续期
sudo caddy reload --config /etc/caddy/Caddyfile

# Nginx + certbot
sudo certbot renew
```

---

## 7.6 应急响应清单

| 现象 | 优先级 | 动作 |
|---|---|---|
| 后端完全挂 | P0 | 1. `docker compose restart backend` 2. 看日志 3. 拉老镜像回滚 |
| 数据库宕机 | P0 | 1. 看磁盘 2. `docker compose restart postgres` 3. 恢复最近 backup |
| 向量库宕机 | P1 | 1. **功能降级**：禁用 semantic search 2. 拉 backup 3. 用 `--cleanup` 重灌 |
| Embedding 服务挂 | P1 | 1. 切到备用 provider（UI 改） 2. 修原 provider |
| 证书过期 | P1 | 1. certbot 续期 2. 重载 nginx |
| 磁盘告警 | P1 | 1. 查 docker volume 2. 清理 / 扩容 |
| 性能慢 | P2 | 1. 看 Grafana 2. 找慢查询 / 推理瓶颈 |

---

## 7.7 容量规划

| 指标 | 当前 | 6 个月预期 | 12 个月预期 |
|---|---|---|---|
| Ontology classes | 50 | 200 | 500 |
| Ontology properties | 200 | 1500 | 5000 |
| Embedding 数量 | 250 | 1700 | 5500 |
| Postgres 表大小 | 50 MB | 500 MB | 2 GB |
| Milvus 集合 | 100 MB | 700 MB | 2 GB |
| 日活用户 | 5 | 30 | 100 |
| 日均 API 请求 | 100 | 2000 | 20000 |

按 12 个月预期：

- Postgres：单实例 16 GB 内存足够，迁移到托管 RDS
- Milvus：进入集群模式（3 datanode + 3 querynode）
- Neo4j：单实例 4 GB 内存仍可撑，监控 `Page Cache Hit Ratio`
- 后端：4 副本 + 8 GB 每副本
- Embedding：独立 GPU 节点（bge-m3 8GB 显存足够）

---

## 7.8 联系 / 升级策略

- 主版本（v1 → v2）：3 个月内保留 v1 灰度
- 数据迁移：必须可逆（迁移前全量备份）
- 回滚：每个变更保留「最后 3 个镜像版本」

---

部署文档结束。回到 [00-overview.md](00-overview.md)。
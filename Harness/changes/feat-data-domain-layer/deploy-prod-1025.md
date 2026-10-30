# 生产发布方案：feat-data-domain-layer（数据域 + 数据层 + 工具页同源化）

- **日期**：2026-10-25
- **目标**：192.168.205.82（CentOS 7，内网隔离，Docker Compose）
- **代码基线**：`7e852b88`（main 之后 7 个提交：后端 ca02b9b5 / 前端 47a31fd9+1cc9addb / 菜单 28d419a8 / docs 其余）
- **迁移**：0132（生产当前）→ 0133 → 0134（后端容器 CMD 自带 `alembic upgrade head`，docker ps 已实证）
- **操作分工**：以下所有生产服务器操作由你手工执行；交付物（镜像包、本文档）由开发机准备

## qa_compose：生产 compose 控制标准入口（后续所有发版命令照此用）

生产使用独立 compose 文件，所有容器控制命令统一封装（**每个新 shell 会话先定义一次**，
或写入 `/root/.bashrc` 永久生效）：

```bash
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}

# 任何操作前先校验配置合法性（改过 yml / env 后必跑）：
qa_compose config --quiet && echo COMPOSE_OK
```

- 路径基准：`/opt/qa-system/docker/`（`docker-compose.centos7.yml` + `.env`）
- `-p qa-system` 是 **frontend 镜像名 `qa-system_frontend` 的派生源**（project 名 + 下划线）；
  backend 镜像则是 compose 文件里的显式 tag `qa-backend:offline`——两个名字来源不同，tag 时别混
- 本文后续 `qa_compose stop backend`、`qa_compose run --rm backend alembic upgrade head`、
  `qa_compose up -d backend frontend`、`qa_compose logs backend` 全走这个入口
- **不要动 `/opt/qa-system/docker/.env`**（SECRET_KEY 是 Fernet 主密钥，换了 5 数据源密码 + 6 LLM key 永久不可解）

## 生产环境容器/镜像实测清单（2026-10-25 docker ps 实证，后续发版照此填名）

| 容器名 | 镜像（compose 引用名） | 端口（宿主机） | 本次动作 |
|---|---|---|---|
| `qa-backend` | **`qa-backend:offline`**（显式名，非派生名） | 8000 | **替换** |
| `qa-frontend` | **`qa-system_frontend`**（下划线，`-p qa-system` 派生） | 5173→80 | **替换** |
| `qa-postgres` | postgres:16-alpine | 127.0.0.1:5433→5432（仅本机） | 不动（只跑 SQL） |
| `qa-milvus` / `qa-milvus-etcd` / `qa-milvus-minio` | milvus v2.4.6 族 | 19530 / 9091（仅本机） | 不动 |
| `qa-neo4j` | neo4j:5.23-community | 7474 / 7687（仅本机） | 不动 |
| `qa-ollama` + `qa-ollama-https` | ollama/ollama + nginx:alpine | 8443 | 不动 |
| `qa-objects` | minio | 9000-9001（仅本机） | 不动 |

> PG 访问一律走 `docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" ...'`
> （凭据在 `/opt/qa-system/docker/.env` 注入的容器 env 里，不要打印、不要宿主机硬编码）。
> 库名以 `docker exec qa-postgres env | grep POSTGRES_DB` 为准（预期 `qa_metadata`）。
> 宿主机直连口 127.0.0.1:5433 仅本机可用。

## Phase 0 交付物（开发机已备好，sha256 已回填）

| 文件 | 内容 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1025-ddl.tar.gz` | `qa-system-backend:amd64-1025-ddl`（linux/amd64，已核验） | `56ed95895021961d962913bb6969d052ba7e346909868653ebce9830487818ba` | 276M |
| `/tmp/qa-frontend-amd64-1025-ddl.tar.gz` | `qa-system-frontend:amd64-1025-ddl`（linux/amd64，已核验，`VITE_API_BASE_URL=` 空同源相对路径） | `ade1cdf7ba2dc02816500d291d2593ba8bc452dafacfa5654e545d3ee2ff506c` | 26M |

> 两镜像均为 `--platform linux/amd64` 构建（不指定平台生产 load 后 `exec format error`）。
> frontend 显式空 `VITE_API_BASE_URL`，否则 bundle 烤入 localhost 绝对地址，非 localhost 访问全站 CORS 挂（1024 实踩）。
> 生产上传输后先 `shasum -a 256` 比对再 load，防中途损坏。

## Phase 1 生产预检（只读，不改动）

```bash
# ⓪ compose 配置合法性
qa_compose config --quiet && echo COMPOSE_OK

# ① 当前迁移版本（期望 0132）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'

# ② 0133 守卫预检（Harness/changes/feat-data-domain-layer/summary.md §9.1）
#    任意 count > 0 会阻断迁移，先发我结果再继续
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "
SELECT '\''null_header_bos'\'', code, name FROM business_object WHERE header_class_id IS NULL;"'
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "
SELECT '\''entity_mapping'\'', count(*) FROM entity_mapping WHERE entity_type IN
  (SELECT code FROM business_object WHERE header_class_id IS NULL)
UNION ALL SELECT '\''feature_definition'\'', count(*) FROM feature_definition WHERE entity_type IN
  (SELECT code FROM business_object WHERE header_class_id IS NULL)
UNION ALL SELECT '\''document_entity_relation'\'', count(*) FROM document_entity_relation WHERE entity_type IN
  (SELECT code FROM business_object WHERE header_class_id IS NULL)
UNION ALL SELECT '\''feature_rule'\'', count(*) FROM feature_rule WHERE data_object IN
  (SELECT code FROM business_object WHERE header_class_id IS NULL);"'

# ③ 磁盘余量（两个包 ~300M + load 后镜像层翻倍）
df -h /var/lib/docker
```

- ② 全 0 → 可直接升级；有引用 → 把输出发我，给补绑/清理 SQL 后再继续

## Phase 2 备份

```bash
# ① DB 逻辑备份（含 0132 全量，回滚兜底）
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > /root/backup/qa_metadata_pre_ddl_$(date +%m%d).sql

# ② 现役镜像打备份 tag（占住旧镜像，防 tag 被顶后 ID 悬空难回滚）
docker tag qa-backend:offline       qa-backend:pre-ddl-1025
docker tag qa-system_frontend       qa-system_frontend:pre-ddl-1025
```

## Phase 3 传输 + 加载 + tag（按实测清单打名，⚠️ 两边目标名不同）

```bash
# 开发机 → 生产传输（内网途径由你定，scp/U盘均可），传完先比对 sha256
# 生产上：
docker load -i qa-backend-amd64-1025-ddl.tar.gz
docker load -i qa-frontend-amd64-1025-ddl.tar.gz

# tag 到生产现役名（与 docker ps 实测一致）
docker tag qa-system-backend:amd64-1025-ddl   qa-backend:offline
docker tag qa-system-frontend:amd64-1025-ddl  qa-system_frontend

# 确认 compose 现在解析到新镜像（image id 应与刚 load 的一致）
docker images | grep -E 'qa-backend|qa-system_frontend'
qa_compose config | grep image
```

## Phase 4 迁移（显式跑，不靠容器 CMD——守卫 raise 时看得见、不会 crash-loop）

```bash
qa_compose config --quiet && echo COMPOSE_OK
qa_compose stop backend
qa_compose run --rm backend alembic upgrade head
# 预期输出 0133 -> 0134 两行 running；若 RuntimeError「0133 阻断」→ 回 Phase 1② 处置

# 确认
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'   # 期望 0134
```

## Phase 5 起容器 + seed 菜单

```bash
qa_compose up -d backend frontend
qa_compose logs backend --since 3m | grep -E "Application startup|Traceback"   # 期望 startup complete 无 Traceback

# 菜单 seed（item.dataDomains + item.adminTools 两行靠这个进来；upsert 幂等）
docker exec -e PYTHONPATH=/app -w /app qa-backend python scripts/seed_menu_config.py
# 期望：>50 rows upserted（生产行数可能略有差异）
```

## Phase 6 验收清单（逐项打勾）

```bash
# ① API 已挂载且鉴权正常（两个都应返回「请先登录」，不是 404）
curl -s http://localhost:8000/api/v1/data-domains | head -c 100
curl -s http://localhost:8000/api/v1/business-objects | head -c 100

# ② 前端 bundle 判据：记录这个 hash 发我（新 bundle 与 dev 的 index-DBE4xCWc 同源，hash 可能因路径差异不同，以能对上为准）
curl -s http://localhost:5173/ | grep -o '/assets/index-[^"]*\.js'
```

UI（登录后）：
- [ ] 业务配置 → **数据域**：三域种子行（PROCUREMENT/QUALITY/LOGISTICS），CRUD、启停开关
- [ ] 业务对象：新建/编辑必选头表类 + 主数据域；附域不含主域
- [ ] 数据源 → 行「更多」→ **数据层分布**：探测过的源出层分布，未探测的出引导文案
- [ ] 系统配置 → **工具管理**：页面可达；数据层下拉出现 ODS_DICT / ODS_BUSINESS 等 7 项（不再是 3 项）

## Phase 7 回滚（仅在验收失败时）

```bash
qa_compose stop backend frontend

# ① DB 回滚：用 Phase 2① 的 dump 恢复（优先）；alembic downgrade 0132 会删
#    data_domain/data_layer_rule 及挂域数据，不推荐
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < /root/backup/qa_metadata_pre_ddl_XXXX.sql

# ② 镜像回滚：备份 tag 打回现役名
docker tag qa-backend:pre-ddl-1025         qa-backend:offline
docker tag qa-system_frontend:pre-ddl-1025 qa-system_frontend
qa_compose up -d backend frontend
```

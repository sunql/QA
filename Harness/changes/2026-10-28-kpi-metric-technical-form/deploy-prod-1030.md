# 生产发布方案：KPI 指标技术形态 + feature 目录修复（1030）

- **日期**：2026-10-30
- **目标**：192.168.205.82（CentOS 7，内网隔离，Docker Compose）
- **代码基线**：main `5f6bef24`（KPI 指标技术形态全套 + feature 目录文案加固）
- **迁移**：生产当前版本以 Phase 1① 实测为准（预期 0134 或 0135）→ 0136 → 0137
- **操作分工**：生产服务器操作由你手工执行；交付物（镜像包、本文档）由开发机准备
- **与 1025 发版的差异**：本版多一段 **Phase 5 KPI 治理数据五连**（Linux 库尚未做过：
  回填 12 指标 / 关键词种子 / 数据源绑定 / 灰度开关）。所有脚本已烧进 backend 镜像，
  在生产容器内执行，无需另传文件。

## qa_compose：生产 compose 控制标准入口（每个新 shell 会话先定义一次）

```bash
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}

# 任何操作前先校验配置合法性（改过 yml / env 后必跑）：
qa_compose config --quiet && echo COMPOSE_OK
```

- `-p qa-system` 是 frontend 镜像名 `qa-system_frontend` 的派生源（project 名 + **下划线**）；
  backend 是 compose 里的显式 tag `qa-backend:offline` —— 两个名字来源不同，tag 时别混
- **不要动 `/opt/qa-system/docker/.env`**（SECRET_KEY 是 Fernet 主密钥）

## Phase 0 交付物（开发机准备，sha256 构建完成后回填）

| 文件 | 内容 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1030.tar.gz` | `qa-system-backend:amd64-1030`（linux/amd64，已核验：代码 sha `0ca4492f` ≡ git 树 `5f6bef24`） | `f86b13890b74ecb21d4e74bced4eda701c61966a0e4eb1e9e8098c4597128b71` | 218M |
| `/tmp/qa-frontend-amd64-1030.tar.gz` | `qa-system-frontend:amd64-1030`（linux/amd64，`VITE_API_BASE_URL=` 空同源相对路径；bundle `index-XQsSE0o6.js` 与开发机同 hash，5 处 localhost 均为正则库+i18n 占位无害上下文） | `ebb8e18be335d7427b665790a30b3e2c7e06a0147b66c3c88b9c98ed668b5e3b` | 27M |

> 两镜像均 `--platform linux/amd64` 构建（不指定平台 load 后 `exec format error`）。
> 传输后先 `sha256sum` 比对再 load，防中途损坏。
> 编排 Worker 本次**不含**（生产未启用灰度；将来启用时 `docker tag qa-backend:offline
> qa-system_agent-orchestration-worker:latest` + `--profile agent-orchestration up -d
> --no-build agent-orchestration-worker`，同源镜像运行时只差 command）。

## Phase 1 生产预检（只读，不改动）

```bash
# ⓪ compose 配置合法性
qa_compose config --quiet && echo COMPOSE_OK

# ① 当前迁移版本（预期 0134 或 0135；0134→0136/0137 与 0135→0137 都被 upgrade head 覆盖）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'

# ② 数据源清单（Phase 5③ 绑定要用 THBI Oracle 的真实 id，**别照抄 Mac 上的 1**）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT id, name, type FROM data_source ORDER BY id;"'

# ③ KPI 目录行数（预期 12；不是 12 先停下来发我）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT count(*) FROM kpi_catalog;"'

# ④ 磁盘余量（两个包 ~300M + load 后镜像层翻倍）
df -h /var/lib/docker
```

## Phase 2 备份

```bash
# ① DB 逻辑备份（回滚兜底）
mkdir -p /root/backup
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > /root/backup/qa_metadata_pre_1030_$(date +%m%d).sql

# ② 本波受影响表单独备份（Phase 5 直接动这三张）
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t ontology_metric -t ontology_metric_source -t kpi_catalog -t system_config' \
  > /root/backup/kpi_tables_pre_1030_$(date +%m%d).sql

# ③ 现役镜像打备份 tag（占住旧镜像防 tag 被顶后 ID 悬空）
docker tag qa-backend:offline    qa-backend:pre-1030
docker tag qa-system_frontend    qa-system_frontend:pre-1030
```

## Phase 3 传输 + 加载 + tag（⚠️ 两边目标名不同）

```bash
# 开发机 → 生产传输（scp/U盘均可），传完先比对 sha256，然后：
docker load -i qa-backend-amd64-1030.tar.gz
docker load -i qa-frontend-amd64-1030.tar.gz

# tag 到生产现役名（与 docker ps 实测一致）
docker tag qa-system-backend:amd64-1030   qa-backend:offline
docker tag qa-system-frontend:amd64-1030  qa-system_frontend

docker images | grep -E 'qa-backend|qa-system_frontend'
qa_compose config | grep image   # 确认解析到新镜像
```

## Phase 4 迁移（显式跑，不靠容器 CMD——报错看得见、不会 crash-loop）

```bash
qa_compose config --quiet && echo COMPOSE_OK
qa_compose stop backend
qa_compose run --rm backend alembic upgrade head
# 预期输出 0136_metric_structured / 0137_metric_source_unique 两行 running

docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'   # 期望 0137
```

## Phase 5 KPI 治理数据五连（容器内执行；脚本读容器 env 的 DATABASE_URL）

> ⚠️ **本段全部用 `docker exec`，要求 `qa-backend` 容器在跑**。Phase 4 结束时 backend
> 处于 stop 状态（`run --rm` 的一次性迁移容器跑完即删）⇒ 先 `qa_compose up -d backend`
> 并确认 `Application startup complete`，再执行本段。

```bash
# ① 回填：先预览再 apply（预期 created=12 updated=0 degraded=0 skipped=0；
#    脚本自带当日备份逻辑，并会提示 12 条未绑数据源——下一步解决）
docker exec -e PYTHONPATH=/app -w /app qa-backend sh -c \
  'python -m scripts.kpi_metric_backfill --database-url "$DATABASE_URL" --allow-prod-write'
docker exec -e PYTHONPATH=/app -w /app qa-backend sh -c \
  'python -m scripts.kpi_metric_backfill --database-url "$DATABASE_URL" --allow-prod-write --apply'

# ② 语义关键词种子：先 --dry-run 预览，再正式（预期 affected=12）
#    ⚠️ dry-run 也要带 --allow-prod-write：assertSafeTarget 对正式库一律要求该旗标
#    （kpi_metric_db_guard.py，标志检查先于 dry-run 分支）；dry-run 不触发备份。
#    正式跑触发的备份闸复用回填脚本的 _ensureProdBackup——回填时放入的 3 个
#    备份文件直接复用，无需再装 pg_dump。
docker exec -e PYTHONPATH=/app -w /app qa-backend sh -c \
  'python -m scripts.seed_kpi_semantic_index --database-url "$DATABASE_URL" --allow-prod-write --dry-run'
docker exec -e PYTHONPATH=/app -w /app qa-backend sh -c \
  'python -m scripts.seed_kpi_semantic_index --database-url "$DATABASE_URL" --allow-prod-write'

# ③ 数据源绑定：<THBI_ID> = Phase 1② 里 THBI Oracle 那行的 id（UPDATE 12 行）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "UPDATE kpi_catalog SET datasource_id=<THBI_ID>, updated_time=now() WHERE metric_id IS NOT NULL;"'

# ④ 灰度开关 = false（先关后开；key 是主键，幂等）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "INSERT INTO system_config (key, value, description) VALUES
   ('\''KPI_CONTRACT_INJECTION_ENABLED'\'', '\''false'\'',
    '\''KPI 契约注入总开关；先关后开：治理列对齐并在隔离测试库演练通过后，人工改回 true（2026-10-30 决策）'\'')
   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, description=EXCLUDED.description, updated_time=now();"'

# ⑤ 只读复跑（判据：RESULT failed=0 / passed=6 / skipped=3，S7.2 由 SKIP 转 PASS；
#    S3 12/12 mismatch 是预期——治理列对齐未做，契约块为空符合设计）
docker exec -e PYTHONPATH=/app -w /app qa-backend sh -c \
  'python -m scripts.kpi_metric_realdata --database-url "$DATABASE_URL" --allow-prod-write'
```

> 回填预览（不带 `--apply`）如有 FAIL/异常输出，停下来发我，别硬 apply。

## Phase 6 起容器

```bash
qa_compose up -d backend frontend
qa_compose logs backend --since 3m | grep -E "Application startup|Traceback"  # 期望 startup complete 无 Traceback

# 菜单 seed（幂等；item.profile / item.changePassword 若缺会导致新建用户 500）
docker exec -e PYTHONPATH=/app -w /app qa-backend python scripts/seed_menu_config.py
```

## Phase 7 验收清单（逐项打勾）

```bash
# ① 迁移版本
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'        # 0137

# ② KPI 数据计数（一行 SQL 出全部判据；预期 12 / 18 / 12 / 12 / 12）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "
SELECT (SELECT count(*) FROM ontology_metric)::text,
       (SELECT count(*) FROM ontology_metric_source)::text,
       (SELECT count(*) FROM kpi_catalog WHERE metric_id IS NOT NULL)::text,
       (SELECT count(*) FROM kpi_catalog WHERE datasource_id IS NOT NULL)::text,
       (SELECT count(*) FROM kpi_catalog WHERE semantic_keywords IS NOT NULL AND semantic_keywords <> '\''[\"\"]'\''::jsonb)::text;"'

# ③ 开关
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT value FROM system_config WHERE key='\''KPI_CONTRACT_INJECTION_ENABLED'\'';"'  # false

# ④ API 路由存活（预期 422=路由+校验正常，不是 404/连接拒绝）
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' -d '{}'

# ⑤ 前端 bundle（预期与开发机同源 hash index-XQsSE0o6.js；不同也能用，发我确认）
curl -s http://localhost:5173/ | grep -o '/assets/index-[^"]*\.js'
```

UI（登录后）：
- [ ] 提问「供应商准时交付率是多少」→ 预期走**特征拦截**返回预计算值（feature 目录新文案生效）；
      若仍报「聚合属性 SUPPLIER_OTD_3M 不属于选定的任何类」→ 记录发我（触发方案 B 立项观察项）
- [ ] 指标治理页可见 12 条指标的技术形态（metric_type / 分子分母）
- [ ] 常规问答冒烟一条（任意已知业务问题出数）

## Phase 8 回滚（仅在验收失败时）

```bash
qa_compose stop backend frontend

# ① DB 回滚：用 Phase 2 的 dump 恢复（优先）；alembic downgrade 不推荐（会删新表新列）
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < /root/backup/qa_metadata_pre_1030_XXXX.sql

# ② 镜像回滚：备份 tag 打回现役名
docker tag qa-backend:pre-1030         qa-backend:offline
docker tag qa-system_frontend:pre-1030 qa-system_frontend
qa_compose up -d backend frontend
```

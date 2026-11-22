# 生产部署 runbook：本体发现 G1–G5 + 文档 AI 上下文（2026-11-22）

> **开发机无法直连生产**（内网隔离、无 SSH 通道）⇒ 本文件 + 镜像包交付，
> **你在生产宿主机逐条执行**。
>
> 代码基线：`e4e73339`（`feat/ontology-evolution`；= PR #177 合并后的 main 树）。
> 前置：**PR #177 需先合入 main**（merge commit；闸门按「净新增 0」人工确认，同 #176）。

## 0. 本批改动概览

| 面 | 内容 |
|---|---|
| 后端 | G2 六个发布校验码；发现流水线（剖析/候选/提案/审核/发布/监测/评测）；关联本体；新 API（admin） |
| 前端 | 语义发布管理页；本体发现工作台（发现源/运行/评审）+ 评审关联本体 |
| 迁移 | **0152 → 0169**（其中 0153–0158 采购域、0160–0165 文档/询价、**0166–0169 本体发现**） |
| 数据 | 无既有表结构破坏性变更（加法为主；0153/0158 有数据回填与 journal 自删表） |
| 环境变量 | 无新增必填项；`ENABLE_SEMANTIC_RELEASE=true` 才会注册发布路由 |

## 1. 交付物（开发机已产出）

| 文件 | 镜像 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1122.tar.gz` | `qa-system-backend:amd64-1122`（linux/amd64） | `2fedf50d214a60271fa1997e8494a1690f60b6cf6f547e66375c20280aadc8f9` | 490M |
| `/tmp/qa-frontend-amd64-1122.tar.gz` | `qa-system-frontend:amd64-1122`（linux/amd64，`VITE_API_BASE_URL=` 空） | `b5fbcef547b0a03728c6def19f9e5cdc479769aed082fd44ebc75fd261f80caf` | 26M |

镜像标识（生产核对用）：

| 项 | backend | frontend |
|---|---|---|
| 解压后镜像体积 | 517 MB | 27.5 MB |
| 平台 | linux/amd64 | linux/amd64 |
| 本机冒烟 | `import app.main` ✅ | — |

> 构建命令（可复现）：`docker buildx build --platform linux/amd64 -f docker/Dockerfile.backend -t qa-system-backend:amd64-1122 --load .`；
> 前端追加 `--build-arg VITE_API_BASE_URL=`（空 ⇒ 同源相对路径，生产必须）。

## 2. 生产预检（只读，不改动）

```bash
# 1.1 compose 入口（唯一权威：从现役容器 label 读，不要 ls 猜）
docker inspect qa-frontend qa-backend \
  --format '{{.Name}} => {{index .Config.Labels "com.docker.compose.project.config_files"}}'
# 预期：/opt/qa-system/docker/docker-compose.centos7.yml

# 1.2 现役版本
docker inspect qa-backend --format '{{.Config.Image}}'; docker inspect qa-frontend --format '{{.Config.Image}}'
# 1.3 生产库迁移版本（容器内展开变量，避免占位符被 bash 吃）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version;"'
# 预期：0152（1119 批次已执行）。若是 0143 或其他，先记录，不继续。

# 1.4 磁盘与备份空间
df -h /opt /var/lib/docker | tail -2
```

**停点**：`alembic_version` 不是 0152 ⇒ 先停下来核对（可能与 1119 批次是否落地有关）。

## 3. 强制备份（有 DDL，必做）

```bash
TS=$(date +%Y%m%d-%H%M)
mkdir -p /opt/qa-system/backups/$TS
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > /opt/qa-system/backups/$TS/qa_metadata-before-0169.dump
ls -lh /opt/qa-system/backups/$TS/
```

## 4. 导入镜像

```bash
gunzip -c qa-backend-amd64-1122.tar.gz  | docker load
gunzip -c qa-frontend-amd64-1122.tar.gz | docker load
docker images | grep -E 'qa-system-(backend|frontend).*1122'
```

## 5. 切换（先后端，后前端）

```bash
cd /opt/qa-system/docker
# 4.1 后端（启动时 CMD 自动 alembic upgrade head ⇒ 0152 → 0169）
docker tag qa-system-backend:amd64-1122  qa-system-backend:prod-1122
docker compose -f docker-compose.centos7.yml --env-file .env up -d backend
docker logs -f qa-backend   # 看到 upgrade 到 0169 且 uvicorn 启动
# 4.2 迁移版本复核
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version;"'
# 预期：0169

# 4.3 前端
docker tag qa-system-frontend:amd64-1122 qa-system-frontend:prod-1122
docker compose -f docker-compose.centos7.yml --env-file .env up -d frontend
```

> ⚠️ 若 `docker compose` 用旧版 v1：不要使用 `!override` 语法的文件；生产入口是自包含的
> `docker-compose.centos7.yml`。

## 6. 菜单 seed（新增「发现评审」）

```bash
docker compose -f docker-compose.centos7.yml exec backend python -m scripts.seed_menu_config
# 幂等；预期输出 67 rows upserted（旧环境可能 66→67）
```

## 7. 冒烟

```bash
# 6.1 健康
curl -s http://127.0.0.1:8000/api/v1/health
# 6.2 新路由已注册（401/403 = 正常；404 = 未注册）
for p in runs candidates findings proposals evaluations; do
  curl -s -o /dev/null -w "$p=%{http_code} " http://127.0.0.1:8000/api/v1/ontology-discovery/$p; done; echo
# 6.3 发布路由（ENABLE_SEMANTIC_RELEASE=true 时）
curl -s -o /dev/null -w "releases=%{http_code}\n" http://127.0.0.1:8000/api/v1/semantic/releases
# 6.4 前端 bundle 含新页面
docker exec qa-frontend sh -c "grep -l '本体发现' /usr/share/nginx/html/assets/*.js | head -1"
# 6.5 浏览器：登录 → 业务配置 → 发现评审（三页签）；语义发布页
```

## 8. 回滚（★关键：旧镜像 CMD 会升级 head）

旧 backend 镜像 head = **0152**；库已到 0169 时，旧镜像启动即
`Can't locate revision identified by '0169'` **起不来**。回滚顺序：

```bash
# 7.1 用**新镜像**把库降回 0152（0153–0169 的 down 主要是 drop 加法表/列；
#     0158 有数据回填，务必确认 pg_dump 已完成）
docker run --rm --network <prod_network> -e DATABASE_URL=postgresql+asyncpg://... \
  qa-system-backend:amd64-1122 alembic downgrade 0152
# 7.2 换回旧 tag 重建
docker tag <old_backend_image> qa-system-backend:prod
docker compose -f docker-compose.centos7.yml --env-file .env up -d backend frontend
# 7.3 复核库版本回到 0152，健康接口 200
```

## 9. 验收判据

- 迁移 = 0169；健康 200；新路由非 404；前端三页签可用；
- 语义发布页可打开（admin）；
- 发现评审可选运行、可判定、可关联本体；
- `pg_dump` 产物在 `/opt/qa-system/backups/<TS>/`。

## 10. 未随本批上生产的（明确）

- `feat/ontology-evolution` 之外的分支工作（无）；
- G5 的业务签收数据（属于运营，不是代码）；
- 「采纳即自动写本体」：仅策略登记（`Harness/rules/本体发现与落地边界.md`），无代码。

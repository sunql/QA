# 生产发布方案：feat-agent-orchestration（通用 Agent 编排，默认关闭）

- **日期**：2026-10-28
- **目标**：192.168.205.82（CentOS 7，内网隔离，Docker Compose）
- **代码基线**：`b84d9b91`（main 已 fast-forward 到此；上一发版点 1025 的 `7e852b88` 之后新增编排 + data-domain-layer 文档等 58 个提交）
- **迁移**：0134（生产当前）→ 0135（纯增量：新建 5 张 `agent_orchestration_*` 表 + 索引，无守卫、无列变更、无数据回填）
- **操作分工**：所有生产服务器操作由你手工执行；交付物（镜像包、本文档）由开发机准备
- **零行为变化保证**：`ENABLE_AGENT_ORCHESTRATION` 不写进生产 `.env` ⇒ 新路由不注册（404）、Worker 不启动、菜单不 seed。功能启用是后续独立灰度流程（见 §灰度）。

## qa_compose：生产 compose 控制标准入口（沿用 1025 定义）

```bash
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK   # 改过 yml/env 后必跑
```

- **不要动 `/opt/qa-system/docker/.env`**（SECRET_KEY 是 Fernet 主密钥）。
- 本次**不要**向 `.env` 追加 `ENABLE_AGENT_ORCHESTRATION` —— 保持默认关闭。
- 生产 compose 文件是旧版（无 Worker 定义）也**无需更新**：Worker 服务带
  `profiles: [agent-orchestration]` 门控，且只有显式 `--profile` 才会拉起；
  本次不启用 Worker，生产 compose 文件可以不动。

## 生产容器/镜像（与 1025 一致，本次只动两个）

| 容器名 | 镜像（compose 引用名） | 本次动作 |
|---|---|---|
| `qa-backend` | `qa-backend:offline`（显式名） | **替换** |
| `qa-frontend` | `qa-system_frontend`（`-p qa-system` 派生名） | **替换** |
| 其余（postgres/milvus/neo4j/ollama/objects） | — | 不动 |

## Phase 0 交付物（开发机准备，sha256 构建后回填）

| 文件 | 内容 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1028-orch.tar.gz` | `qa-system-backend:amd64-1028-orch`（linux/amd64，已核验） | `ece3794fbe0c99636299cce4016dd4ff90b2d3456714a85dded37098077ee9a5` | 217M |
| `/tmp/qa-frontend-amd64-1028-orch.tar.gz` | `qa-system-frontend:amd64-1028-orch`（linux/amd64，已核验，`VITE_API_BASE_URL=` 空） | `aeb72d209fc0167fdec3b6c5d5f3a17811177419651ae1602a4a3445f8e598c4` | 26M |

> 两镜像 `--platform linux/amd64` 构建；frontend 显式空 `VITE_API_BASE_URL`（1024 实踩）。
> 传输后先 `shasum -a 256` 比对再 load。

## Phase 1 生产预检（只读）

```bash
# ⓪ compose 合法性
qa_compose config --quiet && echo COMPOSE_OK

# ① 当前迁移版本（期望 0134）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'

# ② 0135 目标表不存在（期望 0；非 0 说明环境与假设不符，先发我结果）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE '\''agent_orchestration%'\'';"'

# ③ 磁盘余量
df -h /var/lib/docker
```

## Phase 2 备份

```bash
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > /root/backup/qa_metadata_pre_orch_$(date +%m%d).sql

docker tag qa-backend:offline       qa-backend:pre-orch-1028
docker tag qa-system_frontend       qa-system_frontend:pre-orch-1028
```

## Phase 3 传输 + 加载 + tag（⚠️ 两边目标名不同，沿用 1025 实测名）

```bash
docker load -i qa-backend-amd64-1028-orch.tar.gz
docker load -i qa-frontend-amd64-1028-orch.tar.gz

docker tag qa-system-backend:amd64-1028-orch   qa-backend:offline
docker tag qa-system-frontend:amd64-1028-orch  qa-system_frontend

docker images | grep -E 'qa-backend|qa-system_frontend'
qa_compose config | grep image
```

## Phase 4 迁移（显式跑，不靠容器 CMD）

```bash
qa_compose config --quiet && echo COMPOSE_OK
qa_compose stop backend
qa_compose run --rm backend alembic upgrade head
# 预期输出 0134 -> 0135 一行 running

docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'   # 期望 0135
```

## Phase 5 起容器

```bash
qa_compose up -d backend frontend
qa_compose logs backend --since 3m | grep -E "Application startup|Traceback"   # 期望 startup complete 无 Traceback
```

> **不跑菜单 seed 的新增项**：编排菜单入口刻意不在默认 seed（灰度阶段 6 才播种）。
> 如果顺手跑了 `seed_menu_config.py` 也无妨（幂等，且不会带出编排菜单）。

## Phase 6 验收清单

```bash
# ① 关闭态验证：编排路由必须 404（路由未注册；401/403 都说明 flag 被打开了——发我）
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/api/v1/agent-orchestrations
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/api/v1/agent-runs

# ② Worker 未启动（期望空输出）
docker ps | grep agent-orchestration-worker

# ③ 前端 bundle hash（发我，与开发机比对）
curl -s http://localhost:5173/ | grep -o '/assets/index-[^"]*\.js'

# ④ 既有功能回归（期望都返回「请先登录」类文案，不是 404/500）
curl -s http://localhost:8000/api/v1/agents | head -c 100
curl -s http://localhost:8000/api/v1/business-objects | head -c 100
```

UI（登录后）：
- [ ] 登录正常，菜单无「Agent 编排 / Agent 任务」新项（关闭态预期）
- [ ] 对话、业务对象、数据源等原有页面正常
- [ ] `agent_orchestration_*` 5 张新表已建（迁移成功的自然结果，可在 DB 确认）

## Phase 7 回滚（仅在验收失败时）

```bash
qa_compose stop backend frontend

# ① DB 回滚：优先用 Phase 2 dump 恢复；0135 是纯新增表，
#    保留这些空表对旧镜像完全无害，也可以只回镜像不回 DB
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < /root/backup/qa_metadata_pre_orch_XXXX.sql

# ② 镜像回滚
docker tag qa-backend:pre-orch-1028         qa-backend:offline
docker tag qa-system_frontend:pre-orch-1028 qa-system_frontend
qa_compose up -d backend frontend
```

## 后续：功能启用（独立流程，需要审批）

生产镜像就位后，功能启用走灰度手册逐阶段执行：
`Harness/changes/feat-agent-orchestration/deploy-gray-rollout.md`
（阶段 3.5 PRE-ENABLE CHECKLIST → 阶段 4 打 flag + 起 Worker → 阶段 6 播种菜单）。
关键红线：
1. `.env` 加 `ENABLE_AGENT_ORCHESTRATION=true` 只影响 backend 路由暴露；
   Worker 用 `docker compose --profile agent-orchestration ... up -d --no-build` 显式拉起。
2. 起 Worker 前 Dockerfile/compose 已在仓库内，但**离线机没有 worker 镜像**——
   需按手册阶段 2 用本包镜像 tag（compose 派生名以目标机
   `docker compose --profile agent-orchestration config --images` 输出为准）。
3. 4 个 wiki 工具永远不翻 enabled（绑着 4 个在线 legacy Agent）。

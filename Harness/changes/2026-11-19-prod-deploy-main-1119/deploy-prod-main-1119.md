# 生产部署：main 一揽子（#154 401 清理 + #155 改名泰鸿智脑 + #156 Milvus ef 修复）

- **代码版本**：`main @ c959b77f`（= 生产现役基线 `86ff4a1f` 之后的全部已合 main 提交）
- **部署日期**：2026-11-19（开发机产出镜像与本文档，宿主机逐条执行）
- **本次动**：**前端 + 后端两个容器** + 数据库**加法迁移 4 个**（0149–0152）
- **不动**：postgres / neo4j / milvus / ollama 容器一律不动；不改 `/opt/qa-system/docker/.env`

---

## 0. 一句话说清这次改了什么

自生产现役基线（1116 热修，`86ff4a1f`，backend 镜像 `513b4287ff58`）以来，main 上合了 5 个 PR：

| PR | 内容 | 影响面 |
|---|---|---|
| #150 | P5 工作流前端 | 前端新页面（依赖后端 P5 表） |
| #151 | 本体五 Tab 分页 | 前端交互 |
| **#154** | **401 清理 + persist 写盘失败处理** | **前端 + 后端**（本次部署的两个 bug 修复之一） |
| #155 | 品牌改名「AI服务平台」→「泰鸿智脑」 | 前端 5 处（title/favicon/侧边栏/折叠缩写/登录页） |
| **#156** | **Milvus HNSW ef>=limit 修复** | **后端 4 文件**（本次部署的两个 bug 修复之一；线上每轮 chat 的 wiki 注入 property 召回静默失效的根因） |

**数据库**：`alembic upgrade head` 会把生产库从 **0148** 升到 **0152**，共 4 个**纯加法**迁移：

| 迁移 | 内容 | downgrade |
|---|---|---|
| 0149 | CREATE workflow_definition / workflow_instance | drop 两表 |
| 0150 | CREATE workflow_step / workflow_command | drop 两表 |
| 0151 | CREATE approval_task / workflow_outbox | drop 两表 |
| 0152 | 放宽 audit_log.action CHECK（新增 7 个工作流动作白名单） | 恢复旧 CHECK |

全部可逆、不加列、不写业务数据。**采购线（0154–0158）不在 main，不会上生产。**

**验收标准**：
1. 浏览器标签页/侧边栏 = **泰鸿智脑**（favicon T、折叠缩写 TH）；
2. `docker exec qa-backend` 内 `milvus_client.py` 含 `hnswSearchParams`，跑一轮带 wiki 注入的 chat，
   日志**零** `ef(64) should be larger than k` 类 MilvusException；
3. `alembic_version` = **0152**，backend 启动日志 `Application startup complete.` 且 schema drift 无 blocking；
4. #150/#151 的 P5 工作流页面、本体分页可打开。

---

## 1. 交付物（开发机产出，构建完成后回填校验和）

| 文件 | 镜像 | 平台 | sha256 | 大小 |
|---|---|---|---|---|
| `/tmp/qa-backend-amd64-1119-main.tar.gz` | `qa-system-backend:amd64-1119-main`（→ tag `qa-system_backend`） | linux/amd64 | `8ae7a41acfff6ee0c9187cc2dc07bab6d98a92a08211c10e9f06f08a2d76d91f` | 219M |
| `/tmp/qa-frontend-amd64-1119-main.tar.gz` | `qa-system-frontend:amd64-1119-main`（→ tag `qa-system_frontend`，`VITE_API_BASE_URL=` 空 ⇒ 同源相对路径） | linux/amd64 | `851c796c8c310087a4cc08a11e60839bf8a0dc6a8c2b62aa57528bf2198201ae` | 26M |

镜像 manifest list digest（开发机构建输出，生产核对用）：
- backend `sha256:cb7a9131e855ad4a7990f2a4b8ab48f2d6b7efac8d5dbee3161e6b8e9c8131a3`
- frontend `sha256:cd7cade4da217db554fada9aca034c1d10046c4d5a6be432ce5aaa1e6e3a592c`

镜像内容自证（开发机已实测）：backend 库内迁移最高 **0152**（无 0154–0158 采购迁移）、
`milvus_client.py` 含 `hnswSearchParams` ×4、`import app.infrastructure.milvus_client` 后 helper 可用；
frontend 探针 title_new/bundle_new/favicon_T = 1、旧名四项 = 0、css `index-WW9dT-wP.css` 与现役同源。

新前端 bundle：`assets/index-BVutQwOS.js`（css 仍是 `index-WW9dT-wP.css`，与现役逐字节同源）
旧（现役）前端 bundle：`index-rBu8Smfz.js`

> ⚠️ 本次**后端也要换** ⇒ 必须先做 **pg_dump 备份**（见 §3），与 1119 改名 runbook（纯前端、免备份）不同。

---

## 2. Phase 0 生产预检（只读）

```bash
# ⓪ compose 入口 —— 从现役容器 label 读出（唯一权威），不要靠 ls 猜
docker inspect qa-frontend qa-backend \
  --format '{{.Name}} => {{index .Config.Labels "com.docker.compose.project.config_files"}}'
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK
qa_compose config --images    # 确认 backend/frontend 解析出的镜像名（回填 §1 对照）

# ⚠️ 不要改 /opt/qa-system/docker/.env（SECRET_KEY 是 Fernet 主密钥）

# ① 现役容器与镜像（回滚锚点以实际输出为准）
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend'

# ② 现役前端 bundle（必须是 index-rBu8Smfz.js；不是 ⇒ 基线变了，停下发我）
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'

# ③ 现役库的 alembic 版本（预期 0148）
#    用户/库名从 postgres 容器自己的环境变量取（不显示密码；如报错先看：
#    docker exec qa-postgres sh -c 'env | grep ^POSTGRES | grep -v PASSWORD'）
docker ps --format '{{.Names}}' | grep -i postgres
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'
# 不是 0148 ⇒ 停下发我（说明有人跑过别的迁移，本方案迁移链对不上）

# ④ 健康检查
curl -s -o /dev/null -w "backend_health=%{http_code}\n" http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "frontend_health=%{http_code}\n" http://localhost:5173/

# ⑤ 磁盘余量（两个包共 ~250M + 新镜像 + 备份）
df -h /var/lib/docker
```

**停下条件**：② 的 bundle 不是 `index-rBu8Smfz.js`；③ 的 alembic 版本不是 `0148`；④ 任一非 200。

---

## 3. Phase 1 回滚锚点（镜像 tag + **pg_dump**）

```bash
# 镜像锚点
docker tag qa-system_backend  qa-system_backend:pre-1119-main
docker tag qa-system_frontend qa-system_frontend:pre-1119-main
docker images | grep -E 'qa-system_(backend|frontend)'

# 数据库锚点（本次有 DDL，必须备份）
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > /opt/qa-system/backups/pg_pre_1119_main_$(date +%Y%m%d_%H%M).sql
ls -lh /opt/qa-system/backups/ | tail -3
# 备份非空校验：tail -3 看文件大小，应是几百 KB～数百 MB；0 字节 ⇒ 重跑并检查上面报错
```

> 0149–0152 全是加法迁移 ⇒ 即使后续回滚到旧镜像，新表留在库里也只是
> `extra_table` warning（schema drift 的 `extra_table` 是 warning 级不 blocking，已核实
> `backend/app/infrastructure/schema_drift.py:116`）。
> 但旧镜像的 CMD `alembic upgrade head` 会在库=0152 时报「Can't locate revision」**起不来**
> ⇒ 回滚前必须先降级（见 §7）。

---

## 4. Phase 2 传输 + 校验 + 加载 + tag

开发机 → 生产（scp / U 盘均可），**传完先比对 sha256**（§1 表）。

```bash
docker load -i /tmp/qa-backend-amd64-1119-main.tar.gz
docker load -i /tmp/qa-frontend-amd64-1119-main.tar.gz
docker tag qa-system-backend:amd64-1119-main  qa-system_backend
docker tag qa-system-frontend:amd64-1119-main qa-system_frontend
qa_compose config | grep image    # 确认两个服务都解析到新 tag
```

---

## 5. Phase 3 切换（前端 + 后端，不碰数据容器）

```bash
# ① 先后端（会跑 alembic upgrade head → 0152，然后再起 uvicorn）
qa_compose up -d --no-build --no-deps --force-recreate backend

# 盯启动日志：迁移 4 条 + "Application startup complete." 必须出现
docker logs -f qa-backend 2>&1 | grep -E "0149|0150|0151|0152|Application startup complete|ERROR|blocking" | head -30

# ② 再前端
qa_compose up -d --no-build --no-deps --force-recreate frontend
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend'
```

> - `--no-build` 防止 compose 现场 build；`--no-deps` 防止顺带重启 postgres/neo4j/milvus；
>   `--force-recreate` 保证真的换镜像（仅 `up -d` 可能不重建）。
> - v1 compose：**不要加 `--remove-orphans`**。
> - 生产无 worker 容器 ⇒ P5 工作流的超时/补偿后台任务（workflow_worker /
>   agent_orchestration_worker）生产上**不会跑**——审批提交等 API 可用，超时自动转
>   TIMEOUT 的后台巡检不生效。本批不新增 worker 容器（如需要另立批次评估）。

---

## 6. Phase 4 验收探针

### 6.1 后端

```bash
# ① 启动 + 迁移
docker logs qa-backend --since 10m 2>&1 | grep -E "Application startup complete|INFO  \[alembic" | tail -10
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'      # 期望 0152
curl -s -o /dev/null -w "backend_health=%{http_code}\n" http://localhost:8000/api/v1/health   # 200

# ② ef 修复落地（代码探针）
docker exec qa-backend grep -c "hnswSearchParams" /app/app/infrastructure/milvus_client.py   # ≥4（定义+3调用点）
docker exec qa-backend grep -rc "hnswSearchParams" /app/app/infrastructure/milvus_search.py \
  /app/app/infrastructure/milvus_registry_v2.py                                              # 各 ≥1

# ③ ef 修复生效（端到端）：登录后在聊天页问一个会带 wiki 注入的问题
#    （property 召回 topK=10，修复前每轮必炸 ef(64)<k(80)），然后：
docker logs qa-backend --since 10m 2>&1 | grep -c "should be larger than k"    # 期望 0
docker logs qa-backend --since 10m 2>&1 | grep -c "65535"                      # 期望 0

# ④ P5 表已在
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"' \
  | grep -E "workflow_definition|workflow_instance|workflow_step|workflow_command|approval_task|workflow_outbox"   # 6 张
```

### 6.2 前端（代码探针，仿 1119 runbook 双向验证）

```bash
docker exec qa-frontend sh -c '
h=/usr/share/nginx/html
echo bundle=$(ls $h/assets/index-*.js | head -1 | xargs basename)
echo title_new=$(grep -c "泰鸿智脑" $h/index.html)
echo bundle_new=$(grep -c "泰鸿智脑" $h/assets/index-*.js)
echo title_old=$(grep -c "AI服务平台" $h/index.html)
echo en_old=$(grep -c "AI Service Platform" $h/assets/index-*.js)
echo login_old=$(grep -c "Sign in to qa-system" $h/assets/index-*.js)
echo favicon_T=$(grep -c "%3ET%3C/text%3E" $h/index.html)
echo favicon_Q=$(grep -c "%3EQ%3C/text%3E" $h/index.html)
echo resolver=$(grep -c "resolver 127.0.0.11" /etc/nginx/conf.d/default.conf)
'
# 期望：bundle 为 §1 回填的新名；title_new/bundle_new/favicon_T = 1；
#       title_old/en_old/login_old/favicon_Q = 0；resolver = 1
```

### 6.3 链路与浏览器端到端

```bash
curl -s -o /dev/null -w "api_via_frontend=%{http_code}\n" http://localhost:5173/api/v1/health  # 200
```

浏览器（**硬刷新 Ctrl+Shift+R**）：
1. 标签页标题 **泰鸿智脑**、favicon 字母 T；登录页「登录 · 泰鸿智脑」；侧边栏品牌、折叠缩写 **TH**；
2. 登录正常；本体页翻页正常（#151）；P5 工作流页面可打开（#150）；
3. 聊天页发一个带知识注入的问题，回答正常（#156 生效的最终验收）。

---

## 7. 回滚（后端回滚必须先降级 DB）

```bash
# ① 停新后端，用新镜像一次性跑 downgrade（新迁移有 down 路径，0149–0152 都是 drop/恢复 CHECK）
qa_compose stop backend
docker run --rm --network qa-system_default \
  -v /opt/qa-system/docker/.env:/app/.env:ro \
  qa-system-backend:amd64-1119-main \
  alembic downgrade 0148
# （网络名以 docker network ls 实测为准；alembic 需要能连库。容器内 DATABASE_URL
#   读 .env，与 qa_compose 同一文件。）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'    # 期望回到 0148

# ② 换回旧镜像
docker tag qa-system_backend:pre-1119-main  qa-system_backend
docker tag qa-system_frontend:pre-1119-main qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate backend frontend
```

> - 0149–0152 的 downgrade 会 drop 6 张 P5 表 ⇒ 部署窗口内产生的审批/工作流数据会丢
>   （部署前有 pg_dump 兜底，见 §3）。audit_log 的既有数据不受影响（0152 只动 CHECK）。
> - **前端可单独回滚**（不跑 §7 的 ①②，只换 frontend tag + recreate）。
> - 不想动库的替代方案：保持库=0152、只回滚前端（前端回滚无任何 DB 依赖）。

---

## 8. 本批明确没做的事

- ❌ 不带采购线（`feat/procurement-m0` 的 0154–0158 与采购代码）——用户已裁决采购线不发生产；
- ❌ 不新增 worker 容器（生产保持无 worker 拓扑）；
- ❌ 不改生产 `.env`、不动 6 个 ENABLE_* 开关（P5 端点随代码在，但开关默认关，行为不变）；
- ❌ 不动 postgres / neo4j / milvus / ollama 容器与数据；
- ❌ 不改菜单文案（沿用 1119 runbook：用户自行人工调整）。

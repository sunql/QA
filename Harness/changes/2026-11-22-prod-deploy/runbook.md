# 生产部署：本体发现 G1–G5 + 文档 AI 上下文（2026-11-22）

- **代码版本**：`feat/ontology-evolution @ e4e73339`（= PR #177 合并后的 main 树；部署前 #177 必须先合 main）
- **部署日期**：2026-11-22（开发机产出镜像与本文档，**宿主机逐条执行**；开发机不直连生产）
- **本次动**：**前端 + 后端两个容器** + 数据库迁移（**0152 → 0169**，共 16 个）
- **不动**：postgres / neo4j / milvus / ollama 容器；不改 `/opt/qa-system/docker/.env`

---

## 0. 一句话说清这次改了什么

生产现役基线 = **1119 批次（main `c959b77f`，库 `0152`）**。此后合入 main 的两批：

| PR | 内容 | 影响面 |
|---|---|---|
| **#176** | 文档用于 AI 上下文（P0–P3）+ WP-ONT-AI 融合契约 | 后端（文档上下文/守卫）+ 3 个文档迁移 |
| **#177** | **本体发现与持续演进 G1–G5**：语义发布 6 校验码、发现流水线（剖析/候选/提案/四眼审核/发布/监测/评测）、前台工作台与评审、关联本体 | 后端 + 前端 + 4 个发现迁移 |

**数据库**：`alembic upgrade head` 会把库从 **0152** 升到 **0169**，共 **16 个**迁移
（0159 不在链上：`0158 → 0160`）：

| 迁移 | 内容 | 可逆性 |
|---|---|---|
| 0153 | 校准采购域 5 个本体类 `source_table`（DWD_* → ERP 表词表） | 数据 UPDATE，down 有反向映射 |
| 0154 | CREATE `procurement_source_binding` / `procurement_property_binding` | drop 两表 |
| 0155 | CREATE `semantic_release_check` | drop 表 |
| 0156 | Manifest v2（`semantic_release.schema_version` 等） | 恢复 v1 约定 |
| 0157 | 域作用域版本号唯一索引 | drop 索引 |
| 0158 | 采购本体对象核对与补齐（含 journal 自删表） | down 回滚数据，journal 自删 |
| 0160 | 询价草稿表 + `business_command.payload_hash` | drop 表/列 |
| 0161 | 询价决策类型扩 `SEND`（放宽 CHECK） | 恢复旧 CHECK |
| 0162 | Copilot 会话壳 + 轮次映射 | drop 两表 |
| 0163 | 文档目录补原文/向量化元数据（加可空列） | 删列 |
| 0164 | 文档关联 entity_type 允许知识对象 + 去重唯一索引 | 恢复 FK/删索引 |
| 0165 | 文档「可用于 AI 上下文」人工开关 | 删列 |
| 0166 | CREATE 发现流水线六表（run/profile/candidate/finding/proposal/review） | drop 六表 |
| 0167 | CREATE `discovery_evaluation` | drop 表 |
| 0168 | 候选/问题补 `decision_note` | 删两列 |
| 0169 | 候选/问题补 `linked_entity_type/code` | 删四列 |

> 0153 / 0158 含**数据回填**，其余为建表 / 加可空列 / 建索引 / 放宽 CHECK。
> 有 DDL+数据回填 ⇒ **必须 pg_dump**（§3）。

**验收标准**：
1. `alembic_version` = **0169**，backend 启动日志 `Application startup complete.` 且无 blocking 漂移；
2. 前端 bundle = §1 回填值；「业务配置 → **发现评审**」三个页签可打开；「语义发布」页可打开；
3. `GET /api/v1/ontology-discovery/{runs,candidates,findings,proposals,evaluations}` 非 404（401/403 正常）；
4. 发现评审可选运行、可判定、可「关联本体」；发布页可看校验结论（含 CLS/REL/GRP/AGT/GRF/MET 码）。

---

## 1. 交付物（开发机已产出并自证）

| 文件 | 镜像（→ 生产引用名见 §4） | 平台 | sha256 | 大小 |
|---|---|---|---|---|
| `/tmp/qa-backend-amd64-1122.tar.gz` | `qa-system-backend:amd64-1122` | linux/amd64 | `2fedf50d214a60271fa1997e8494a1690f60b6cf6f547e66375c20280aadc8f9` | 490M |
| `/tmp/qa-frontend-amd64-1122.tar.gz` | `qa-system-frontend:amd64-1122`（`VITE_API_BASE_URL=` 空 ⇒ 同源） | linux/amd64 | `b5fbcef547b0a03728c6def19f9e5cdc479769aed082fd44ebc75fd261f80caf` | 26M |

镜像标识（生产核对用）：

| 项 | backend | frontend |
|---|---|---|
| image ID（`docker image inspect .Id`） | `sha256:b8652634ce1ea3cb0d9d4c655899abffc625d88c202e48317e090e39c065daa4` | `sha256:8c0e06c6998167d58b469592e614604cbb07ec3eba47db51542bf1d80d1d9b87` |
| 解压后体积 | 517 MB | 27.5 MB |
| 前端 bundle | — | **`assets/index-DPRRPLTb.js`**（css `index-WW9dT-wP.css`，与现役同源） |

镜像内容自证（开发机已实测）：

```bash
# backend：迁移最高 0169；关键新代码在
docker run --rm --platform linux/amd64 --entrypoint sh qa-system-backend:amd64-1122 -c '
  ls /app/alembic/versions | grep -E "^016[6-9]"          # 0166…0169 四个
  ls /app/app/services | grep -E "discovery_(candidate_service|monitor_service|proposal_service|evaluation_service|signoff)"
  grep -c normalizePolicyTables /app/app/services/discovery_snapshot_service.py   # 2
  grep -c publishProposal /app/app/services/semantic_release_service.py           # 1
  python -c "import app.main" && echo import-ok'
# frontend：bundle 含新页面文案
docker run --rm --platform linux/amd64 qa-system-frontend:amd64-1122 sh -c '
  ls /usr/share/nginx/html/assets/index-*.js
  grep -c "本体发现" /usr/share/nginx/html/assets/index-*.js
  grep -c "关联本体" /usr/share/nginx/html/assets/index-*.js'
```

---

## 2. Phase 0 生产预检（只读，不改动）

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
qa_compose config --images        # 回填 §4 的 tag 目标名（backend/frontend 各自实际引用名）

# ⚠️ 不要改 /opt/qa-system/docker/.env（SECRET_KEY 是 Fernet 主密钥）

# ① 现役容器与镜像（回滚锚点以实际输出为准）
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend'

# ② 现役前端 bundle（记录，回滚时对照）
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'

# ③ 现役库 alembic 版本（预期 0152；若为 0143 ⇒ 1119 批次未执行，停下发我）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'

# ④ 健康与磁盘
curl -s -o /dev/null -w "backend_health=%{http_code}\n" http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "frontend_health=%{http_code}\n" http://localhost:5173/
df -h /var/lib/docker
```

**停下条件**：③ 不是 **0152**；④ 任一非 200；② 记下来备用（不是固定值也继续，只要记录）。

---

## 3. Phase 1 回滚锚点（镜像 tag + **pg_dump**）

```bash
# ① 镜像锚点（名字以 §2 ⓪ 的 config --images 为准；示例为 1119 实测值）
docker tag <backend现役引用名>   <backend现役引用名>:pre-1122
docker tag <frontend现役引用名>  <frontend现役引用名>:pre-1122
docker images | grep -E 'pre-1122'

# ② 数据库锚点（本次有 DDL+数据回填，必须备份）
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > /opt/qa-system/backups/pg_pre_1122_$(date +%Y%m%d_%H%M).sql
ls -lh /opt/qa-system/backups/ | tail -3      # 0 字节 ⇒ 重跑并检查报错
```

> 旧 backend 镜像的 CMD 是 `alembic upgrade head && uvicorn`；库升到 0169 后旧镜像启动会
> `Can't locate revision identified by '0169'` **起不来** ⇒ 回滚必须先降级到预检版本（§7）。

---

## 4. Phase 2 传输 + 校验 + 加载 + tag

开发机 → 生产（scp / U 盘均可），**传完先比对 sha256**（§1 表）。

```bash
docker load -i qa-backend-amd64-1122.tar.gz
docker load -i qa-frontend-amd64-1122.tar.gz

# tag 成**生产 compose 实际引用的名字**（§2 ⓪/⑩ 的输出），例如：
#   1119 实测：backend = qa-backend:offline；frontend = qa-system_frontend
docker tag qa-system-backend:amd64-1122  <backend现役引用名>
docker tag qa-system-frontend:amd64-1122 <frontend现役引用名>
docker images | grep -E '<backend现役引用名>|<frontend现役引用名>'
# 两行 Image ID 应变为 §1 的 b8652634… / 8c0e06c6…
```

---

## 5. Phase 3 切换（先后端，再前端，不碰数据容器）

```bash
# ① 后端（CMD 会跑 alembic upgrade head：0152 → 0169，然后起 uvicorn）
qa_compose up -d --no-build --no-deps --force-recreate backend
docker logs -f qa-backend 2>&1 \
  | grep -E "Running upgrade|015[3-9]|016[0-9]|Application startup complete|ERROR|blocking" | head -60

# ② 迁移版本复核
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'      # 期望 0169

# ③ 菜单 seed（新增「发现评审」；幂等，预期 67 rows upserted）
qa_compose exec backend python -m scripts.seed_menu_config

# ④ 前端
qa_compose up -d --no-build --no-deps --force-recreate frontend
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend'
```

> - `--no-build` 防止现场 build；`--no-deps` 防止顺带重启 postgres/neo4j/milvus；
>   `--force-recreate` 保证真的换镜像。
> - v1 compose：**不要加 `--remove-orphans`**。
> - 生产无 worker 容器 ⇒ 发现运行的异步任务走 API 进程内 `BackgroundTasks`（单进程）；
>   本批不新增 worker 容器。

### 5.5 按需开启的开关（本次实测会用到）

生产 `.env` 默认可能没开这些；按业务需要补齐后 **recreate backend**（改 env 后 `restart` 无效）：

| 开关 | 打开后出现 | 影响面 |
|---|---|---|
| `ENABLE_SEMANTIC_RELEASE=true` | `/api/v1/semantic/releases`（语义发布页） | admin-only；关闭时整组 404 |
| `ENABLE_KNOWLEDGE_FACADE=true` | `/api/v1/procurement/agents/catalog`、知识查询 | 读侧登录可用；关闭时 404（Copilot 页会报 catalog 404） |
| `ENABLE_AGENT_ORCHESTRATION=true` | `/api/v1/procurement/copilot/*`、agent-runs | **Copilot 页面必需**；关闭时 404 |
| `ENABLE_WORKFLOW=true` | `/api/v1/workflow/*`、`/procurement/rfqs/*` | 仅在用 RFQ/审批时开 |

Copilot 首次启用还需种子（幂等，按顺序）：

```bash
qa_compose exec backend python -m scripts.seed_agent_tool_configs
qa_compose exec backend python -m scripts.seed_agents
qa_compose exec backend python -m scripts.seed_procurement_agent_profiles
qa_compose exec backend python -m scripts.seed_agent_orchestration_menu
```

> `mode=task` 的 Copilot run 需要 `agent-orchestration-worker`（生产默认无 worker ⇒ task 会停在 RUNNING；
> 流式 `mode=stream` 在 API 进程内可跑）。要开 worker 属**新增拓扑**，另批评估。

---

## 6. Phase 4 验收探针

### 6.1 后端

```bash
docker logs qa-backend --since 10m 2>&1 | grep -E "Application startup complete|INFO  \[alembic" | tail -10
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'                                    # 0169
curl -s -o /dev/null -w "backend_health=%{http_code}\n" http://localhost:8000/api/v1/health   # 200

# 新路由已注册（401/403 = 正常；404 = 未注册）
for p in runs candidates findings proposals evaluations; do
  curl -s -o /dev/null -w "$p=%{http_code} " http://localhost:8000/api/v1/ontology-discovery/$p
done; echo

# 语义发布路由（ENABLE_SEMANTIC_RELEASE=true 时）
curl -s -o /dev/null -w "releases=%{http_code}\n" http://localhost:8000/api/v1/semantic/releases

# 新表已建
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"' \
  | grep -E "discovery_(run|profile|candidate|finding|proposal|review|evaluation)"   # 7 张
```

### 6.2 前端（代码探针）

```bash
docker exec qa-frontend sh -c '
h=/usr/share/nginx/html
echo bundle=$(ls $h/assets/index-*.js | head -1 | xargs basename)
echo discovery_new=$(grep -c "本体发现" $h/assets/index-*.js)
echo review_new=$(grep -c "关联本体" $h/assets/index-*.js)
echo release_new=$(grep -c "语义发布" $h/assets/index-*.js)
echo css=$(ls $h/assets/*.css | head -1 | xargs basename)
echo resolver=$(grep -c "resolver 127.0.0.11" /etc/nginx/conf.d/default.conf)'
# 期望：bundle = index-DPRRPLTb.js；discovery_new/review_new/release_new ≥1；resolver=1
```

### 6.3 浏览器端到端（硬刷新 Ctrl+Shift+R）

1. 登录 → 侧边栏「业务配置」下出现 **发现评审**；
2. 「发现评审」三个页签：发现源 / 运行 / 评审（可登记源、采快照、触发运行、判定、关联本体）；
3. 「语义发布」页：列表 / 详情 / 校验结论（severity × result）/ 激活 / 回滚；
4. 聊天页正常回答（文档 AI 上下文为加法特性，不改既有链路）。

---

## 7. 回滚（后端回滚必须先降级 DB）

```bash
# ① 预检版本记录为 PRE_VER（0152；若 1119 未执行则 0143）
qa_compose stop backend

# ② 用**新镜像**一次性降级到预检版本（需要能连库；挂 .env 与 qa_compose 同文件）
docker run --rm --network <qa-system 网络名> \
  -v /opt/qa-system/docker/.env:/app/.env:ro \
  qa-system-backend:amd64-1122 \
  alembic downgrade <PRE_VER>
# 网络名：docker network ls 实测（1119 为 qa-system_default）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'                # 应回到 PRE_VER

# ③ 换回旧镜像
docker tag <backend现役引用名>:pre-1122  <backend现役引用名>
docker tag <frontend现役引用名>:pre-1122 <frontend现役引用名>
qa_compose up -d --no-build --no-deps --force-recreate backend frontend
```

> - downgrade 会 drop 本批新建的表/列并反向回填 0153/0158 的数据 ⇒ 部署窗口内产生的
>   发现/发布数据会丢（部署前 pg_dump 兜底）。
> - **前端可单独回滚**（不动库，只换 frontend tag + recreate）。
> - 不想动库的替代：保持库=0169、只回滚前端（前端无 DB 依赖）。

---

## 8. 本批明确没做的事

- ❌ 不新增 worker 容器（生产保持无 worker 拓扑；发现运行走 API 进程内后台任务）；
- ❌ 不改生产 `.env`、不动 ENABLE_* 开关（`ENABLE_SEMANTIC_RELEASE` 未开时发布路由 404，属预期）；
- ❌ 不动 postgres / neo4j / milvus / ollama 容器与数据；
- ❌ 不做「采纳即自动写本体」（仅策略登记：`Harness/rules/本体发现与落地边界.md`）；
- ❌ 不带任何未合 main 的分支改动（部署基线 = #177 合并后的 main 树）。

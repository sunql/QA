---
created: 2026-11-27
updated: 2026-11-27
---

# 生产发版方案：催货单闭环（1127-chase01）

> 沿用 `Harness/changes/2026-11-26-release-1126/runbook.md` 的 Phase 0–8 结构。
> 固定值（容器名 / 镜像引用名 / 网络名）已按实测预填，**没有占位符**。

## 0. 一句话说清这次改了什么

**催货单闭环**：采购 Copilot · 到货跟踪里对**未到货**的订单行选行 → **按供应商**创建催货单
（一个供应商一张）→ 校验 → 审批 → 发送（经命令端口，outbox/决策留痕）。本期 `SENT` 即终态
（供应商答复回流、自动升级是二期）。

### 0.1 PR / commit 表

| PR | commit | 内容 |
|---|---|---|
| #187 | `8f99a5cd` | 后端：迁移 0172 三张表 + 状态机 + 命令端口 `sendChase` + 服务层 + API + 治理守卫登记 |
| #187 | `e7df635c` | 前端：Copilot 到货跟踪卡行级勾选 + 「按供应商创建催货单」+ 催货单管理页 + i18n |
| #187 | `dad443c7` | 三张催货表入册采购范围裁决目录（准则 H，CI 抓到的真净新增失败） |

**PR #187 已合入 main**（merge commit `946e7b3e`，2026-11-27 08:26）。
镜像构建源 `dad443c7` 与合并后 main 的**运行时代码逐字一致**
（`git diff dad443c7 946e7b3e -- backend/app backend/alembic frontend/src` 为空；
两者仅差本发版方案文档）。**本批不含 main 上未合入的任何分支改动。**

### 0.2 迁移表（`0171 → 0172`）

| revision | 内容 | 数据回填 | 可逆性 |
|---|---|---|---|
| `0172` | 新建 3 张表：`procurement_chase_draft` / `procurement_chase_line` / `procurement_chase_decision` | **无**（纯建表，零数据回填） | `downgrade 0171` ⇒ **drop 三张表**（不丢任何既有业务数据） |

**回滚边界（PRE_VER）= `0171`**。

⚠️ 0172 的 `downgrade` 会 **drop 三张表** ⇒ 回滚后**催货单数据全丢**（新建的单据本来也没业务价值，
但记录在此以免误判「回滚无损」）。

### 0.3 验收标准

- 后端：`alembic_version = 0172`；三张催货表存在；`GET /api/v1/procurement/chase/drafts` 返回 401/403（路由已注册）
- 前端：bundle 里能 grep 到催货文案；到货跟踪卡能勾选未到货行并开单
- 端到端：勾选未到货行 → 创建催货单（1 供应商 1 张）→ 校验 → 审批 → 发送 ⇒ 状态 `SENT`，
  决策时间线含 `CHASE-SIM-*` 外部引用

## 1. 交付物（开发机产出并自证）

> 构建源：`feat/delivery-chase-closed-loop @ dad443c7`
> 平台 `linux/amd64`。

### 1.1 镜像

| 镜像 tag | 平台 | Image ID | 说明 |
|---|---|---|---|
| `qa-system-backend:amd64-1127-chase01` | linux/amd64 | `5a0ab312e6fa` | backend + agent-orchestration-worker 同镜像 |
| `qa-system-frontend:amd64-1127-chase01` | linux/amd64 | `17fd38d9a64c` | nginx + 静态 bundle ``index-D4h2y-ri.js`` |

### 1.2 传输包

| 文件 | 大小 | sha256 |
|---|---|---|
| `qa-backend-amd64-1127-chase01.tar.gz` | 491M | `893cc2246ceb3c19f7a0d20ae944eb6f5201cd4468e5487487959127ad507f0d` |
| `qa-frontend-amd64-1127-chase01.tar.gz` | 26M | `e23fd220425759ce5c029cc884daf21595be378ed2bcda9668aa07eb8af967ba` |

### 1.3 镜像内容自证（构建后实测）

```bash
# backend：迁移文件 + 新代码 + 命令端口
docker run --rm --platform linux/amd64 --entrypoint sh qa-system-backend:amd64-1127-chase01 -c \
  'ls /app/alembic/versions/ | grep 0172;
   grep -c "procurement_chase" /app/app/main.py;
   grep -c "sendChase" /app/app/services/procurement_command_port.py;
   python -c "import app.services.procurement_chase_service as m; print(\"chase_service_ok\", bool(m.ProcurementChaseService))"'

# frontend：bundle 文案（中文 key 的字面量会进 bundle）
docker run --rm --platform linux/amd64 --entrypoint sh qa-system-frontend:amd64-1127-chase01 -c \
  'ls /usr/share/nginx/html/assets/index-*.js'
for t in "按供应商创建催货单" "催货单" "已隐藏"; do
  n=$(docker run --rm --platform linux/amd64 --entrypoint sh qa-system-frontend:amd64-1127-chase01 -c \
      "grep -cF '$t' /usr/share/nginx/html/assets/index-*.js" || true)
  echo "$t = $n"      # 期望 ≥1
done
```

### 1.4 自证实测结果（2026-11-27 开发机，已跑）

backend（`docker run --rm --platform linux/amd64`）：

| 项 | 实测 |
|---|---|
| `alembic/versions/0172_procurement_chase.py` | ✅ 存在 |
| `app/main.py` → `procurement_chase` 挂载 | ✅ 命中 2 处 |
| `app/services/procurement_command_port.py` → `sendChase` | ✅ 命中 2 处 |
| `app/services/procurement_chase_service.py` → `createDraftFromPending` | ✅ 命中 1 处 |
| `app/domain/procurement_scope_ruling.py` → `procurement_chase_draft` 裁决 | ✅ 命中 1 处 |
| `import app.services.procurement_chase_service` | ✅ `chase_service_ok True` |

frontend：

| 项 | 实测 |
|---|---|
| bundle | `index-D4h2y-ri.js` |
| `按供应商创建催货单` | ✅ 命中 1 |
| `催货单` | ✅ 命中 1 |

## 2. Phase 0 生产预检（只读）

```bash
# ⓪ compose 入口核对（权威来源 = 现役容器 label，不要靠 ls 猜）
docker inspect qa-backend --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'

qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}

qa_compose config --quiet && echo COMPOSE_OK
qa_compose config | grep 'image:'          # v1 无 --images，用这条
qa_compose ps

# 现役镜像
docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' | grep -E 'offline|qa-system_frontend'

# 现役前端 bundle（记录备用，回滚用）
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'

# 迁移版本（**不是 0171 ⇒ 停**）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'

# 健康 ×2 + 磁盘
curl -s -o /dev/null -w "backend=%{http_code}\n"  http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "frontend=%{http_code}\n" http://localhost:5173/
df -h / /var/lib/docker | tail -2
```

### 2.1 停下条件

- ⓪ compose 入口与 §0.2 假设不符 ⇒ 停，先核对再动
- `config | grep 'image:'` 里 backend / frontend 的镜像引用名**不是** `qa-backend:offline` /
  `qa-system_frontend` ⇒ 停（以实测为准，回写 skill 固定值表）
- alembic **不是 `0171`** ⇒ 停（别的批次先落地了，需重新评估边界）
- 任一健康检查非 200 ⇒ 停
- 磁盘余量不足（backend tar 431M + 前端 26M + 解压余量）⇒ 停

## 3. Phase 1 回滚锚点（三者齐备才准进 Phase 2）

```bash
# ① 镜像锚点（⚠️ 引用名自带 tag，锚点 tag 只能 <repo>:pre-1127-chase01，不能两个冒号）
docker tag qa-backend:offline   qa-backend:pre-1127-chase01
docker tag qa-system_frontend   qa-system_frontend:pre-1127-chase01
docker images | grep -E 'qa-backend:pre-1127-chase01|qa-system_frontend:pre-1127-chase01'

# ② 库备份（plain SQL；0 字节 ⇒ 重跑）
mkdir -p /opt/qa-system/backups
BACKUP=/opt/qa-system/backups/pg_pre_1127-chase01_$(date +%Y%m%d_%H%M).sql
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$BACKUP"
ls -l "$BACKUP"          # 期望 > 0 字节

# ③ 记录 PRE_VER
PRE_VER=0171              # 回滚 downgrade 的目标
echo "PRE_VER=$PRE_VER  BACKUP=$BACKUP"
```

## 4. Phase 2 传输（开发机 → 生产机）

```bash
# 生产机上
sha256sum qa-backend-amd64-1127-chase01.tar.gz qa-frontend-amd64-1127-chase01.tar.gz   # 比 §1.2
docker load -i qa-backend-amd64-1127-chase01.tar.gz
docker load -i qa-frontend-amd64-1127-chase01.tar.gz

# tag 成生产 compose 实际引用的名字
docker tag qa-system-backend:amd64-1127-chase01   qa-backend:offline
docker tag qa-system-frontend:amd64-1127-chase01  qa-system_frontend
docker images | grep -E 'qa-backend:offline|qa-system_frontend'    # Image ID 与 §1.1 一致
```

## 5. Phase 3 切换（先后端 → worker → 前端，不碰数据容器）

```bash
# ① 后端（CMD 自带 alembic upgrade head：0171 → 0172，然后起 uvicorn）
qa_compose up -d --no-build --no-deps --force-recreate backend
docker logs -f qa-backend 2>&1 \
  | grep -E "Running upgrade|0172|Application startup complete|ERROR|blocking" | head -60

# ② 迁移版本复核（期望 0172）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'

# ③ seed（本批**无新增 seed**）
#    本批不新增 Agent / 工具 / 场景 / 绑定 —— 催货单不接 LLM、不需要本体绑定。
#    （1126 的 seed 链已就位，本批不重跑；若日志报「场景 Agent 不存在」⇒ 那是 1126 未落全，
#      按 1126 runbook §5 ③ 补，不属本批范围。）

# ④ worker（与 backend 同镜像；不 recreate 就跑旧代码）
qa_compose up -d --no-build --no-deps --force-recreate agent-orchestration-worker

# ⑤ 前端
qa_compose up -d --no-build --no-deps --force-recreate frontend
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend|worker'
```

> - `--no-build` 防现场 build；`--no-deps` 防顺带重启 postgres/neo4j/milvus；`--force-recreate` 才真换镜像。
> - v1 compose：**不要**加 `--remove-orphans`。
> - 本批**无 `.env` 变更**。若现场发现 `ENABLE_AGENT_ORCHESTRATION` 为 false，按「先改 .env 后
>   force-recreate」铁律处理（顺序反了等于没改）。

## 6. Phase 4 验收探针

### 6.1 后端

```bash
# 迁移版本
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'          # 期望 0172

# 三张新表（期望 3 行）
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F"|"' <<'SQL'
SELECT table_name FROM information_schema.tables
WHERE table_name LIKE 'procurement_chase%' ORDER BY 1;
SQL

# 新路由已注册（401/403 = 已注册且要登录；404 = 未注册）
curl -s -o /dev/null -w "chase_drafts=%{http_code}\n" http://localhost:8000/api/v1/procurement/chase/drafts

# 命令端口 / 服务层可导入
docker exec qa-backend python -c "import app.services.procurement_chase_service as m; print('chase_service_ok', bool(m.ProcurementChaseService))"
```

### 6.2 前端（代码探针）

```bash
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'   # 期望 `index-D4h2y-ri.js`
for t in "按供应商创建催货单" "催货单" "已隐藏"; do
  n=$(docker exec qa-frontend sh -c "grep -cF '$t' /usr/share/nginx/html/assets/index-*.js" || true)
  echo "$t = $n"      # 期望 ≥1
done
```

### 6.3 浏览器端到端（Ctrl+Shift+R 硬刷新）

1. 登录 → 「采购 AI 副驾」→ **到货跟踪**场景 → 跑一次查询（或用欢迎卡）；
2. 「未到货订单」tab → **展开某个订单的行明细** → 勾选要催的行
   （只有 `notArrived` 状态、非脏行的行可勾；脏行 checkbox 应为禁用）；
3. 点「**按供应商创建催货单（N）**」→ 成功提示「已创建 N 张催货单（M 个供应商）」；
4. 进 **催货单管理页**（`/procurement/chase`）→ 看到刚建的草稿（`DRAFT`）；
5. 详情 → **校验**（→ `PENDING_APPROVAL`）→ **审批通过**（→ `READY_TO_SEND`）→
   **发送催货**（→ `SENT`）；决策时间线里最后一条 `SEND` 的意见是 `CHASE-SIM-xxxx`；
6. **回归核验**：到货跟踪卡、待到货场景仍正常（无「本体绑定缺失」类报错）。

> 若第 3 步提示「供应商不在统一实体映射」⇒ 该 supplierCode 不在 `entity_mapping`
> （生产有 3500 条 SUPPLIER 映射，新供应商未覆盖）⇒ 属数据问题，按 §6.4 记名后处理。
> 若第 5 步报 409 ⇒ 状态机拒绝（未审批就 send）⇒ 属正常守卫。

### 6.4 已知边界（不必当故障）

- 催货单**不接 LLM**：发送走模拟器（`SimulatedErpSrmAdapter.sendChase`）⇒
  不会真的通知供应商；外部引用是确定性 `CHASE-SIM-*`。
- **供应商答复回流不做**（本期 `SENT` 即终态）；**超期未答复自动升级不做**（二期）。
- 催货行是**服务端回查**的：前端只提交 `{poNo, poLine}` 身份对，页面上看到的欠交数量
  可能与开单瞬间不同（以服务端为准）。

## 7. 回滚

后端回滚**必须先降库**：库在 0172 时，旧镜像（只认得 0171）启动会
`Can't locate revision identified by '0172'` 直接起不来。

```bash
# ① 停后端与 worker
qa_compose stop backend agent-orchestration-worker

# ② 用**新镜像**降级到 PRE_VER（0171）—— 会 drop 三张催货表（催货单数据丢失，见 §0.2）
docker run --rm --network qa-system_default \
  -v /opt/qa-system/docker/.env:/app/.env:ro \
  qa-system-backend:amd64-1127-chase01 alembic downgrade 0171

# ③ 复核
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'          # 期望 0171

# ④ 换回旧镜像并拉起
docker tag qa-backend:pre-1127-chase01         qa-backend:offline
docker tag qa-system_frontend:pre-1127-chase01 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate backend agent-orchestration-worker frontend
```

前端可**单独**回滚（不动库、不动后端）：

```bash
docker tag qa-system_frontend:pre-1127-chase01 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate frontend
```

## 8. 本批明确没做的事

- ❌ 不动 `postgres` / `neo4j` / `milvus`（etcd/minio/standalone）/ `qa-objects` / `mysql-demo` 容器
- ❌ 不改 `.env` 的任何开关或密钥（本批无新增 env）
- ❌ 不跑任何 seed（本批不新增 Agent / 工具 / 场景 / 绑定）
- ❌ 不装本体图/向量（本批不加本体类）
- ❌ 不接真实 SRM（发送走模拟器；换端口实现即可，属二期）
- ❌ 不做供应商答复回流 / 超期自动升级（二期）
- ❌ 不含 main 上未合入的任何分支改动（**含 #187 本身** ⇒ 发版前先确认 #187 已合入 main）
- ❌ 不动 `AGENT_ORCHESTRATION` 开关与既有 Agent / 场景配置

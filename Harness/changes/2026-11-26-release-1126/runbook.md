# 生产部署：到货跟踪（P5）+ 场景配置化（P4）+ 待到货迁本体（2026-11-26，标识 `1126`）

> 结构遵循 `Harness/skills/thdeploy/SKILL.md`（对照 `2026-11-22-prod-deploy/runbook.md`）。
> 除 §0 的「每批变量」外，**所有容器/镜像/网络名均为固定值预填**，现场不需要补名字。

## 0. 一句话说清这次改了什么

把采购 Copilot 从「硬编码表名」迁到「本体绑定解析」，并新增**到货跟踪**场景（P5）与
**场景配置化**（P4，场景 = 库里一行配置）。本批**动数据库**：新增 2 条迁移（`0169 → 0171`）、
新增 1 张配置表、2 个新列。

| PR | 内容 |
|---|---|
| #178 | 全部改动（OPEN，head `c63f8eed`，`feat/ontology-evolution` → `main`） |

### 0.1 迁移表（`0169 → 0171`）

| rev | 动作 | 数据回填 | downgrade |
|---|---|---|---|
| 0170 | 建表 `copilot_scenario`；`procurement_copilot_conversation` 加列 `pinned_at` | **无** | drop 表 / drop 列 |
| 0171 | `procurement_copilot_turn` 加列 `data_stamp` | **无** | drop 列 |

**回滚边界（PRE_VER）= `0169`**。两条迁移都是纯加法 DDL、零数据回填，降级不丢业务数据；
但按硬规则仍**必须**在 Phase 1 做 `pg_dump`（`copilot_scenario` 的场景配置行只存在库里，种子可重建但会丢管理员改过的配置）。

### 0.2 验收标准（一句话）

场景墙出现「到货跟踪」；**进场景即出本期业务现状卡**；点「近 3 个月到货概览」能出
**结构化结果卡**（订单到货率 + 三态 + 供应商表 + 双 Tab）；结论正文**不出现工具名/内部字段名**；
**待到货场景不回归**（迁本体后仍能出摘要卡）。

### 0.3 ⚠️ 本批唯一的硬门禁：本体绑定必须先在库

本批起，「待到货」和「到货跟踪」都走 `procurement_source_binding` /
`procurement_property_binding`（运行时解析器 **fail-closed**，没有兜底数据源）。
生产此前这两个场景是**直连物理表**，从未需要绑定行 ⇒ **库里很可能一条都没有**。

若缺绑定：
- 「待到货」从**能用**变成**接口 502**（回归！）；
- 「到货跟踪」直接不可用。

⇒ §2.1 的绑定审计是本批**唯一**可以「停下来不发」的依据，**审计不过不得切后端**。

### 0.4 执行顺序速查（一页版）

| # | 阶段 | 关键命令 | 停/过 |
|---|---|---|---|
| 1 | 预检 | §2 ⓪–④ | alembic=0169、health=200 |
| 2 | **绑定审计** | §2.1 ④-1 / ④-2 | **3 行 + 12 行全 t**，否则停 |
| 3 | 锚点 | §3 `docker tag …:pre-1126` ×2 + `pg_dump` | 三者齐备 |
| 4 | 传输 | §4 比 sha256 → `docker load` ×2 → `docker tag` ×2 | Image ID 吻合 |
| 5 | 切后端 | §5 ① `qa_compose up … backend` | 日志见 0170/0171 + startup complete |
| 6 | 迁移复核 | §5 ② | 0171 |
| 7 | seed | §5 ③ a→b→c→d（顺序不可颠倒） | 四步都无异常 |
| 8 | worker | §5 ④ | 容器 Running |
| 9 | 前端 | §5 ⑤ | — |
| 10 | 验收 | §6.1 / 6.2 / 6.3 | 本文件 §0.2 |

---

## 1. 交付物（开发机产出）

> ⛔ **构建尚未完成（2026-11-26 14:45 状态）**：本机访问 Docker Hub 不通
> （`registry-1.docker.io/v2/` 超时返回 000），`docker buildx` 卡在
> `load metadata for docker.io/library/...` 这一步。镜像**还没产出**，下表的
> Image ID / 大小 / sha256 均为待回填占位，**不要拿去发版**。
> 构建恢复后按 §1.4 的步骤产出并回填本表。

### 1.1 镜像

| 镜像 tag | 平台 | Image ID | 说明 |
|---|---|---|---|
| `qa-system-backend:amd64-1126` | linux/amd64 | `BACKEND_IMAGE_ID` | backend + agent-orchestration-worker 同镜像 |
| `qa-system-frontend:amd64-1126` | linux/amd64 | `FRONTEND_IMAGE_ID` | nginx + 静态 bundle |

### 1.2 传输包

| 文件 | 大小 | sha256 |
|---|---|---|
| `qa-backend-amd64-1126.tar.gz` | `BACKEND_TAR_SIZE` | `BACKEND_TAR_SHA` |
| `qa-frontend-amd64-1126.tar.gz` | `FRONTEND_TAR_SIZE` | `FRONTEND_TAR_SHA` |

### 1.3 镜像内容自证（构建脚本内置，任一缺失即构建失败）

- backend：`agent_tools_procurement.py:_PendingDialect`、`procurement_analytics.py:该数据源执行待到货查询失败`、
  `procurement_copilot.py:deleteCopilotConversation` / `pinCopilotConversation`、
  `copilot_scenario_render.py:renderScenarioQuestion`、`seed_copilot_scenarios.py:seed_copilot_scenarios`
- frontend：bundle `FRONTEND_BUNDLE`，含「采购 Copilot」「待到货 / 逾期未到」「分析结果」

> 生产机**不 build**：镜像靠开发机 `docker save` + `docker load`。

### 1.4 构建步骤（含离线兜底）

标准路径（Docker Hub 可达时）：

```bash
cd <repo>
./scripts/build_backend_images.sh  --platform linux/amd64 --tag amd64-1126
./scripts/build_frontend_image.sh  --platform linux/amd64 --tag amd64-1126
```

**Docker Hub 不可达时**（本次实测）：`FROM` 里的裸 tag 会被补成 `docker.io/...` 去查元数据而卡死，
即使本地有同名镜像。改用 **digest 引用**即只读本地镜像库、完全不联网：

```bash
# 1) 取本地镜像 ID（基础镜像本地已有）
B=$(docker image inspect qa-system-backend-base:2026-11-amd64 --format '{{.Id}}')
# 2) 基础镜像本地没有的公共镜像，先从可用镜像站拉（本机可用：docker.m.daocloud.io）
docker pull --platform linux/amd64 docker.m.daocloud.io/library/node:20-alpine
docker pull --platform linux/amd64 docker.m.daocloud.io/library/python:3.12-slim
# 3) 把两个 Dockerfile 复制成临时副本，把 FROM 改成 sha256:<ID> 后构建
#    （仓库内 Dockerfile 不改；构建完删副本）
```

> 现成的两个临时副本已放在开发机 `/tmp/qa-release-1126/.tmp.Dockerfile.{backend,frontend}`
> （FROM 已全部换成 digest），可直接：
> ```bash
> docker buildx build --platform linux/amd64 -f /tmp/qa-release-1126/.tmp.Dockerfile.backend \
>   --build-arg "RUNTIME_BASE=<backend-base 的 sha256>" -t qa-system-backend:amd64-1126 --load <repo>
> docker buildx build --platform linux/amd64 -f /tmp/qa-release-1126/.tmp.Dockerfile.frontend \
>   -t qa-system-frontend:amd64-1126 --load <repo>
> ```
> 注：`--load` 后需人工补跑 §1.3 的自检（脚本里的 grep 校验不随之执行）。

打包：

```bash
docker save qa-system-backend:amd64-1126  | gzip > qa-backend-amd64-1126.tar.gz
docker save qa-system-frontend:amd64-1126 | gzip > qa-frontend-amd64-1126.tar.gz
shasum -a 256 qa-*-amd64-1126.tar.gz
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
qa_compose config | grep 'image:'        # v1 无 `config --images`，用这条看引用名

# ① 现役容器与镜像（回滚锚点以实际输出为准）
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend|worker'

# ② 现役前端 bundle（记录，回滚时对照）
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'

# ③ 现役库 alembic 版本（预期 0169；不是 0169 ⇒ 停）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'

# ④ 健康与磁盘
curl -s -o /dev/null -w "backend_health=%{http_code}\n" http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "frontend_health=%{http_code}\n" http://localhost:5173/
df -h /var/lib/docker
```

### 2.1 本体绑定审计（⛔ 不过就停）

```bash
# ④-1 三个类是否各有 1 条「已确认 + BASE 角色 + 生效」源绑定；期望 3 行
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F"|"' <<'SQL'
SELECT c.class_name, b.source_table, b.is_candidate,
       (SELECT count(*) FROM procurement_property_binding p WHERE p.source_binding_id=b.id) AS props
FROM procurement_source_binding b JOIN ontology_class c ON c.id=b.class_id
WHERE c.class_name IN ('DWD_PURCHASE_ORDER_DTL','DWD_GOODS_RECEIPT_DTL','DIM_FACILITY')
  AND b.binding_role='BASE' AND b.valid_to IS NULL
ORDER BY 1;
SQL

# ④-2 工具必需的 12 个属性是否都已绑定；期望 12 行全 t
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F"|"' <<'SQL'
WITH req(class_name, property_name) AS (VALUES
 ('DWD_PURCHASE_ORDER_DTL','PO_NO'),('DWD_PURCHASE_ORDER_DTL','PO_LINE_NO'),
 ('DWD_PURCHASE_ORDER_DTL','PO_QTY'),('DWD_PURCHASE_ORDER_DTL','ORDER_DATE'),
 ('DWD_PURCHASE_ORDER_DTL','SUPPLIER_CODE'),('DWD_PURCHASE_ORDER_DTL','ITEM_CODE'),
 ('DWD_PURCHASE_ORDER_DTL','EXP_RCV_DATE'),
 ('DWD_GOODS_RECEIPT_DTL','PO_NO'),('DWD_GOODS_RECEIPT_DTL','PO_LINE_NO'),
 ('DWD_GOODS_RECEIPT_DTL','RCV_QTY_STU'),('DWD_GOODS_RECEIPT_DTL','ITEM_DESC'),
 ('DWD_GOODS_RECEIPT_DTL','RCV_DATE'))
SELECT r.class_name, r.property_name, EXISTS (
  SELECT 1 FROM procurement_source_binding b
  JOIN ontology_class c ON c.id=b.class_id
  JOIN procurement_property_binding pb ON pb.source_binding_id=b.id
  JOIN ontology_property p ON p.id=pb.property_id
  WHERE c.class_name=r.class_name AND p.property_name=r.property_name
    AND b.binding_role='BASE' AND b.is_candidate=false
    AND b.valid_to IS NULL AND pb.valid_to IS NULL
    AND b.valid_from <= now()) AS ok
FROM req r ORDER BY 1,2;
SQL
```

**判定（硬门禁）**

| 结果 | 动作 |
|---|---|
| ④-1 = 3 行且 `is_candidate=f`，④-2 = 12 行全 `t` | ✅ 通过，继续 Phase 1 |
| ④-1 缺 `DIM_FACILITY` **仅此一条** | 可继续：新镜像启动链会自动补（§5 ③ 里也有显式 seed） |
| ④-1 缺 `DWD_PURCHASE_ORDER_DTL` / `DWD_GOODS_RECEIPT_DTL`，或 ④-2 有 `f` | ⛔ **停止发版**，把两段输出发回。缺失绑定需要按类补 seed（一次到位、可复跑），**不要**手工逐条建 |

> 为什么停：缺绑定不影响迁移/启动，但会让「待到货」从能用变成 502 —— 那是比不发版更糟的结果。

### 2.2 停下条件（汇总）

- ③ alembic **不是 0169**（说明有别批次先落地，需重新评估边界）
- ④ 任一健康检查非 200
- 2.1 判定为「停止」
- `qa_compose config` 输出里 backend / frontend 的**镜像引用名**不是 `qa-backend:offline` / `qa-system_frontend`
  （若不同，以实测为准，并回写 `Harness/skills/thdeploy/SKILL.md`）

---

## 3. Phase 1 回滚锚点（镜像 tag + pg_dump）

```bash
# ① 镜像锚点（⚠️ 引用名自带 tag，锚点 tag 只能写成 <repo>:pre-1126，不能写两个冒号）
docker tag qa-backend:offline   qa-backend:pre-1126
docker tag qa-system_frontend   qa-system_frontend:pre-1126
docker images | grep 'pre-1126'

# ② 数据库锚点
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > /opt/qa-system/backups/pg_pre_1126_$(date +%Y%m%d_%H%M).sql
ls -lh /opt/qa-system/backups/ | tail -3     # 0 字节 ⇒ 重跑并检查报错
```

三者齐备（`pre-1126` 两个 tag + 非空 pg_dump + 记录 `PRE_VER=0169`）才准进 Phase 2。

---

## 4. Phase 2 传输 + 校验 + 加载 + tag

```bash
# 传完先比 sha256（对 §1.2）
sha256sum qa-backend-amd64-1126.tar.gz qa-frontend-amd64-1126.tar.gz

docker load -i qa-backend-amd64-1126.tar.gz
docker load -i qa-frontend-amd64-1126.tar.gz

# tag 成生产 compose 实际引用的名字（固定值）
docker tag qa-system-backend:amd64-1126  qa-backend:offline
docker tag qa-system-frontend:amd64-1126 qa-system_frontend

docker images | grep -E 'qa-backend:offline|qa-system_frontend'
# 两行 Image ID 应为 §1.1 的 BACKEND_IMAGE_ID / FRONTEND_IMAGE_ID
```

---

## 5. Phase 3 切换（先后端 → seed → worker → 前端，不碰数据容器）

```bash
# ① 后端（CMD 自带 alembic upgrade head：0169 → 0170 → 0171，然后起 uvicorn）
qa_compose up -d --no-build --no-deps --force-recreate backend
docker logs -f qa-backend 2>&1 \
  | grep -E "Running upgrade|017[01]|Application startup complete|ERROR|blocking|seed" | head -80

# ② 迁移版本复核（期望 0171）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'
```

**此时启动日志里应出现 `procurement agent profile seed` 的告警**（「场景 Agent DELIVERY_TRACKING_AGENT 不存在」）——
**属预期**：Agent 行由 `seed_agents` 建，而它不在启动链上。按 ③ 顺序补即可。

```bash
# ③ seed（顺序不可颠倒；全部幂等，可重复跑）
#    a. 建 DELIVERY_TRACKING_AGENT 等 Agent 行 + 工具绑定
qa_compose exec backend python -m scripts.seed_agents
#    b. 建/启用编排 profile（依赖 a 的 Agent 行，缺行会直接抛错）
#       **本步同时把「存量 profile 的旧措辞指令」刷新成新版**（2026-11-26）：
#       指令改措辞（去掉「来源要写工具名」、加「正文禁内部术语」）后，存量部署的
#       指令不会自动更新 ⇒ LLM 继续把 delivery_tracking_query 印进正文。
#       同步只在存量指令**逐字等于上一版种子**时进行（管理员改过的不动）。
qa_compose exec backend python -m scripts.seed_procurement_agent_profiles
#    c. 写入 copilot_scenario 场景配置（P4 起场景=DB 资产；不跑 ⇒ 场景墙空白）
qa_compose exec backend python -m scripts.seed_copilot_scenarios
#    d. 补 DIM_FACILITY 源绑定（启动链已兜底；显式跑一次看输出）
qa_compose exec backend python -m scripts.seed_procurement_bindings

# ④ worker（与 backend 同镜像；不 recreate 就跑旧代码）
qa_compose up -d --no-build --no-deps --force-recreate agent-orchestration-worker

# ⑤ 前端
qa_compose up -d --no-build --no-deps --force-recreate frontend
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend|worker'
```

> - `--no-build` 防现场 build；`--no-deps` 防顺带重启 postgres/neo4j/milvus；`--force-recreate` 才真换镜像。
> - v1 compose：**不要**加 `--remove-orphans`。
> - 本批**无 `.env` 开关变更**（`ENABLE_AGENT_ORCHESTRATION=true` 自 2026-11-23 起已是 true）。
>   若现场发现它为 false，按「先改 .env 后 recreate」铁律处理，顺序反了等于没改。

---

## 6. Phase 4 验收探针

### 6.1 后端

```bash
# 迁移版本
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'                       # 期望 0171

# 新表 / 新列
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\d copilot_scenario"' | head -20
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F"|"' <<'SQL'
SELECT table_name, column_name FROM information_schema.columns
WHERE (table_name, column_name) IN (('procurement_copilot_conversation','pinned_at'),
                                    ('procurement_copilot_turn','data_stamp'))
ORDER BY 1;
SQL

# 场景配置已入库（期望 3 行：pending_delivery / delivery_tracking / supplier_risk）
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F"|"' <<'SQL'
SELECT scenario_code, enabled, sort_order FROM copilot_scenario ORDER BY sort_order;
SQL

# 新路由已注册（401/403 = 已注册且要登录；404 = 未注册）
# 注意：/copilot/* 整组挂在 ENABLE_AGENT_ORCHESTRATION 下 ⇒ 404 优先怀疑该开关没进进程，
#       而不是「路由没写」；/pending-delivery/* 不受开关控制，恒在。
curl -s -o /dev/null -w "scenarios=%{http_code}\n"    http://localhost:8000/api/v1/procurement/copilot/scenarios
curl -s -o /dev/null -w "sites=%{http_code}\n"        http://localhost:8000/api/v1/procurement/pending-delivery/sites
curl -s -o /dev/null -w "pd_summary=%{http_code}\n"   http://localhost:8000/api/v1/procurement/pending-delivery/summary
curl -s -o /dev/null -w "dt_overview=%{http_code}\n"  http://localhost:8000/api/v1/procurement/delivery-tracking/overview

# 指令措辞已刷新（正文禁内部术语）——期望 7 行全带禁令
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F"|"' <<'SQL'
SELECT a.agent_code, (p.instructions LIKE '%内部术语%') AS has_ban
FROM agent_definition a JOIN agent_orchestration_profile p ON p.agent_id = a.id
WHERE a.agent_code LIKE 'PROCUREMENT%' OR a.agent_code = 'DELIVERY_TRACKING_AGENT'
ORDER BY 1;
SQL

# 工具已注册 + 已 seed 进工具目录（期望各 1 行）
docker exec qa-backend python -c "import app.services.delivery_tracking_query as m; print('module_ok', bool(m.deliveryTrackingHandler))"
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F"|"' <<'SQL'
SELECT name, handler_ref, enabled FROM agent_tool_config WHERE name = 'delivery_tracking_query';
SQL

# Agent → profile → 工具绑定（期望 1 行：enabled=t、tool=delivery_tracking_query）
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F"|"' <<'SQL'
SELECT a.agent_code, p.enabled AS profile_enabled, b.tool_name, b.enabled AS tool_enabled
FROM agent_definition a
LEFT JOIN agent_orchestration_profile p ON p.agent_id = a.id
LEFT JOIN agent_orchestration_binding b ON b.profile_id = p.id
WHERE a.agent_code = 'DELIVERY_TRACKING_AGENT';
SQL
```

### 6.2 前端（代码探针）

```bash
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'   # 期望 FRONTEND_BUNDLE
for t in "到货达成概览" "订单到货率" "量级加权到货率" "本期业务现状（实时）" "待到货 / 逾期未到"; do
  n=$(docker exec qa-frontend sh -c "grep -cF '$t' /usr/share/nginx/html/assets/index-*.js" || true)
  echo "$t = $n"     # 期望 ≥1
done
```

### 6.3 浏览器端到端（Ctrl+Shift+R 硬刷新）

1. 登录 → 「采购 Copilot」场景墙：应看到 **3 张场景卡**（待到货 / 到货跟踪 / 供应商风险速览）；
2. 点「到货跟踪」→ **进场景即应看到「本期业务现状（实时）」卡**（零 LLM，不用发问）：
   - 订单到货率 + 量级加权到货率 + 订单总数 + 未到货订单；
   - 窗口为本期 24 日周期（如 2026-10-24 ~ 2026-11-24）；
3. 点推荐问题「**近 3 个月到货概览**」→ 等结构化卡：
   - 「到货达成概览」+ 「数仓层 DWD」角标；
   - 订单到货率（大号主指标）、量级加权到货率（带「受超大额未到货行影响，仅作参考」）；
   - 全到货 / 部分到货 / 未到货三态 Tag；双 Tab 明细可展开物料行；
   - 卡上**不应**出现「结果数据过大」降级提示；
4. **正文不再出现工具名**（本批修复）：展开「解读与建议」卡，正文应有
   「采购订单与到货记录 + 窗口」这类业务说法，**不得**出现
   `delivery_tracking_query` / `window.mode` / `datasourceId` / `monthsBack`；
5. 回场景墙点「待到货」→ 发一次 → **摘要卡正常出**（回归核验）；
6. 侧栏会话可置顶（P4 pin），刷新后置顶仍在。

> 若第 2 步出「本体绑定缺失 / 无法解析」类报错 ⇒ 直接回 §2.1，说明绑定没落全。
> 若第 4 步仍见工具名 ⇒ 指令没刷新，回 §5 ③b 重跑 `seed_procurement_agent_profiles`
> 并确认输出里有「指令刷新」字样。

---

## 7. 回滚

后端回滚**必须先降库**：库在 0171 时，旧镜像（只认得 0169）启动会
`Can't locate revision identified by '0171'` 直接起不来。

```bash
# ① 停后端
qa_compose stop backend agent-orchestration-worker

# ② 用**新镜像**降级到 PRE_VER（0169）
docker run --rm --network qa-system_default \
  -v /opt/qa-system/docker/.env:/app/.env:ro \
  qa-system-backend:amd64-1126 alembic downgrade 0169

# ③ 复核
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'                    # 期望 0169

# ④ 换回旧镜像并拉起
docker tag qa-backend:pre-1126        qa-backend:offline
docker tag qa-system_frontend:pre-1126 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate backend agent-orchestration-worker frontend
```

前端可**单独**回滚（不动库、不动后端）：
```bash
docker tag qa-system_frontend:pre-1126 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate frontend
```

> downgrade 0171 → 0170 → 0169 只 drop 两个列与一张表；`copilot_scenario` 里的场景配置行随之丢失，
> 重新发版后 `seed_copilot_scenarios` 会重建（幂等）。

---

## 8. 本批明确没做的事

- ❌ 不动 `postgres` / `neo4j` / `milvus`（etcd/minio/standalone）/ `qa-objects` / `mysql-demo` 容器
- ❌ 不改 `.env` 的任何开关或密钥（本批无新增 env）
- ❌ 不装本体图/向量（本批不加本体类；`DIM_FACILITY` 类在库中已存在，只补绑定行）
- ❌ 不跑 `m1b_confirm_procurement_bindings.py`（该脚本自带 dev-only 守卫、且会顺带做域发布；
  生产缺绑定时按 §2.1 停下另补，不在现场跑）
- ❌ 不含 main 上未合入的任何分支改动

# 生产发布方案：chat 输出中文化（1101）

- **日期**：2026-11-01
- **目标**：192.168.205.82（CentOS 7，内网隔离，Docker Compose）
- **代码基线**：`main` @ `0e248bca`（#94 squash 合并；含 #95 L4 轨迹卡、#93 聊天输出同步）
- **迁移**：0139 → **0140**（`ontology_property.unit VARCHAR(50)` 可空，无回填）。
  生产已在 0139（1031 l4trace 批），upgrade head 只跑 0140。
  ⚠️ 0140 初版曾编号 0139 与 l4trace 撞号，已重编号；**镜像内必须只有
  `0140_ontology_property_unit.py` 这一条 unit 迁移**（见 Phase 2 ④ 哨兵）。
- **发版源**：worktree `/private/tmp/qa-deploy-1101-zhlabels`（detached @ `0e248bca`）
- **操作分工**：开发机已产出镜像包与本文档；生产服务器命令由你执行（内网无 SSH 免密通道）

## 0. 交付物（开发机，构建后回填 sha256）

| 文件 | 内容 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1101-zhlabels.tar.gz` | `qa-system-backend:amd64-1101-zhlabels`（linux/amd64） | `4271f68d12c041353bffc07759a964f93426d9fc317d1c0dd1dc663aecb78d51` | 228M |
| `/tmp/qa-frontend-amd64-1101-zhlabels.tar.gz` | `qa-system-frontend:amd64-1101-zhlabels`（linux/amd64，`VITE_API_BASE_URL=` 空同源） | `31816a20672e15ce4896d37f0ea60d01e8f2643186fb4dbb2cfa96cbe6eb2a87` | 27M |

镜像侧核验（开发机，构建后执行并回填结果）：

- backend：`0140_ontology_property_unit.py` 在镜像内（docker export 核验）；`field_display_resolver.py` 在；无 `/app/.env`
- frontend：`localhost:5173` 引用 **0**；bundle `index-CAcx60U4.js`；`columnLabels` 出现 3 处（均经 docker run 实测）

## 1. qa_compose 入口（每个新 shell 先定义）

```bash
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.yml \
    -f /opt/qa-system/docker/docker-compose.centos.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK
```

## 2. Phase 1 预检（只读）

```bash
# ① 容器实际引用的镜像名（tag 前以它为准，别照抄仓库名）
docker ps --format '{{.Names}}\t{{.Image}}' | grep -E 'backend|frontend'

# ② 当前迁移版本（预期 0139）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'

# ③ unit 列预检（预期 0 行——迁移前不该存在）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT count(*) FROM information_schema.columns \
   WHERE table_name='"'"'ontology_property'"'"' AND column_name='"'"'unit'"'"';"'

# ④ 磁盘余量（两包 ~250M，load 后镜像层翻倍）
df -h /var/lib/docker
```

**若 ② 不是 0139，先停下发我结果。**

## 3. Phase 2 备份 + 回滚锚点

```bash
mkdir -p /root/backup
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > /root/backup/qa_metadata_pre_1101_zhlabels_$(date +%m%d).sql

# 锚点前先确认 ① 的实际镜像名（本例按 qa-backend:offline / qa-system_frontend，
# 若 ① 输出不同，以 ① 为准）
docker tag qa-backend:offline       qa-backend:pre-1101-zhlabels
docker tag qa-system_frontend       qa-system_frontend:pre-1101-zhlabels
```

## 4. Phase 3 传输 + 加载 + tag

开发机 → 生产（scp/U 盘均可）。**传完先比对 sha256**：

```bash
shasum -a 256 qa-backend-amd64-1101-zhlabels.tar.gz qa-frontend-amd64-1101-zhlabels.tar.gz   # 开发机
sha256sum  qa-backend-amd64-1101-zhlabels.tar.gz qa-frontend-amd64-1101-zhlabels.tar.gz      # 生产
```

生产：

```bash
docker load -i qa-backend-amd64-1101-zhlabels.tar.gz
docker load -i qa-frontend-amd64-1101-zhlabels.tar.gz

docker tag qa-system-backend:amd64-1101-zhlabels   qa-backend:offline
docker tag qa-system-frontend:amd64-1101-zhlabels  qa-system_frontend

qa_compose config | grep image     # 确认解析到新 tag
```

## 5. Phase 4 迁移（显式跑，不靠容器 CMD）

```bash
qa_compose stop backend
qa_compose run --rm backend alembic upgrade head
# 预期输出：0140_ontology_property_unit running
# （当前 0139 ⇒ 只跑 0140 一步）

docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'   # 期望 0140
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT count(*) FROM information_schema.columns \
   WHERE table_name='"'"'ontology_property'"'"' AND column_name='"'"'unit'"'"';"'  # 期望 1
```

`0140` 只 `add_column`（可空），失败可直接停在这里：backend 仍按旧镜像回滚启动即可。

## 6. Phase 5 起服务 + 冒烟

```bash
qa_compose up -d backend frontend
docker logs --tail 30 qa-backend | grep -E "Application startup complete|Error|Traceback"
```

只读冒烟：

```bash
# 迁移版本 + 列
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'

# 后端新代码哨兵（期望 ≥1：解析层文件在镜像内）
docker exec qa-backend sh -c 'ls /app/app/services/ | grep -c field_display_resolver || true'

# 前端 bundle 新 hash + 中文化标记
curl -sS http://127.0.0.1:5173/ | grep -oE 'index-[A-Za-z0-9_-]+\.js' | head -1
docker exec qa-frontend sh -c 'grep -rc "columnLabels" /usr/share/nginx/html/assets/ | awk -F: "{s+=\$2} END {print s+0}"'  # 期望 ≥1

# 健康
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5173/api/v1/health
```

业务冒烟（浏览器，硬刷新 Cmd/Ctrl+Shift+R）：

1. 登录后问一个会出表格/图表的取数问题（如供应商收货数量汇总）；
2. 已治理字段（本体属性有别名/单位的）表头/图例应显示中文名+单位（`名称（数量）` 形态）；
3. 未治理字段回落原始列名，且回答末尾出现「字段治理提示」段（缺中文含义 / 缺单位两类）；
4. 历史回放同一会话，表头与提示与即时展示一致；
5. 本体 → 属性管理：编辑属性应出现「单位」表单项，保存后重查取数问题，表头带（单位）。

## 7. 回滚

```bash
qa_compose stop backend frontend
docker tag qa-backend:pre-1101-zhlabels       qa-backend:offline
docker tag qa-system_frontend:pre-1101-zhlabels qa-system_frontend
qa_compose up -d backend frontend
```

- `ontology_property.unit` 列可保留（旧代码不读它，无副作用）；如需彻底回退：
  `qa_compose run --rm backend alembic downgrade 0139`（会删列，已填单位随之丢失）。

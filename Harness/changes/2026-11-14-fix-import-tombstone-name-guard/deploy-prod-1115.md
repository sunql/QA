# 生产部署：类墓碑占名（PR #128）

- **代码版本**：`main @ 10c79bb3`（squash 合并 PR #128）
- **变更性质**：**零 DDL**、**零业务数据写**、**零新环境变量**
- **迁移链**：head 仍是 `0143_kpi_time_grain`，与生产当前版本相同 ⇒ 启动时的
  `alembic upgrade head` 是 **no-op**
- **生产命令由宿主机执行**：开发机（192.168.x 内网隔离环境之外的 Mac）**无法直连生产**，
  本文件是交付给生产宿主机执行的 runbook

---

## 1. 交付物（开发机已产出）

| 文件 | 镜像 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1115-tombguard.tar.gz` | `qa-system-backend:amd64-1115-tombguard`（linux/amd64，源 `main @ 10c79bb3`） | `25785c05284dc47726c3974b8b939bf97d6aa59ca9d45c0a87a024f544cf1601` | 218M |
| `/tmp/qa-frontend-amd64-1115-tombguard.tar.gz` | `qa-system-frontend:amd64-1115-tombguard`（linux/amd64，`VITE_API_BASE_URL=` 空 ⇒ 同源相对路径） | `e38a1de389aaa1a826ef99c8d0bc73e2fb172362d9706b1dabcc1393b6993daf` | 26M |

> **worker 复用 backend 包**：若生产开了 `agent-orchestration` profile，
> `qa-agent-orchestration-worker` 与 backend 是**同一镜像、不同 CMD** ⇒ 不需要额外发包，
> 只需把 backend 镜像 retag 成 worker 的镜像名（见 §2 ①-b、§4、§5）。

**镜像侧核验（开发机已实测通过）**：

- backend（`amd64/linux`，image `sha256:c4f5006d…`）：
  `MSG_CLASS_NAME_OCCUPIED_BY_TOMBSTONE` 命中 **2**；`name_collision` **1**；
  `需 owner 部门成员或管理员` **2**；`alembic/versions/0143_kpi_time_grain.py` 在；
  **无 `/app/.env`**
- frontend（`amd64/linux`，image `sha256:d9f6eef7…`）：bundle **`index-LixAJrlt.js`**；
  `tombstonedAlert` **3**（对照键，证明 grep 有效）、`tombstoneTableOnlyTag` **3**、
  `tombstoneTooltip` **3**；`http://localhost:5173` 引用 **0** 处

---

## 2. Phase 1 生产预检（只读，不改动）

```bash
# ⓪ compose 入口 —— **不要靠 `ls` 猜**，从现役容器的 label 直接读出昨天的文件（唯一权威）
docker inspect qa-backend qa-frontend \
  --format '{{.Name}} => {{index .Config.Labels "com.docker.compose.project.config_files"}}'
# 2026-11-15 生产实测结果：两者都指向**单独一个自包含文件**
#   /opt/qa-system/docker/docker-compose.centos7.yml
# 目录里另外那两个 docker-compose.yml / docker-compose.centos.yml 是**仓库同步过来的副本**，
# 不是生产实际使用的入口：前者缺 ollama / `:z`，后者的 `!override` 需要 v2 插件（v1 解析即炸）。
# ⇒ 生产全程只用 label 里读出的那一个文件。多条证据不符时以 label 为准。

# 生产只有 v1（docker-compose 1.29.2；v2 插件未安装）⇒ 只用 centos7.yml 这一个文件，
# 它不含 `!override`，v1 可解析。
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK
qa_compose config --services    # 确认服务名（backend / frontend / … 以及有无 worker）
qa_compose config --images      # 确认镜像名

# ⚠️ 不要改 /opt/qa-system/docker/.env（SECRET_KEY 是 Fernet 主密钥，改了旧 token 全失效）

# ① 现役镜像名（后续 tag 锚点以它为准，别照抄）
docker ps --format '{{.Names}}\t{{.Image}}' | grep -E 'backend|frontend|worker'

# ①-b 是否存在 agent-orchestration-worker（生产若开了 agent-orchestration profile 就会有）
#     它复用**同一个 backend 镜像**、只是 CMD 不同 ⇒ 只换 backend 会让它停在旧代码上
#     （本地栈 2026-11-15 实测踩到：worker 从 11-13 起一直是旧代码）。存在则后续步骤
#     必须一起换，并把它的 `.Image` 名记下来（形如 qa-system-agent-orchestration-worker，
#     下划线/连字符随 compose 版本而变，**以实际输出为准**）
docker ps --format '{{.Names}}\t{{.Image}}' | grep agent-orchestration-worker || echo NO_WORKER

# ② 迁移版本：期望 0143
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'

# ③ 磁盘余量（两包 ~250M，load 后镜像层会翻倍）
df -h /var/lib/docker
```

**停下条件**：② 不是 `0143` ⇒ 停下发我（说明生产被单独迁移过，需要重新核对镜像与库的对应关系）。

---

## 3. Phase 2 回滚锚点（镜像 tag，不碰数据库）

```bash
docker tag qa-backend:offline        qa-backend:pre-1115
docker tag qa-system_frontend        qa-system_frontend:pre-1115

# 若有 worker（①-b 不是 NO_WORKER），锚点用 ①-b 记下的镜像名
# ⚠️ §2 ①-b 的 NO_WORKER 判断与此处 export 要一致；§3–§7 建议在**同一个 shell** 里执行，
#    换 shell 记得重新 export
export WORKER_IMAGE=qa-system-agent-orchestration-worker   # ← 换成 ①-b 的实际输出
docker image inspect "$WORKER_IMAGE" >/dev/null 2>&1 && docker tag "$WORKER_IMAGE" worker:pre-1115

docker images | grep -E 'qa-backend|qa-system_frontend|worker'
```

> 本批**零 DDL、零业务写** ⇒ **不需要 pg_dump**。
> 若仍想留一份数据库快照：`docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > /root/backup/qa_metadata_pre_1115_$(date +%m%d).sql`

---

## 4. Phase 3 传输 + 校验 + 加载 + tag

开发机 → 生产（scp / U 盘均可）。**传完先比对 sha256**：

```bash
shasum -a 256 qa-backend-amd64-1115-tombguard.tar.gz qa-frontend-amd64-1115-tombguard.tar.gz   # 开发机
sha256sum  qa-backend-amd64-1115-tombguard.tar.gz qa-frontend-amd64-1115-tombguard.tar.gz      # 生产
```

生产：

```bash
docker load -i qa-backend-amd64-1115-tombguard.tar.gz
docker load -i qa-frontend-amd64-1115-tombguard.tar.gz

docker tag qa-system-backend:amd64-1115-tombguard   qa-backend:offline
docker tag qa-system-frontend:amd64-1115-tombguard  qa-system_frontend

# 若有 worker：worker 与 backend 同源镜像，只是 compose 用 command 覆盖 CMD
docker image inspect "$WORKER_IMAGE" >/dev/null 2>&1 && \
  docker tag qa-system-backend:amd64-1115-tombguard "$WORKER_IMAGE"

qa_compose config | grep image      # 确认解析到新 tag
```

---

## 5. Phase 4 切换

```bash
qa_compose stop backend frontend
qa_compose up -d backend frontend

# 若有 worker：必须 `--no-build`（否则 compose 会现场 build 一份镜像），
# `--no-deps` 防止顺带重启 postgres/neo4j/milvus
qa_compose --profile agent-orchestration up -d --no-build --no-deps agent-orchestration-worker
```

> ⚠️ **worker 的坑（本地栈 2026-11-15 实测）**：worker 在 compose 里用 `build:` 而非
> `image:`，派生镜像名 `qa-system-agent-orchestration-worker`。只换 backend 不 retag
> worker ⇒ 容器继续跑**旧代码**，且健康检查与日志**全都正常**，不会报错。
> 不带 `--no-build` ⇒ compose 会现场编译（慢，且可能拉到错误架构的基础镜像）。

> backend 的 CMD 是 `alembic upgrade head && uvicorn ...`。
> 本次 head = `0143` = 生产当前版本 ⇒ 迁移为 **no-op**（不会有 DDL，也不会有锁表）。
> **前后端可任意顺序部署**：后端未升级时前端拿不到 `nameCollision` ⇒ 走第三态（不禁勾、不承诺成功）；
> 前端未升级时同样不启用新逻辑 ⇒ 不存在「前端严于后端」的中间态。

---

## 6. Phase 5 验收探针

```bash
# ① 后端已起（等 20~40s，启动要跑 seed）
docker logs qa-backend --since 5m 2>&1 | tail -5
# 期望出现：Application startup complete. / Uvicorn running on http://0.0.0.0:8000

# ② 后端代码探针：全部从 0 变正数才算部署生效
docker exec qa-backend sh -c "
  echo MSG=\$(grep -c 'MSG_CLASS_NAME_OCCUPIED_BY_TOMBSTONE' /app/app/services/messages_zh.py)
  echo COLLISION=\$(grep -c 'name_collision' /app/app/domain/schemas.py)
  echo PERM=\$(grep -c '需 owner 部门成员或管理员' /app/app/services/messages_zh.py)"
# 期望：MSG=2  COLLISION=1  PERM=2

# ②-b worker 探针（无 worker 则跳过）：代码必须已换，**同时 CMD 必须仍是 worker**
docker exec qa-agent-orchestration-worker sh -c "
  echo MSG=\$(grep -c 'MSG_CLASS_NAME_OCCUPIED_BY_TOMBSTONE' /app/app/services/messages_zh.py)
  echo CMD=\$(cat /proc/1/cmdline | tr '\0' ' ')"
# 期望：MSG=2  CMD=python -m app.workers.agent_orchestration_worker
# ⚠️ 若 CMD 变成 uvicorn ⇒ 把 backend 镜像 tag 到了 worker 上但 compose 没生效（镜像名不符）
docker logs qa-agent-orchestration-worker --since 5m 2>&1 | tail -5

# ③ 前端探针（对照键 tombstonedAlert 必须仍 >0，否则说明 grep 打错文件）
docker exec qa-frontend sh -c '
  f=$(ls /usr/share/nginx/html/assets/index-*.js | head -1); echo bundle=$(basename $f)
  for k in tombstonedAlert tombstoneTableOnlyTag tombstoneTooltip; do
    printf "%s=%s\n" "$k" "$(grep -c "$k" $f)"; done'
# 期望：bundle=index-LixAJrlt.js  tombstonedAlert=3  tombstoneTableOnlyTag=3  tombstoneTooltip=3

# ④ 健康检查
curl -s -o /dev/null -w "backend=%{http_code}\n" http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "frontend=%{http_code}\n" http://localhost:5173/
```

浏览器侧**硬刷新**（`Ctrl+Shift+R`）后再验：本地导入 → 预览 → 选择一张已被软删的表，
应显示「已被软删除类占用」状态列 + 该行复选框置灰 + tooltip 点明 id / 删除时间 / 恢复与改名各自的权限。

---

## 7. Phase 6 回滚（任一探针不符即执行）

```bash
docker tag qa-backend:pre-1115        qa-backend:offline
docker tag qa-system_frontend:pre-1115 qa-system_frontend
docker image inspect worker:pre-1115 >/dev/null 2>&1 && docker tag worker:pre-1115 "$WORKER_IMAGE"
qa_compose stop backend frontend
qa_compose up -d backend frontend
qa_compose --profile agent-orchestration up -d --no-build --no-deps agent-orchestration-worker
```

回滚**不需要**动数据库：本批零 DDL、零业务写，库结构与数据均未改变。

---

## 8. 上线后行为变化（对用户可见）

| 位置 | 变化 |
|---|---|
| 导入预览 | 类名被占用（活类**或**墓碑）的行**复选框置灰**并标注占用者 id；仅源表相同的行**只提示不阻断** |
| 建类 / 改类报错 | 被墓碑占用时点名 id + 删除时间 + 两步权限（恢复 = owner 部门成员或管理员；改名 = 管理员） |
| 本体编辑 | 只改描述（类名原样回传）**不再**被「同名墓碑」误拦 |

**不改变**：不会自动清理任何墓碑；不提供「覆盖导入」；用户仍需「恢复后改名」或改用其他类名才能重导
（与部署前行为一致，只是提示变得可执行）。

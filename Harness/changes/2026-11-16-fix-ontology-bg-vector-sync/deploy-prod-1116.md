# 生产部署：本体向量后台同步修复（MissingGreenlet）

- **代码版本**：分支 `fix/ontology-bg-vector-sync` @ `584bc361`（base `origin/main @ 0b97d33c`）

> 🔴 **生产热修镜像 ≠ 仓库分支 HEAD**：镜像 `amd64-1116-vecsync` 构建自**热修专用提交**
> `5dd4ed86`（base `5ab1313c` = 生产现役 `10c79bb3` + 纯 docs 提交）⇒ **alembic head = 0143
> = 生产当前版本 ⇒ 启动迁移 no-op**。
> 仓库分支后来 rebase 到 `origin/main @ 0b97d33c`（含上游 P3a v2 双写 + **0145–0148 四个新迁移**）
> ⇒ **该分支的镜像不得用于本次生产热修**：起容器会自动执行 4 个新迁移
> （`0145` 加列/删列、`0146` 建 2 表 2 索引、`0147` 建不可变触发器、`0148` 改 CHECK 约束）
> ⇒ 有 DDL、有锁表风险，远超「零 DDL 热修」的承诺。
> 两条线的分工：**生产部署 = fix-only 镜像**；**仓库分支 = 代码归档与后续合并**。
> 0145–0148 的迁移需要**单独的发版批次**（含锁表评估与回填），不属于本次热修。
- **变更性质**：**零 DDL**、**零业务数据写**、**零新环境变量**、**仅后端**
- **迁移链**：head 仍是 `0143_kpi_time_grain`，与生产当前版本相同 ⇒ 启动时的
  `alembic upgrade head` 是 **no-op**
- **生产命令由宿主机执行**：开发机无法直连生产（192.168.205.82 内网隔离、无 SSH 免密通道），
  本文件是交付给生产宿主机执行的 runbook

> ⚠️ 本镜像基于 `main @ 5ab1313c`，**同时包含 PR #128（类墓碑占名）的后端修复**。
> 若生产已上过 1115（`amd64-1115-tombguard`），这对本次是重复内容，无副作用；
> 若生产仍停在 1114，本次会顺带把 #128 的后端部分带上——前端交互不变（未升级前端时
> 自动退化为第三态），见 `../2026-11-14-fix-import-tombstone-name-guard/deploy-prod-1115.md`。

---

## 1. 交付物（开发机产出）

| 文件 | 镜像 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1116-vecsync.tar.gz` | `qa-system-backend:amd64-1116-vecsync`（linux/amd64，源 `fix/ontology-bg-vector-sync @ 584bc361`） | `0ca1b1a8dccf092938a8cacfae6d052581f3465a07566a289fba2d22fd560d54` | 278M |

**不需要前端包**：本修复无任何用户界面变化。

**镜像侧核验（开发机已实测）**：见 §1.1。

### 1.1 镜像自检输出（2026-11-16 14:2x 开发机实测）

```
Arch=amd64/linux  ID=sha256:80964c359d444f49ec2d8210d2bca4c305cf7a3c5d7a8e76b0ece29772034e5a
Size=292620843 bytes（镜像）/ 278M（tar.gz）

$ docker run --rm --platform linux/amd64 --entrypoint sh qa-system-backend:amd64-1116-vecsync -c '…'
LOCK=5            # _deleteMilvusVectorUnderLock 出现 5 次（修复前镜像为 0）
LAZY=4
OWNSESSION=1
SCRIPT=yes        # scripts/ontology_background_sync_realdata.py 在
GATES=9           # 三道隔离闸门字符串
TOMBSTONE=2       # 含 PR #128 后端修复
ENVFILE=absent    # 镜像内无 /app/.env（不烤秘钥）
ALEMBIC_HEAD=0143_kpi_time_grain.py
```

---

## 2. Phase 1 生产预检（只读，不改动）

```bash
# ⓪ compose 入口 —— 从现役容器的 label 直接读，别靠 ls 猜
docker inspect qa-backend qa-frontend \
  --format '{{.Name}} => {{index .Config.Labels "com.docker.compose.project.config_files"}}'
# 2026-11-15 生产实测：/opt/qa-system/docker/docker-compose.centos7.yml（唯一权威）
# 生产只有 v1（docker-compose 1.29.2；v2 插件未装）⇒ 只用 centos7.yml，它不含 `!override`

qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK
qa_compose config --services

# ⚠️ 不要改 /opt/qa-system/docker/.env（SECRET_KEY 是 Fernet 主密钥，改了旧 token 全失效）

# ① 现役镜像名（回滚锚点以它为准，别照抄）
docker ps --format '{{.Names}}\t{{.Image}}' | grep -E 'backend|frontend|worker'

# ①-b 是否有 agent-orchestration-worker（复用同一 backend 镜像、CMD 不同）
#     存在则必须一起 retag，否则 worker 继续跑旧代码且日志全正常
docker ps --format '{{.Names}}\t{{.Image}}' | grep agent-orchestration-worker || echo NO_WORKER

# ② 迁移版本：期望 0143（与本镜像 head 相同 ⇒ 启动迁移 no-op）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'

# ③ 磁盘余量（包约 220M，load 后镜像层翻倍）
df -h /var/lib/docker

# ④ 部署前留证：生产是否还在报 MissingGreenlet（用于部署后对比）
docker logs qa-backend --since 24h 2>&1 | grep -c MissingGreenlet || true
```

**停下条件**：② 不是 `0143` ⇒ 停下核对镜像与库的对应关系，不要继续。

---

## 3. Phase 2 回滚锚点（只 tag 镜像，不碰数据库）

```bash
docker tag qa-backend:offline qa-backend:pre-1116
docker tag qa-system_frontend qa-system_frontend:pre-1116
# 若有 worker（①-b 不是 NO_WORKER）：用 ①-b 的实际镜像名
export WORKER_IMAGE=qa-system-agent-orchestration-worker
docker image inspect "$WORKER_IMAGE" >/dev/null 2>&1 && docker tag "$WORKER_IMAGE" worker:pre-1116
docker images | grep -E 'qa-backend|qa-system_frontend|worker'
```

> 本批**零 DDL、零业务写** ⇒ 不需要 `pg_dump`。

---

## 4. Phase 3 传输 + 校验 + 加载 + tag

传输（scp / U 盘均可），**先比对 sha256**：

```bash
shasum -a 256 qa-backend-amd64-1116-vecsync.tar.gz    # 开发机
sha256sum  qa-backend-amd64-1116-vecsync.tar.gz       # 生产（应与 §1.1 一致）
```

生产：

```bash
docker load -i qa-backend-amd64-1116-vecsync.tar.gz

docker tag qa-system-backend:amd64-1116-vecsync qa-backend:offline
# 若有 worker（与 backend 同源镜像，仅 CMD 不同）
docker image inspect "$WORKER_IMAGE" >/dev/null 2>&1 && \
  docker tag qa-system-backend:amd64-1116-vecsync "$WORKER_IMAGE"

qa_compose config | grep image       # 确认解析到新 tag
```

---

## 5. Phase 4 切换

```bash
qa_compose stop backend
qa_compose up -d backend

# 若有 worker：必须 --no-build（否则现场 build）+ --no-deps（防止顺带重启中间件）
qa_compose --profile agent-orchestration up -d --no-build --no-deps agent-orchestration-worker
```

> `Dockerfile.backend` 的 CMD = `alembic upgrade head && uvicorn …`。
> 本次 head = `0143` = 生产当前版本 ⇒ **no-op**，不会 DDL、不会锁表。
> 前端不需要动。

---

## 6. Phase 5 验收探针

```bash
# ① 后端已起（等 20~40s，启动要跑 seed）
docker logs qa-backend --since 5m 2>&1 | tail -5
# 期望：Application startup complete. / Uvicorn running on http://0.0.0.0:8000

# ② 代码探针：全部从 0 变正数才算生效
docker exec qa-backend sh -c "
  echo LOCK=\$(grep -c '_deleteMilvusVectorUnderLock' /app/app/services/ontology_service.py)
  echo LAZY=\$(grep -c 'lazyload' /app/app/services/ontology_service.py)
  echo OWNSESSION=\$(grep -c 'def ownSession' /app/app/infrastructure/database.py)"
# 期望：LOCK=5  LAZY=4  OWNSESSION=1

# ②-b worker 探针（无 worker 跳过）：代码已换 + CMD 必须仍是 worker
docker exec qa-agent-orchestration-worker sh -c "
  echo LOCK=\$(grep -c '_deleteMilvusVectorUnderLock' /app/app/services/ontology_service.py)
  echo CMD=\$(cat /proc/1/cmdline | tr '\0' ' ')"
# 期望：LOCK=5  CMD=python -m app.workers.agent_orchestration_worker

# ③ 健康检查
curl -s -o /dev/null -w "backend=%{http_code}\n" http://localhost:8000/api/v1/health

# ④ 新增日志中不应再出现 MissingGreenlet（与 Phase 1 ④ 的数量对比）
docker logs qa-backend --since 10m 2>&1 | grep -c MissingGreenlet || true
```

**功能面观察（非阻塞）**：编辑任意一个本体类/属性的描述并保存 ⇒
`docker logs qa-backend --since 1m 2>&1 | grep -i 'milvus\|embedding'` 不应出现
`MissingGreenlet` 或 `greenlet_spawn`。若失败也会是正常的 warning 日志，不影响保存接口返回。

---

## 7. Phase 6 回滚（任一探针不符即执行）

```bash
docker tag qa-backend:pre-1116 qa-backend:offline
docker tag qa-system_frontend:pre-1116 qa-system_frontend
docker image inspect worker:pre-1116 >/dev/null 2>&1 && docker tag worker:pre-1116 "$WORKER_IMAGE"
qa_compose stop backend
qa_compose up -d backend
qa_compose --profile agent-orchestration up -d --no-build --no-deps agent-orchestration-worker
```

回滚**不需要**动数据库：零 DDL、零业务写。

---

## 8. 上线后行为变化与注意事项

| 位置 | 变化 |
|---|---|
| 本体类/属性/指标保存 | 请求失败（回滚）后，已登记的后台向量同步**不再抛 `MissingGreenlet`**；原异常也不再被覆盖 |
| 连续编辑同一实体 | Milvus 最终态携带**最后一次已提交**的文本，不再出现旧描述后写覆盖 |
| 删除实体 | 在途的向量写入不会再把已删实体的向量写回（不会"复活"） |
| 用户界面 | **无变化** |

**注意事项**：

1. **不补历史欠账**：本修复只保证"新写入正确"。历史上已产生的孤儿向量 / 缺失向量不会自动修复——
   写钩子只在 create/update/delete 时触发。如需对账，另开只读核对任务，不在本批范围。
2. **重启窗口**：重启 backend 会终止在途的后台同步任务（`asyncio.create_task` 随进程消失）⇒
   重启瞬间正在同步的实体可能落后一次；管理员再次编辑该实体即会补上。
3. **不要在生产执行** `backend/scripts/ontology_background_sync_realdata.py`：它是开发/测试环境的
   取证脚本，会创建并删除真实 PG/Neo4j/Milvus 数据（三道隔离闸门只放行测试库与 qa_test Milvus）。
4. **worker 必须一起换**：worker 与 backend 同镜像；只换 backend 会让 worker 继续跑旧代码，
   且健康检查与日志全正常、不报错。

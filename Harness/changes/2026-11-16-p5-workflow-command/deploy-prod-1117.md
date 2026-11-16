# 生产部署：P5 工作流 / Command / 审批（默认关闭上线）

- **代码版本**：`feat/workflow-command` 合并后的 `main @ <<MERGE_SHA>>`（PR #<<PR_NO>>）
  ⚠️ **发布前必须把 `<<MERGE_SHA>>` / `<<PR_NO>>` / `<<IMAGE_TAG>>` 补成实际值**
  （镜像必须构建自**合并后的 main**，不能构建自分支头：分支头不含后续 rebase 的内容）
- **变更性质**：**有 DDL**（4 个迁移：0149–0152）＋ 新增 1 个 compose 服务（Worker）
  ＋ 新增 1 个开关（`ENABLE_WORKFLOW`，**默认关**）
- **迁移链**：仓内 head `0152`；生产必须**先**在 `0148`（见 §1 前置）
- **生产命令由宿主机执行**：开发机无法直连生产（192.168.205.82 内网隔离、无 SSH 免密通道），
  本文件是交付给生产宿主机执行的 runbook
- **不改** `/opt/qa-system/docker/.env` 的任何**既有行**（`SECRET_KEY` = Fernet 主密钥，
  改了旧 token 全失效）；开启开关时只**追加**一行

---

## 0. 这次上线是什么 / 不是什么

| | 内容 |
|---|---|
| **是** | ① 4 张工作流表族（`workflow_definition` / `workflow_instance` / `workflow_step` / `business_command` / `approval_task` / `workflow_outbox` 共 6 张）② `audit_log.action` CHECK 放宽（新增 `APPROVE` / `REJECT` / `COMPENSATE` / `WORKFLOW_*`）③ 一个独立进程 Worker（默认**不启动**）④ 开关默认关 ⇒ `/api/v1/workflow/*` 路由 **404** |
| **不是** | ① 真实 ERP/SRM 写入 —— Worker 只注册了**模拟适配器**（`SimulatedCommandHandler`，只回显、不写外部系统）② 不新增中间件、不新增镜像（Worker **复用 backend 现役镜像**）③ 不改既有镜像版本 ④ 不动前端 |

⇒ **风险面**：本批上生产后，**默认没有任何新能力对外可见**（路由 404、无 Worker）。
真正的能力在 §7「开启」之后才生效，且即便生效，写入也只落在 P5 自己的表里。

---

## 1. 前置条件（**任一不满足 ⇒ 停下**）

| # | 前置 | 怎么查 | 不满足怎么办 |
|---|---|---|---|
| 1 | **生产 alembic 版本 = `0148`** | §3 ② | 先发「迁移链收口」批次（`0144`–`0148`），**本批不得连跑 9 个迁移** |
| 2 | **部署线空闲** | 与发版负责人确认 | 等 1114 / 1116 收口 |
| 3 | **生产鉴权是真鉴权** | §3 ③ | `AUTH_MODE` 必须是 `real` 且 `AUTH_STUB_ENABLED=0`；否则**不得开启**开关（见 §7 硬停条件） |
| 4 | 备份可用（pg_dump 落盘 + 校验） | §4 | 无备份 ⇒ 不执行 §5 之后的任何步骤 |
| 5 | 迁移已在**测试库**演练过（upgrade + downgrade 往返） | CI / 本地 `qa_metadata_test_p5` | 未演练 ⇒ 不上线 |

> ⚠️ **前置 1 的来历**：P5 的迁移接在 `0148` 之后（`0149`–`0152`），而生产现在停在
> `0143`。若直接上 P5 镜像，一次要跑 `0144`–`0152` 共 **9 个**迁移，其中 `0145`（加列/删列）、
> `0146`（建 2 表 2 索引）、`0147`（建不可变触发器）、`0148`（改 `audit_log` CHECK）
> 都有 DDL / 锁表风险 —— 超出本批的评估口径。**先收口 0144–0148，再上 P5。**

---

## 2. 交付物（开发机产出）

| 文件 | 说明 |
|---|---|
| `qa-backend-amd64-1117-p5.tar.gz` | **backend 镜像**（linux/amd64，源 = 合并后的 main）；Worker **复用同一镜像**（只换 CMD）⇒ 不新增镜像 |
| `compose.workflow-worker.centos7.fragment.yml` | **Worker 的 compose 服务片段**（仓内本目录，v1 兼容写法） |
| `deploy-prod-1117.md` | 本文件（生产操作手册） |

### 2.1 镜像构建（**必须在联网 x86_64 构建机**上做）

Mac 是 arm64、生产内网隔离 ⇒ 两者都构建不出能跑的镜像。

```bash
# 联网 x86_64 构建机
git clone <repo> qa-p5 && cd qa-p5
git checkout <<MERGE_SHA>>
docker build --platform linux/amd64 -f docker/Dockerfile.backend \
  -t qa-system-backend:amd64-1117-p5 .
docker save qa-system-backend:amd64-1117-p5 | gzip > qa-backend-amd64-1117-p5.tar.gz
sha256sum qa-backend-amd64-1117-p5.tar.gz > qa-backend-amd64-1117-p5.tar.gz.sha256
```

**镜像自检（必做，缺一不放行）**：

```bash
docker run --rm --platform linux/amd64 --entrypoint sh qa-system-backend:amd64-1117-p5 -c '
  echo ALEMBIC_HEAD=$(ls /app/alembic/versions | grep -c "^0152_")
  echo WORKER=$(test -f /app/app/workers/workflow_worker.py && echo yes || echo no)
  echo TABLES=$(grep -c "__tablename__" /app/app/domain/workflow_models.py)
  echo CMD=$(cat /proc/1/cmdline | tr "\0" " ")
  echo ENVFILE=$(test -f /app/.env && echo present || echo absent)
'
```

期望：`ALEMBIC_HEAD=1` `WORKER=yes` `TABLES=6` `CMD=sh -c alembic upgrade head && uvicorn …` `ENVFILE=absent`

---

## 3. Phase 1 生产预检（只读，不改动）

```bash
# ⓪ compose 入口 —— 从现役容器的 label 直接读，别靠 ls 猜
docker inspect qa-backend \
  --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
# 期望：/opt/qa-system/docker/docker-compose.centos7.yml（唯一权威）

qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK
qa_compose config --services        # 期望 9 个服务，且**不含** workflow-worker

# ① 现役镜像名（回滚锚点以它为准，别照抄）
docker ps --format '{{.Names}}\t{{.Image}}' | grep -E 'backend|frontend'

# ② 迁移版本 —— **本批的硬前置**
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'
# 期望：0148（不是 0148 ⇒ 停下，见 §1 前置 1）

# ③ 鉴权（决定能不能开开关，见 §1 前置 3）
grep -E '^(AUTH_MODE|AUTH_STUB_ENABLED)=' /opt/qa-system/docker/.env
# 期望：AUTH_MODE=real 且 AUTH_STUB_ENABLED=0

# ④ 磁盘余量（镜像 load 后层翻倍）
df -h /var/lib/docker

# ⑤ 部署前留证：工作流相关表应**不存在**（本批才建）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT count(*) FROM information_schema.tables \
   WHERE table_name IN (\x27workflow_definition\x27,\x27workflow_instance\x27,\x27workflow_step\x27,\
   \x27business_command\x27,\x27approval_task\x27,\x27workflow_outbox\x27);"'
# 期望：0
```

> ⚠️ **不要改** `/opt/qa-system/docker/.env` 的**既有行**（尤其 `SECRET_KEY`）。

---

## 4. Phase 2 备份（**有 DDL ⇒ 硬前置**）

```bash
# 备份整库（推荐：恢复时不用拼对象）
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > /opt/qa-transfer/qa_metadata_pre-1117.dump
ls -lh /opt/qa-transfer/qa_metadata_pre-1117.dump
sha256sum /opt/qa-transfer/qa_metadata_pre-1117.dump
```

**停下条件**：备份文件为空 / 体积异常小（< 1MB）⇒ 停下，先修备份。

> 本批只**新增表**与**放宽 CHECK 约束**，不改既有列 ⇒ 整库备份已足够；
> 若只备份受影响对象，可用 `pg_dump -t workflow_* -t business_command -t approval_task`。

---

## 5. Phase 3 回滚锚点（只 tag 镜像，不碰数据库）

```bash
docker tag qa-backend:offline qa-backend:pre-1117
docker images | grep qa-backend
```

> 回滚锚点 = 切换前**正在跑**的镜像。核对：
> `docker inspect qa-backend --format '{{.Image}}'` 应等于
> `docker image inspect qa-backend:pre-1117 --format '{{.Id}}'`。

---

## 6. Phase 4 传输 + 校验 + 加载 + tag

```bash
# 开发机
shasum -a 256 qa-backend-amd64-1117-p5.tar.gz
# 生产（应与开发机一致）
sha256sum qa-backend-amd64-1117-p5.tar.gz

docker load -i qa-backend-amd64-1117-p5.tar.gz
docker tag qa-system-backend:amd64-1117-p5 qa-backend:offline
```

---

## 7. Phase 5 切换 —— **默认关闭**上线

> 本阶段**只换 backend 镜像**（跑迁移 0149–0152），**不追加 Worker 片段、不改 .env**
> ⇒ 工作流路由仍然 404、没有 Worker ⇒ 与上线前对外表现一致。

```bash
qa_compose stop backend
qa_compose up -d --no-build --no-deps backend
```

> ⚠️ `Dockerfile.backend` 的 CMD = `alembic upgrade head && uvicorn …`
> ⇒ **本次启动会执行 `0149`–`0152` 四个迁移**（建 6 张表 + 放宽 `audit_log` CHECK）
> ⇒ 这是本批**唯一**的 DDL 时刻 ⇒ §4 的备份必须已经在手。

---

## 8. Phase 6 验收探针（默认关闭状态）

```bash
# ① backend 已起（启动要跑 seed，等 20~40s）
docker logs qa-backend --since 5m 2>&1 | tail -5
# 期望：Application startup complete. / Uvicorn running on http://0.0.0.0:8000

# ② 迁移已到 0152
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'
# 期望：0152

# ③ 6 张表已建
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT count(*) FROM information_schema.tables \
   WHERE table_name IN (\x27workflow_definition\x27,\x27workflow_instance\x27,\x27workflow_step\x27,\
   \x27business_command\x27,\x27approval_task\x27,\x27workflow_outbox\x27);"'
# 期望：6

# ④ audit_log CHECK 已放宽（PG 把 IN 渲染成 = ANY(ARRAY[...])，先抠出值列表再比）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT pg_get_constraintdef(oid) FROM pg_constraint \
   WHERE conrelid = \x27audit_log\x27::regclass AND conname = \x27ck_audit_log_action\x27;"'
# 期望：值列表里含 APPROVE / REJECT / COMPENSATE / WORKFLOW_SUBMIT 等

# ⑤ 开关默认关 ⇒ 路由 404（对外完全不存在）
curl -s -o /dev/null -w "workflow=%{http_code}\n" http://localhost:8000/api/v1/workflow/instances
# 期望：404

# ⑥ 后端健康
curl -s -o /dev/null -w "health=%{http_code}\n" http://localhost:8000/api/v1/health
# 期望：200

# ⑦ 没有 Worker（默认不启动）
docker ps --format '{{.Names}}' | grep -c qa-workflow-worker || echo NO_WORKER
```

**本阶段到此为止 = G6「默认关闭可上线」已达成。** 能力开启是 §9 的**独立动作**。

---

## 9. Phase 7 开启（**独立窗口，单独决策**）

### 9.1 硬停条件

| 检查 | 要求 | 不满足 |
|---|---|---|
| `AUTH_MODE` | 必须是 `real` | **不得开启**（stub 下 `X-User-Roles` 可伪造 ⇒ 任何客户端都能冒充审批人放行写操作） |
| `AUTH_STUB_ENABLED` | 必须是 `0` | 同上 |
| 业务确认 | 审批角色已在 RBAC 里配好（矩阵里的 `approverRole`） | 先配角色再开 |

### 9.2 追加 Worker 片段（**必须插到 `services:` 之下**）

⚠️ **绝不能 `cat fragment >> compose.yml`**（追加到文件末尾）：生产 compose 最后一个顶层键是
`volumes:` ⇒ 片段会落进 `volumes:` 里 ⇒ compose 报
`volumes.workflow-worker additional properties ... not allowed`（**片段必须挂在 `services:` 下**）。

```bash
# ① 先把片段传到生产（仓内本目录）
#    scp compose.workflow-worker.centos7.fragment.yml \
#        root@<prod>:/opt/qa-system/docker/

# ② 备份 compose 文件
cp -a /opt/qa-system/docker/docker-compose.centos7.yml \
      /opt/qa-system/docker/docker-compose.centos7.yml.bak-1117

# ③ 幂等检查（已插入过就跳过，别插两次）
grep -c 'workflow-worker:' /opt/qa-system/docker/docker-compose.centos7.yml

# ④ 插入：把片段内容插到 `services:` 行**之后**（awk，CentOS 7 的 gawk 4.0.2 支持）
awk -v frag=/opt/qa-system/docker/compose.workflow-worker.centos7.fragment.yml '
  BEGIN { while ((getline line < frag) > 0) ins = ins line "\n" }
  /^services:/ && !done { print; printf "%s", ins; done = 1; next }
  { print }
' /opt/qa-system/docker/docker-compose.centos7.yml > /tmp/centos7.new.yml

# ⑤ **先验证再落盘**
docker-compose -p qa-system --env-file /opt/qa-system/docker/.env \
  -f /tmp/centos7.new.yml config --quiet && echo COMPOSE_OK
cp /tmp/centos7.new.yml /opt/qa-system/docker/docker-compose.centos7.yml
qa_compose config --quiet && echo COMPOSE_OK
qa_compose config --services | grep workflow-worker    # 期望：workflow-worker
```

### 9.3 打开 API 侧开关（**只追加一行**）

```bash
# ⚠️ 只**追加**，不改任何既有行（SECRET_KEY 是 Fernet 主密钥）
grep -q '^ENABLE_WORKFLOW=' /opt/qa-system/docker/.env \
  || echo 'ENABLE_WORKFLOW=true' >> /opt/qa-system/docker/.env
```

### 9.4 起服务

```bash
# backend 重建（让它看到新的 env）⇒ CMD 会再跑一次 alembic（此时是 no-op）
qa_compose up -d --no-build --no-deps backend

# Worker 起（--no-build 防止现场构建；--no-deps 防止顺带重启中间件）
qa_compose up -d --no-build --no-deps workflow-worker
```

---

## 10. Phase 8 开启后的验收探针

```bash
# ① 路由已注册（未带 token ⇒ 403 而不是 404 ⇒ 证明开关已生效）
curl -s -o /dev/null -w "workflow=%{http_code}\n" http://localhost:8000/api/v1/workflow/instances
# 期望：403（real 模式无 Bearer ⇒ PermissionDeniedError ⇒ 403），**不再是 404**

# ② Worker 在跑
docker ps --format '{{.Names}}\t{{.Status}}' | grep qa-workflow-worker
docker logs qa-workflow-worker --since 5m 2>&1 | grep -E '工作流 worker 启动|轮询失败'
# 期望：有「工作流 worker 启动」，**没有**「轮询失败」

# ③ Worker 的 CMD 必须是 worker（不是 uvicorn）
docker exec qa-workflow-worker sh -c 'cat /proc/1/cmdline | tr "\0" " "'
# 期望：python -m app.workers.workflow_worker

# ④ Worker 与 backend 是**同一镜像**（避免出现「worker 跑旧代码」）
docker inspect qa-backend qa-workflow-worker --format '{{.Name}} => {{.Image}}'
# 期望：两个容器的 image digest 相同

# ⑤ 无鉴权告警（说明 §9.1 前提成立）
docker logs qa-backend --since 10m 2>&1 | grep -c 'ENABLE_WORKFLOW=true 但鉴权仍为 stub'
# 期望：0
```

---

## 11. Phase 9 回滚

### 11.1 L1 快速回滚（**秒级，不动数据库**）—— 首选

```bash
# ① 停 Worker（**必须**停：开关关时 worker 立即退出，`restart: unless-stopped` 会反复拉起）
qa_compose stop workflow-worker && qa_compose rm -f workflow-worker

# ② 关 API 侧开关并重建 backend
sed -i '/^ENABLE_WORKFLOW=/d' /opt/qa-system/docker/.env
qa_compose up -d --no-build --no-deps backend

# ③ 验证：路由回到 404
curl -s -o /dev/null -w "workflow=%{http_code}\n" http://localhost:8000/api/v1/workflow/instances
# 期望：404
```

### 11.2 L2 代码回滚（回到上一版镜像）

```bash
docker tag qa-backend:pre-1117 qa-backend:offline
qa_compose stop backend && qa_compose up -d --no-build --no-deps backend
# Worker 若还在跑，也要一起换（同一镜像 ⇒ 否则跑旧代码且日志全正常）
qa_compose up -d --no-build --no-deps workflow-worker
```

### 11.3 L3 数据回滚（**一般不需要**）

本批只新增表 + 放宽 CHECK ⇒ 回滚代码后**旧表留着无害**（空表 + 旧代码不读它们）。
确需清理时（**会丢数据，慎用**）：

```bash
docker exec qa-backend sh -c 'alembic downgrade 0148'   # 由新镜像执行 ⇒ 撤掉 0149–0152
```

> ⚠️ `downgrade` 会 **DROP** 6 张表 ⇒ 之后 P5 的全部数据消失。
> 计划 G6 判据里的「`downgrade()` 可独立撤掉本批迁移」指的是**能力存在**，不是**默认动作**。

---

## 12. 坑位清单（生产实测/推导，勿踩）

| # | 坑 | 后果 | 处置 |
|---|---|---|---|
| 1 | `cat fragment >> compose.yml`（追加到末尾） | 片段落进顶层 `volumes:` ⇒ compose 报 `additional properties not allowed` | 用 §9.2 的 awk 插入法（插到 `services:` 之后） |
| 2 | 用 `docker compose`（v2 插件） | 生产**没装** v2 插件 ⇒ `command not found` | 一律 `docker-compose`（连字符） |
| 3 | 片段里写 `!override` / `profiles:` | v1 解析即炸 / **静默忽略** ⇒ 以为「默认不启动」其实开关失效 | 片段已禁用；仓内守卫 `test_workflow_deploy_fragment.py` 钉死 |
| 4 | 只关开关、不停 Worker | worker 立即退出 ⇒ `restart: unless-stopped` 反复拉起 ⇒ 空转日志 | §11.1：先停容器再关开关 |
| 5 | 只换 backend 镜像、不重建 Worker | Worker 跑旧代码，**日志与健康检查全正常** | §10 ④ 的同镜像校验；每次发版都要 `up -d workflow-worker` |
| 6 | 在 stub 鉴权环境开 `ENABLE_WORKFLOW` | 任意客户端伪造 `X-User-Roles` 冒充审批人 | §9.1 硬停条件；backend 启动也会打 `🚨` 告警 |
| 7 | 改 `.env` 里的 `SECRET_KEY` | 旧 token 全部失效 | 只**追加** `ENABLE_WORKFLOW`，不碰既有行 |
| 8 | 生产 alembic 还没到 `0148` 就上本批 | 一次连跑 9 个迁移（含加列/删列/建触发器） | §1 前置 1；先发迁移链收口批次 |
| 9 | `up -d` 不带 `--no-build --no-deps` | 现场构建 / 顺带重启中间件 | 全部 `up -d --no-build --no-deps <service>` |
| 10 | 迁移与代码不同批 | 旧代码遇到新表/新约束 ⇒ 运行期报错 | D-10：迁移与代码**同一个镜像**上线 |

---

## 13. 上线后行为变化与注意事项

| 位置 | 变化 |
|---|---|
| 工作流 API | 开关关：**404**（对外完全不存在）；开关开：需要真鉴权（无 Bearer ⇒ 403） |
| 数据库 | 新增 6 张空表；`audit_log.action` 的 CHECK 白名单变宽（**旧值全部仍可用**） |
| 后台进程 | 新增 1 个独立 Worker 容器（默认不启动）；它**只**写 P5 自己的表（模拟适配器不写外部系统） |
| 用户界面 | **无变化** |

**注意事项**：

1. **开关是分体的**：worker 侧在 compose 片段里（`environment`），API 侧在 `.env`。
   只开一边 ⇒ worker 空转 / 路由 404，但都不会写错数据。
2. **审批角色要先配**：矩阵里的 `approverRole`（如 `purchase_manager`）必须在 RBAC 里真实存在，
   否则审批任务永远没人能批（到期被清扫为 TIMEOUT）。
3. **审批超时**：默认 24 小时（矩阵里的 `timeoutHours`）⇒ 实例会卡在「等待审批」直到清扫。
4. **本批不含真实 ERP/SRM 写入**：Worker 只注册了 `po.write` 的**模拟**适配器
   ⇒ 即使开关全开，也不会有任何外部系统被写。
5. **每次发版都要重建 Worker**：它与 backend 同一镜像 ⇒ retag 不会自动更新已运行的容器。

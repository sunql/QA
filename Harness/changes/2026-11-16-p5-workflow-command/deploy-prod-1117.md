# 生产部署：P5 工作流 / Command / 审批（默认关闭上线）

- **代码版本**（占位符已回填，均为 `main` 上的**合并后**提交）：
  - **窗口②（P5 本体 + 前端）**：backend `main @ 1e04b46e`（PR #150，内含 PR #148 的 P5 后端）·
    frontend 同源 `1e04b46e`
  - **窗口①（迁移链收口 0143→0148）**：backend `main @ 86ff4a1f`（PR #143）—— **见 §0.2**
  - ⚠️ 镜像必须构建自**合并后的 main**，不能构建自分支头（分支头不含后续 rebase 的内容）
- **镜像 tag**：backend `qa-system-backend:amd64-1117-p5` ·
  frontend `qa-system-frontend:amd64-1117-p5`（窗口① 另用 `qa-system-backend:amd64-1117-mig0148`）
- **变更性质**：**有 DDL**（4 个迁移：0149–0152）＋ 新增 1 个 compose 服务（Worker）
  ＋ 新增 1 个开关（`ENABLE_WORKFLOW`，**默认关**）
  ＋ **前端有变化**（3 个页面 + 3 个菜单项 + i18n）⇒ 需**同步发前端镜像**（见 §2.2 / §7.1）
- **迁移链**：仓内 head `0152`；生产必须**先**在 `0148`（见 §1 前置 1 与 §0.2）
- **生产命令由宿主机执行**：开发机无法直连生产（192.168.205.82 内网隔离、无 SSH 免密通道），
  本文件是交付给生产宿主机执行的 runbook
- **不改** `/opt/qa-system/docker/.env` 的任何**既有行**（`SECRET_KEY` = Fernet 主密钥，
  改了旧 token 全失效）；开启开关时只**追加**一行

---

## 0. 这次上线是什么 / 不是什么

| | 内容 |
|---|---|
| **是** | ① **6 张**工作流表（`workflow_definition` / `workflow_instance` / `workflow_step` / `business_command` / `approval_task` / `workflow_outbox`）② `audit_log.action` CHECK 放宽（新增 **7 个**动作：`APPROVE` / `REJECT` / `COMPENSATE` / `WORKFLOW_SUBMIT` / `WORKFLOW_APPROVAL_REQUESTED` / `WORKFLOW_TIMEOUT` / `WORKFLOW_CANCEL`）③ 一个独立进程 Worker（默认**不启动**）④ 开关默认关 ⇒ `/api/v1/workflow/*` 路由 **404** ⑤ **前端同步发**：3 个新页面（`/workflow` 实例+提交、`/workflow/approvals` 待办、`/workflow/definitions` 定义管理）+ 3 个菜单项 + i18n |
| **不是** | ① 真实 ERP/SRM 写入 —— Worker 只注册了**模拟适配器**（`SimulatedCommandHandler`，只回显、不写外部系统）② 不新增中间件、Worker **复用 backend 现役镜像**（不新增镜像）③ 不改既有镜像版本 ④ 不改既有业务页面（前端只**新增**页面与菜单项） |

⇒ **风险面**：本批上生产后，**默认没有任何新能力对外可见**（路由 404、无 Worker）。
真正的能力在 §9「开启」之后才生效，且即便生效，写入也只落在 P5 自己的表里。

> ⚠️ **前端不再是「无变化」**：旧版 runbook 写「不动前端」，那是**只交付后端时**的说法。
> 现在两批前端已合入 `main @ 1e04b46e`（PR #150）⇒ 窗口② **必须同时发前端镜像**，
> 否则菜单 seed 出来的 3 个入口点了会 404（前端路由已存在、后端路由 404）。
> 详见 §2.2（构建）与 §7.1（切换）。

---

## 0.1 W5 / R11 收口带来的行为变化（**开启开关后才会看到**）

本批实现期间补上了「命令没有生产调用方 ⇒ Worker 永远空转」的缺口，
因此**开启开关后**行为与最初规划有出入，**上线前必读**：

| 变化 | 说明 |
|---|---|
| **提交实例即入队命令** | 定义里带 `commandType` 的步骤，**提交时就落一条 `business_command`（PENDING）** ⇒ Worker 会领走它。之前是「提交了但没人写」，现在是「提交了就会走命令」。 |
| **未注册的 `commandType` 会失败关闭** | Worker 只注册了 **`po.write`**（模拟适配器）。定义里写了别的 `commandType` ⇒ `UnknownCommandTypeError` ⇒ 命令终态 FAILED ⇒ **触发实例补偿**（实例 → COMPENSATED）⇒ **不是**静默挂起。 |
| **步骤定义必须「有活儿」** | 一步**必须**有 `commandType` **或** `requiresApproval=true`；两者皆无 ⇒ **定义解析期就拒绝**（422），不会创建实例。 |
| **新增取消路由** | `POST /api/v1/workflow/instances/{instanceId}/cancel` ⇒ 实例 CANCELLED、待审批 CANCELLED、**待执行命令 CANCELLED**（Worker 不再领走）、未开始的步骤 SKIPPED。权限：提交人本人或**真实** admin。 |
| **命令状态多了 `CANCELLED`** | `business_command.status` 的 CHECK 多了 `CANCELLED`（终态，不可领取）。 |
| **终态失败落 `ABORTED`**（R11） | 不可重试 / 尝试次数耗尽的命令落 **`ABORTED`**（终态，Worker 领不走）。⇒ 巡检读法：`FAILED` = **还会重试**（可重试失败），`ABORTED` = **不会再试**（终态失败）。看到 `ABORTED` 不是新故障，是终态失败的正常落点。 |
| **审计动作 7 个** | `audit_log.action` 新增 `WORKFLOW_CANCEL`（取消也留痕）。 |

> ⚠️ **取消权限的真实身份要求**：stub 鉴权下 `X-User-Roles` 可伪造 ⇒
> 跨用户取消要求 `dbUserId` 来自 DB / JWT；**自取消（提交人本人）** 不要求。
> 这与 §9.1 的硬停条件（必须真鉴权）方向一致。

---

## 0.2 窗口划分与「迁移链收口」的**唯一正确源**（**别选错 commit**）

生产当前停在 **0143**，且现役镜像是**热修专用镜像**（构建自 `5dd4ed86`，base `5ab1313c`
⇒ head `0143` ⇒ 启动迁移 **no-op**、零 DDL）。P5 的迁移接在 `0152`，直接上 ⇒ 一次连跑
`0144`–`0152` 共 9 个迁移（其中 `0145` 加列/删列、`0146` 建 2 表 2 索引、`0147` 建不可变触发器、
`0148` 改 `audit_log` CHECK ⇒ 有 DDL / 锁表风险）⇒ **超出本批评估口径**。
⇒ 拆成两个窗口：

| 窗口 | 目的 | 镜像源（**main 上的合并后提交**） | 迁移 | 镜像 tag |
|---|---|---|---|---|
| **① 迁移链收口** | 把生产从 `0143` 推到 `0148` | **`86ff4a1f`**（PR #143） | `0144`–`0148`（**5 个**：`0144`→`0145`→`0146`→`0147`→`0148`） | `qa-system-backend:amd64-1117-mig0148` |
| **② P5 本体 + 前端** | P5 的 4 个迁移 + 两批前端 | **`1e04b46e`**（PR #150，含 PR #148 的后端） | `0149`–`0152`（4 个） | `qa-system-backend:amd64-1117-p5` + `qa-system-frontend:amd64-1117-p5` |
| ③ 开启 | `ENABLE_WORKFLOW=true` + Worker | — | 无 | — |

### 🔴 窗口① 的源**只能是 `86ff4a1f`**（用别的会把生产热修回退掉）

`main` 上 head = `0148` 的提交**不止一个**，但**只有 `86ff4a1f` 同时含本体向量热修**
（`ontology_service.py` 的 `ownSession` 自开会话 = `MissingGreenlet` 生产事故的修复）：

| main 提交 | alembic head | 含 `ownSession` 修复 | 能不能做窗口① 的源 |
|---|---|---|---|
| `75a435a3`（#144） | 0148 | ❌ | ❌ 会回退生产热修 |
| `68398378`（#145） | 0148 | ❌ | ❌ 会回退生产热修 |
| `73b03fed`（排期规划） | 0148 | ❌ | ❌ 会回退生产热修 |
| **`86ff4a1f`（#143）** | **0148** | **✅** | ✅ **唯一正确源** |

> 取证：`git show <sha>:backend/app/services/ontology_service.py | grep -c ownSession`
> ⇒ `86ff4a1f` = 8；`68398378` / `75a435a3` = 0。
> 判据不是「哪个新」而是「**既到 0148、又保住热修**」⇒ 只认 `86ff4a1f`（或其后**仍以 0148
> 为 head** 的 main 提交）。
>
> ⚠️ **不要用 rebase 分支镜像**：热修分支的 rebase 版 base 是 `0b97d33c`，**自带 0144–0148**
> （取证：该 commit 下 `backend/alembic/versions/` 已有 `0144`–`0148` 五个文件，链为
> `0143→0144→0145→0146→0147→0148`）
> ⇒ 一次把窗口① 的 **5 个**迁移全跑了 ⇒ 绕开了本 runbook 的分窗口评估（这正是当初
> 「热修镜像 ≠ 分支 HEAD」要防的事）。

### 0.3 两个窗口的**执行顺序**（谁先谁后）

> 窗口① 会跑 DDL（`0144`–`0148`）⇒ **必须在 §4 备份之后**；两个窗口各自换一次 backend 镜像。

| 步 | 动作 | 章节 | 做完的判据 |
|---|---|---|---|
| 1 | 备份落盘 + 校验 | §4 | `qa_metadata_pre-1117.dump` 非空、sha256 已记 |
| 2 | 打回滚锚点（**窗口① 前就打**） | §5 | `qa-backend:pre-1117` = 切换前正在跑的镜像 |
| 3 | **窗口①**：加载 mig0148 镜像 → tag → 只切 backend | §6 / §7 | `alembic version` = **0148**；热修仍在（`ownSession` 生效） |
| 4 | **窗口②**：加载 p5 镜像 + 前端镜像 → tag → 切 backend | §6 / §7 | `alembic version` = **0152**；6 张工作流表存在 |
| 5 | **窗口②**：切前端 | §7.1 | 前端容器起来、§8 ⑨⑩ 探针通过 |
| 6 | 验收探针（默认关闭态） | §8 | 全部符合期望 |
| 7 | 开启（`ENABLE_WORKFLOW` + Worker）—— **独立决策** | §9 | 真实审批人可走完整流程 |

> 窗口① 与窗口② 的**镜像 tag 名不同**（`amd64-1117-mig0148` vs `amd64-1117-p5`），
> 但生产 compose 里 backend 引用的是**同一个 image 名**（现役为 `qa-backend:offline`）⇒
> 每次切换前都要把对应镜像 tag 成那个名字（见 §6）。
>
> ⚠️ 窗口① 的回滚：若 `0144`–`0148` 出问题 ⇒ `alembic downgrade 0143`（**用窗口① 的镜像**）
> ＋ 把 `qa-backend:offline` tag 回 `qa-backend:pre-1117`。

---


## 1. 前置条件（**任一不满足 ⇒ 停下**）

| # | 前置 | 怎么查 | 不满足怎么办 |
|---|---|---|---|
| 1 | **窗口② 之前，生产 alembic 版本必须已是 `0148`**（窗口① 之前是 `0143`） | §3 ② | 先做**窗口①**「迁移链收口」（`0144`–`0148`，**源 = `86ff4a1f`**，见 §0.2 / §0.3），**本批不得连跑 9 个迁移** |
| 2 | **部署线空闲** | 与发版负责人确认 | 等 1114 / 1116 收口 |
| 3 | **生产鉴权是真鉴权** | §3 ③ | `AUTH_MODE` 必须是 `real` 且 `AUTH_STUB_ENABLED=0`；否则**不得开启**开关（见 §9.1 硬停条件） |
| 4 | 备份可用（pg_dump 落盘 + 校验） | §4 | 无备份 ⇒ 不执行 §5 之后的任何步骤 |
| 5 | 迁移已在**测试库**演练过（upgrade + downgrade 往返） | CI / 本地 `qa_metadata_test_p5` | 未演练 ⇒ 不上线 |
| 6 | **确认这是生产环境**（`APP_ENV=production`） | §3 ⓪⓪ | 不是 production ⇒ **停下**（本 runbook 会 `pg_dump` / `docker load` / `docker-compose up` ⇒ 在开发机上误跑 = 把开发库当生产库操作） |
| 7 | **开发库（5433）的 P5 表结构已重建**（**非生产前置，但本地验收前必做**） | §14.3 | 开发库的 `0149`–`0151` 是**旧快照**（迁移后来原地改过）⇒ 结构与代码不一致 ⇒ 按 §14 一次性重建 |

> ⚠️ **前置 1 的来历**：P5 的迁移接在 `0148` 之后（`0149`–`0152`），而生产现在停在
> `0143`。若直接上 P5 镜像，一次要跑 `0144`–`0152` 共 **9 个**迁移，其中 `0145`（加列/删列）、
> `0146`（建 2 表 2 索引）、`0147`（建不可变触发器）、`0148`（改 `audit_log` CHECK）
> 都有 DDL / 锁表风险 —— 超出本批的评估口径。**先收口 0144–0148，再上 P5。**

---

## 2. 交付物（开发机产出）

| 文件 | 说明 |
|---|---|
| `qa-backend-amd64-1117-p5.tar.gz` | **backend 镜像**（linux/amd64，源 = `main @ 1e04b46e`）；Worker **复用同一镜像**（只换 CMD）⇒ 不新增镜像 |
| `qa-frontend-amd64-1117-p5.tar.gz` | **frontend 镜像**（linux/amd64，源 = 同一 commit）—— **窗口② 必发**（§2.2） |
| `qa-backend-amd64-1117-mig0148.tar.gz` | **窗口①** 的 backend 镜像（源 = `main @ 86ff4a1f`，head 0148） |
| `compose.workflow-worker.centos7.fragment.yml` | **Worker 的 compose 服务片段**（仓内本目录，v1 兼容写法） |
| `deploy-prod-1117.md` | 本文件（生产操作手册） |

### 2.1 镜像构建（**必须在联网 x86_64 构建机**上做）

Mac 是 arm64、生产内网隔离 ⇒ 两者都构建不出能跑的镜像。

```bash
# 联网 x86_64 构建机
git clone <repo> qa-p5 && cd qa-p5

# ⚠️ **每个镜像构建前后都断言 HEAD**（同一工作区要 checkout 三个不同 commit
#    ⇒ 构建到错的 commit 是本 runbook 最贵的失误：镜像里可能带另一窗口的迁移）
assert_head() {  # $1 = 期望 SHA
  actual=$(git rev-parse --short=8 HEAD)
  [ "$actual" = "$1" ] || { echo "HEAD 不符：期望 $1，实际 $actual"; exit 1; }
  git status --short | grep -q . && { echo "工作区不干净 ⇒ 先清干净"; exit 1; }
  echo "HEAD=$actual OK"
}

# —— 窗口②（P5 本体）——
git checkout 1e04b46e && assert_head 1e04b46e
docker build --platform linux/amd64 -f docker/Dockerfile.backend \
  -t qa-system-backend:amd64-1117-p5 .
docker save qa-system-backend:amd64-1117-p5 | gzip > qa-backend-amd64-1117-p5.tar.gz
sha256sum qa-backend-amd64-1117-p5.tar.gz > qa-backend-amd64-1117-p5.tar.gz.sha256

# —— 窗口①（迁移链收口 0143→0148）——
git checkout 86ff4a1f && assert_head 86ff4a1f
docker build --platform linux/amd64 -f docker/Dockerfile.backend \
  -t qa-system-backend:amd64-1117-mig0148 .
docker save qa-system-backend:amd64-1117-mig0148 | gzip > qa-backend-amd64-1117-mig0148.tar.gz
sha256sum qa-backend-amd64-1117-mig0148.tar.gz > qa-backend-amd64-1117-mig0148.tar.gz.sha256
```

> ⚠️ **一个工作区构建三个 commit** ⇒ 每次 `checkout` 后**必须**断言 HEAD 且工作区干净
> （`git status --short` 为空）—— 否则会把上一个 commit 的残留文件烤进下一个镜像
> （典型症状：窗口① 的镜像里混进 P5 的迁移 ⇒ 生产一次跑完 9 个迁移 ⇒ 正是本 runbook 要防的事）。

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

### 2.2 前端镜像构建（窗口② 必发）

**两个前端批次已合入 `main @ 1e04b46e`**（PR #150）：实例列表/详情/提交表单 + 待办审批 +
定义管理（新建/编辑/发布/停用）。⇒ 前端**必须与 backend 同一窗口发布**，否则：
菜单 seed 出来的 3 个入口已在前端路由里、点进去却 404（后端路由未注册）。

```bash
# 联网 x86_64 构建机
git checkout 1e04b46e && assert_head 1e04b46e    # assert_head 见 §2.1
# ⚠️ **必须显式把 VITE_API_BASE_URL 留空** ⇒ 前端走同源相对路径 /api/v1（经 nginx 反代）
docker build --platform linux/amd64 -f docker/Dockerfile.frontend \
  --build-arg VITE_API_BASE_URL= \
  -t qa-system-frontend:amd64-1117-p5 .
docker save qa-system-frontend:amd64-1117-p5 | gzip > qa-frontend-amd64-1117-p5.tar.gz
sha256sum qa-frontend-amd64-1117-p5.tar.gz > qa-frontend-amd64-1117-p5.tar.gz.sha256
```

**三条雷（都是实测踩过的）**：

1. **禁止 `docker compose build frontend`**：compose 里
   `args.VITE_API_BASE_URL: ${VITE_API_BASE_URL:-http://localhost:5173/api/v1}`
   ⇒ 默认值是**绝对地址**，Vite 在**编译期**把它烤进 JS bundle ⇒ 生产用非
   `localhost:5173` 的地址访问时，所有 API 请求打到**浏览器自己机器的 5173** ⇒ 全挂。
   ⇒ 走上面的 `docker build` + `--build-arg VITE_API_BASE_URL=`（空值）。
2. **npm 源卡死**：`npm ci` 在受限网络会挂住 ⇒ 需要时加
   `--build-arg NPM_REGISTRY=https://registry.npmmirror.com`。
3. **部署源必须 fresh worktree**：构建前 `git checkout 1e04b46e` 后确认工作区干净
   （`git status --short` 为空），否则旧产物会混进 bundle。

**镜像自检（缺一不放行）**：

```bash
docker run --rm --entrypoint sh qa-system-frontend:amd64-1117-p5 -c '
  echo ABS_URL=$(grep -o "http://localhost:5173" /usr/share/nginx/html/assets/*.js | wc -l)
  echo WORKFLOW_TXT=$(grep -o "工作流实例" /usr/share/nginx/html/assets/*.js | wc -l)
  echo DEFS_TXT=$(grep -o "工作流定义" /usr/share/nginx/html/assets/*.js | wc -l)
  echo RESOLVER=$(grep -c "resolver 127.0.0.11" /etc/nginx/conf.d/default.conf)
'
```

期望：`ABS_URL=0`（**必须是 0**）· `WORKFLOW_TXT≥1` · `DEFS_TXT≥1` · `RESOLVER=1`
（nginx 用**变量形式** `proxy_pass http://$backendUpstream` + `resolver 127.0.0.11 valid=10s`
⇒ 后端容器重建换 IP 后不会 502；写死主机名的旧写法会**启动时钉死 IP** ⇒ 重建 backend 即全站
`/api/` 502，且前端 `fetchMenuConfig` 抛错后**静默**退回兜底导航 ⇒ 表现为「菜单少了、没有分组」）。

---

## 3. Phase 1 生产预检（只读，不改动）

```bash
# ⓪⓪ 环境断言 —— **先确认这是生产**，再做后面任何一步
#     （本 runbook 会 pg_dump / docker load / docker-compose up）
grep -E '^APP_ENV=' /opt/qa-system/docker/.env
# 期望：APP_ENV=production（不是 ⇒ 停下，见 §1 前置 6）

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
# 期望：**0143**（现役生产就是 0143 —— 窗口① 的目标正是把它推到 0148）
# ⚠️ 不是 0143 ⇒ 停下：迁移链与本 runbook 的窗口划分对不上（别在这时候连跑 9 个迁移）

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
# ⚠️ 先收紧 umask：pg_dump 落盘的是**整库备份**（含用户 / 角色 / 业务数据）
#    ⇒ 默认 umask 下同机其它账号可读 ⇒ 备份文件本身成了泄漏面。
umask 077

# 备份整库（推荐：恢复时不用拼对象）
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > /opt/qa-transfer/qa_metadata_pre-1117.dump
ls -lh /opt/qa-transfer/qa_metadata_pre-1117.dump
ls -l /opt/qa-transfer/qa_metadata_pre-1117.dump   # 期望：-rw-------（umask 生效）
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
shasum -a 256 qa-backend-amd64-1117-mig0148.tar.gz \
           qa-backend-amd64-1117-p5.tar.gz \
           qa-frontend-amd64-1117-p5.tar.gz
# 生产（应与开发机一致）
sha256sum qa-backend-amd64-1117-mig0148.tar.gz \
          qa-backend-amd64-1117-p5.tar.gz \
          qa-frontend-amd64-1117-p5.tar.gz

# —— 窗口① 的 backend 镜像（迁移链收口 0143→0148）——
docker load -i qa-backend-amd64-1117-mig0148.tar.gz
docker tag qa-system-backend:amd64-1117-mig0148 qa-backend:offline   # 见下方「image 名从哪来」

# —— 窗口② 的 backend 镜像（P5 本体，迁移 0149–0152）——
docker load -i qa-backend-amd64-1117-p5.tar.gz
docker tag qa-system-backend:amd64-1117-p5 qa-backend:offline

# —— 窗口② 的前端镜像 ——
docker load -i qa-frontend-amd64-1117-p5.tar.gz
docker tag qa-system-frontend:amd64-1117-p5 <生产 compose 里前端引用的 image 名>
```

> ⚠️ **image 名与服务名都不要猜**：生产 compose 是
> `/opt/qa-system/docker/docker-compose.centos7.yml`（v1，9 个服务，**无 worker**），
> 仓内的 `docker/docker-compose.centos.yml` 只有 4 个服务（是片段，不是生产全量）。
> backend 现役引用的是 `qa-backend:offline`（§6 的 tag 就是覆盖它）⇒
> **先读出真实的服务名与 image 名，再决定 tag 哪个、怎么重启**：

```bash
qa_compose config --services
# 期望：能看到 backend、postgres、…；前端服务名以本机输出为准（本地栈叫 frontend）

# 前端镜像引用名（用于上面的 docker tag）
qa_compose config | grep -i -B3 -A8 'frontend:' | grep -i 'image:'
# 期望：一条 image: 行 ⇒ 把新前端镜像 tag 成那个名字
```

> ⚠️ 两个窗口的 backend 镜像**共用同一个 image 名**（`qa-backend:offline`）
> ⇒ 窗口② 覆盖窗口① 的 tag。窗口① 想回滚 ⇒ 用 `amd64-1117-mig0148` 重新 tag 回去。

---

## 7. Phase 5 切换 —— **默认关闭**上线

> 本阶段**只换 backend 镜像**，**不追加 Worker 片段、不改 .env**
> ⇒ 工作流路由仍然 404、没有 Worker ⇒ 与上线前对外表现一致。

**窗口①（先做，迁移链收口）**：

```bash
# §6 已把 qa-system-backend:amd64-1117-mig0148 tag 成 qa-backend:offline
qa_compose stop backend
qa_compose up -d --no-build --no-deps backend
# 判据：alembic version = 0148（§3 ② 的查法），且后台同步热修仍在（ownSession 生效）
```

**窗口②（P5 本体）**：

```bash
# §6 已把 qa-system-backend:amd64-1117-p5 tag 成 qa-backend:offline
qa_compose stop backend
qa_compose up -d --no-build --no-deps backend
# 判据：alembic version = 0152
```

> ⚠️ `Dockerfile.backend` 的 CMD = `alembic upgrade head && uvicorn …`
> ⇒ 窗口① 的这次启动执行 `0144`–`0148`，窗口② 的这次启动执行 `0149`–`0152`
> ⇒ **两次都是 DDL 时刻** ⇒ §4 的备份必须在窗口① 之前就在手。
>
> ⚠️ 两次都**必须**带 `--no-build --no-deps`：不带 `--no-build` ⇒ compose 现场重建镜像
> （生产机无源码 ⇒ 直接失败）；不带 `--no-deps` ⇒ 顺带重启中间件（postgres / milvus …）。

### 7.1 前端切换（窗口②，**与 backend 同一窗口**）

```bash
# 服务名以 §6 的 `qa_compose config --services` 实测输出为准
qa_compose stop frontend
qa_compose up -d --no-build --no-deps frontend
```

- **务必带 `--no-build`**：否则 compose 会**现场重建镜像**（生产机没有源码 ⇒ 直接失败），
  且 `!override` / `profiles:` 在 v1 上会炸（见 §12 雷区）。
- 前端容器起来后**先探 `/api/` 链路**再进页面（§8 ⑧）：前端容器内的 nginx 要能解析
  `backend:8000`（同一 Docker 网络）⇒ 若生产前端不在同一网络，`/api/` 会 502，
  而前端会**静默**退回兜底导航（菜单少了、没有分组），**看不出是 502**。
- ⚠️ **前端切在 backend 之后**：前端新页面会请求 `/api/v1/workflow/*`；
  若前端先上而 backend 未上 ⇒ 页面 404（路由未注册）⇒ 用户看到「点菜单就报错」。
  开关默认关时这也是**预期行为**（接口 404 ⇒ 页面提示「未启用」）。

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

# ⑧ 前端已起 + 菜单已 seed（窗口②）
#    菜单表：3 条新菜单码（种子只在「行不存在」时插入 ⇒ 幂等）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT code || \x27|\x27 || path FROM menu_config \
   WHERE code IN (\x27item.workflowInstances\x27,\x27item.workflowApprovals\x27,\
   \x27item.workflowDefinitions\x27) ORDER BY code;"'
# 期望：3 行（item.workflowApprovals|/workflow/approvals、item.workflowDefinitions|/workflow/definitions、
#         item.workflowInstances|/workflow）
# ⚠️ 少于 3 行 ⇒ seed 没跑到（backend 启动会跑 seed）⇒ 查 backend 启动日志

# ⑨ 前端 → API 的反代链路（**打前端容器自己的 nginx，不直连 backend**）
#    ⚠️ 别拿宿主机端口猜前端 origin（生产端口映射以本机 compose 为准）
FRONTEND_CTR=$(docker ps --format '{{.Names}}' | grep -E 'frontend' | head -1)
echo "FRONTEND_CTR=$FRONTEND_CTR"
docker exec "$FRONTEND_CTR" sh -c \
  'wget -qO- http://127.0.0.1/api/v1/health >/dev/null 2>&1 && echo frontend_health=200 || echo frontend_health=FAIL'
# 期望：frontend_health=200（nginx 按 resolver 解析到 backend:8000 并转发成功）
# ⚠️ FAIL / 502 ⇒ 前端容器解析不到 backend:8000（不在同一 Docker 网络 / nginx 写死了旧 IP）
#    ⇒ 页面会**静默**退回兜底导航（菜单少了、没有分组），**看不出是 502**
#    浏览器侧复核（把 <前端对外地址> 换成用户实际访问的地址）：
#    curl -s -o /dev/null -w "frontend_health=%{http_code}\n" http://<前端对外地址>/api/v1/health

# ⑩ 前端 bundle 里确实带上了新入口（静态资源里搜菜单文案）
docker exec "$FRONTEND_CTR" sh -c \
  'echo WORKFLOW_TXT=$(grep -o "工作流实例" /usr/share/nginx/html/assets/*.js | wc -l); \
   echo DEFS_TXT=$(grep -o "工作流定义" /usr/share/nginx/html/assets/*.js | wc -l)'
# 期望：WORKFLOW_TXT≥1 且 DEFS_TXT≥1（两个批次的页面都在 bundle 里）
# ⚠️ =0 ⇒ 拿到的是**旧**前端镜像（bundle 里没有这两页）⇒ 回 §2.2 重建镜像，别只重启容器
```

> ⚠️ **开关默认关时**，`/workflow*` 三个页面的接口返回 **404** 是**预期**（不是故障）：
> 前端页面存在、菜单存在、点进去提示「未启用」⇒ 能力开启见 §9。

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

⚠️ 片段里的开关是 ``${ENABLE_WORKFLOW:-false}`` ⇒ **追加片段本身零副作用**
（worker 侧默认关）⇒ 追加 ≠ 开闸 ⇒ 开闸只发生在 §9.3（写 `.env`）+ §9.4（起服务）。

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

### 9.3 打开开关（**只追加一行**）

```bash
# ⚠️ 只**追加**，不改任何既有行（SECRET_KEY 是 Fernet 主密钥）
grep -q '^ENABLE_WORKFLOW=' /opt/qa-system/docker/.env \
  || echo 'ENABLE_WORKFLOW=true' >> /opt/qa-system/docker/.env
```

> 这一行同时打开**两侧**：API 侧（`app/config.py` 读 `.env` ⇒ 注册
> `/api/v1/workflow/*` 路由）与 worker 侧（片段里的 ``${ENABLE_WORKFLOW:-false}``
> 由 compose 从同一份 `.env` 插值 ⇒ 容器拿到 `true`）。
> ⚠️ 两侧必须**同时**开：只开 API 侧 ⇒ 有实例没人执行；只开 worker ⇒ 空转。

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

# ⑥ 取消路由已注册（未带 token ⇒ 403 而不是 404 ⇒ 证明开关已生效）
curl -s -o /dev/null -w "workflow_cancel=%{http_code}\n" \
  -X POST http://localhost:8000/api/v1/workflow/instances/1/cancel \
  -H 'Content-Type: application/json' -d '{"reason":"probe"}'
# 期望：403（real 模式无 Bearer）；**404 ⇒ 开关没生效**

# ⑦ 命令状态 CHECK 含 CANCELLED（PG 把 IN 渲染成 = ANY(ARRAY[...])，抠出值列表再比）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT pg_get_constraintdef(oid) FROM pg_constraint \
   WHERE conrelid = \x27business_command\x27::regclass \
     AND conname = \x27ck_business_command_status\x27;"'
# 期望：值列表里含 CANCELLED

# ⑧ audit_log CHECK 含 WORKFLOW_CANCEL（7 个新动作）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT pg_get_constraintdef(oid) FROM pg_constraint \
   WHERE conrelid = \x27audit_log\x27::regclass AND conname = \x27ck_audit_log_action\x27;"'
# 期望：值列表里含 WORKFLOW_CANCEL
```

---

## 11. Phase 9 回滚

### 11.1 L1 快速回滚（**秒级，不动数据库**）—— 首选

```bash
# ① 停 Worker（**必须**停：开关关时 worker 立即退出，`restart: unless-stopped` 会反复拉起）
qa_compose stop workflow-worker && qa_compose rm -f workflow-worker

# ② 关 API 侧开关并重建 backend
#    （删掉这一行 ⇒ **两侧同时关**：API 侧读不到 ⇒ 路由不注册；
#      worker 侧片段的 ${ENABLE_WORKFLOW:-false} 回落成 false）
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
| 11 | 窗口① 的镜像源选成 `68398378` / `75a435a3`（同为 head 0148 但**不含**本体向量热修） | 生产**回退** `MissingGreenlet` 修复 ⇒ 后台同步再次炸 | 只认 `86ff4a1f`（或其后**仍以 0148 为 head** 的 main 提交）；判据见 §0.2 的对照表 |
| 12 | 用热修分支的 **rebase 版**镜像当窗口① 的源 | 该分支 base `0b97d33c` **自带 0144–0148** ⇒ 一次把窗口① 的 **5 个**迁移全跑掉 ⇒ 绕开分窗口评估 | 镜像必须构建自 **main 的合并后提交**，不是分支头（§2.1） |
| 13 | `docker compose build frontend`（没显式留空 `VITE_API_BASE_URL`） | 默认值 `http://localhost:5173/api/v1` 被**烤进 bundle** ⇒ 生产换地址即全站 API 挂 | 用 `docker build --build-arg VITE_API_BASE_URL=`（§2.2 雷 1） |
| 14 | 前端 nginx 写死 `proxy_pass http://backend:8000` | nginx **启动时**解析一次并缓存 IP ⇒ 后端容器重建换 IP ⇒ 全站 `/api/` 502，而前端**静默**退回兜底导航（菜单少了） | 仓内 `docker/nginx.conf` 已是变量形式（`resolver 127.0.0.11 valid=10s` + `proxy_pass http://$backendUpstream`）⇒ **必须用仓内 nginx.conf**，别用旧镜像里的 |
| 15 | 前端先于 backend 切换 | 新页面请求 `/api/v1/workflow/*` 未注册 ⇒ 点菜单就 404 | 先切 backend（§7），再切前端（§7.1） |

---

## 13. 上线后行为变化与注意事项

| 位置 | 变化 |
|---|---|
| 工作流 API | 开关关：**404**（对外完全不存在）；开关开：需要真鉴权（无 Bearer ⇒ 403）；**新增取消路由** |
| 数据库 | 新增 6 张空表；`audit_log.action` 的 CHECK 白名单变宽（**旧值全部仍可用**）；`business_command.status`（**新表，整套状态都是新的**）= `PENDING` / `EXECUTING` / `SUCCEEDED` / `FAILED` / `COMPENSATED` / `CANCELLED` / `ABORTED` |
| 后台进程 | 新增 1 个独立 Worker 容器（默认不启动）；它**只**写 P5 自己的表（模拟适配器不写外部系统） |
| 用户界面 | **有变化**（窗口②）：新增 3 个页面（`/workflow` / `/workflow/approvals` / `/workflow/definitions`）+ 3 个菜单项（`item.workflowInstances` / `item.workflowApprovals` / `item.workflowDefinitions`，挂 `section.bizConfig`）+ `workflow.*` / `workflowDefinition.*` 中英文案。**开关默认关时**：页面与菜单**都在**，但接口 404 ⇒ 点进去是「未启用」提示（**不是**故障）。定义管理页只有 **admin** 可见；定义体决定「谁批这件事」⇒ 写入口只对 admin 开放。 |

**注意事项**：

1. **开关是分体的**：worker 侧在 compose 片段里（`environment`），API 侧在 `.env`。
   只开一边 ⇒ worker 空转 / 路由 404，但都不会写错数据。
2. **审批角色要先配**：矩阵里的 `approverRole`（如 `purchase_manager`）必须在 RBAC 里真实存在，
   否则审批任务永远没人能批（到期被清扫为 TIMEOUT）。
3. **审批超时**：默认 24 小时（矩阵里的 `timeoutHours`）⇒ 实例会卡在「等待审批」直到清扫。
4. **本批不含真实 ERP/SRM 写入**：Worker 只注册了 `po.write` 的**模拟**适配器
   ⇒ 即使开关全开，也不会有任何外部系统被写。
   ⚠️ 但定义里写了别的 `commandType` ⇒ 命令**失败关闭**并触发补偿（见 §0.1）。
5. **每次发版都要重建 Worker**：它与 backend 同一镜像 ⇒ retag 不会自动更新已运行的容器。
6. **提交即入队**（§0.1）：开启开关后，提交实例会立刻产生一条 `business_command`（PENDING），
   Worker 会在**下一次轮询**（默认 2s）领走它 ⇒ 这条链路是**活的**，不是纸面能力。
   ⇒ R11 起 Worker **一轮排空**：单轮最多领走 50 条待执行命令（**可重试失败不在同轮热转**，
   仍按轮询节奏重试）⇒ 批量提交不会被 2s 轮询间隔压死。
7. **前端菜单靠 seed、不靠迁移**：菜单表 `menu_config` 的 3 条新行由
   `backend/scripts/seed_menu_config.py` 在 **backend 启动时**写入（**只在「行不存在」时插入**
   ⇒ 幂等；**不覆盖** UI 改过的 label/icon/path）。
   ⇒ 已挂载的库**第一次**上新版 backend 时自动出现；若菜单没出现 ⇒ seed 没跑到，
   查 backend 启动日志（**不要**手改 menu_config，走了 UI 编辑的行会被 seed 跳过）。
8. **前端 bundle 的地址是编译期固化的**：`VITE_API_BASE_URL` 必须留空（相对路径），
   否则换访问地址即全站 API 挂（§2.2 雷 1）。**改前端后必须重建镜像**，不能只重启容器。

---

## 14. 开发库（5433）的 P5 漂移与一次性重建（**非生产**）

### 14.1 漂移是怎么来的（**别再当成「没发生过」**）

P5 的迁移 `0149`–`0151` 在**开发期**已在**开发库**（5433 `qa_metadata`）执行过
（反复 `upgrade head` 建表 + 跑 P5 用例）。之后这些迁移被**原地编辑**过：

| 迁移 | 原地编辑内容 |
|---|---|
| 0149 | 删 `workflow_instance.current_step` / `business_key` / `result_json`；`created_by` 改 **NOT NULL**；幂等唯一约束改成 `(definition_id, created_by, idempotency_key)` |
| 0151 | `workflow_outbox.event_type` 注释纠偏（「worker 据此路由」→「事件流词汇表；当前无消费者：只写不读」） |

⇒ **开发库里的 P5 表结构 ≠ 当前迁移文件**（2026-11-17 实测：多 3 列、
`created_by` 仍可空、唯一约束少 `created_by` 一列、3 处列注释过期）
⇒ 开发库上 `alembic check` 必报漂移（且开发库停在 `0151` ⇒ 连 `check` 都进不去：
「Target database is not up to date」）。

> ✅ **生产不受影响**：生产停在 `0143` ⇒ 从未跑过 `0149` ⇒ 生产按**当前**
> 迁移文件执行，零漂移。**本节的一切操作都不许在生产上做。**
> ✅ **测试库（5434 `qa_metadata_test_p5`）不受影响**：R11 改完 0149/0151 后
> **重建过**测试库再验 `alembic check`（实测 `No new upgrade operations detected`）。

### 14.2 为什么是「重建」而不是「ALTER 补齐」

漂移里有 **NOT NULL 收紧**与 **唯一约束换列** ⇒ 逐条 `ALTER` 反而更脆
（改一半失败 ⇒ 库里半新半旧）。6 张表**实测全空** ⇒ 直接重建最省事也最安全。

### 14.3 一次性重建脚本（**只对开发库**）

前置确认（**必须先跑**）：6 张 P5 表**全空** ⇒ 重建不丢数据。

```bash
# 开发机（**不是生产**）：确认 P5 表为空
PW=$(docker exec qa-postgres printenv POSTGRES_PASSWORD)   # 密码不落盘、不打印
docker exec -e PGPASSWORD="$PW" qa-postgres psql -U qa_user -d qa_metadata -tAc \
  "select count(*) from workflow_instance union all select count(*) from workflow_step
   union all select count(*) from business_command union all select count(*) from approval_task
   union all select count(*) from workflow_outbox union all select count(*) from workflow_definition;"
# 期望：6 行，全是 0（**非 0 ⇒ 停下，先确认能不能丢**）

# ① 删 6 张 P5 表（含 FK ⇒ CASCADE 一次删干净）+ 版本戳退回 0148
docker exec -i -e PGPASSWORD="$PW" qa-postgres psql -U qa_user -d qa_metadata -v ON_ERROR_STOP=1 <<'SQL'
DROP TABLE IF EXISTS workflow_outbox, approval_task, business_command,
                   workflow_step, workflow_instance, workflow_definition CASCADE;
UPDATE alembic_version SET version_num = '0148';
SQL

# ② 用**当前**迁移文件重放 0149 → 0152
cd backend
DATABASE_URL="postgresql+asyncpg://qa_user:${PW}@127.0.0.1:5433/qa_metadata" \
  python -m alembic upgrade head

# ③ 验证（判据 = **P5 相关漂移为 0**，不是整库零漂移 —— 见下）
DATABASE_URL="postgresql+asyncpg://qa_user:${PW}@127.0.0.1:5433/qa_metadata" \
  python -m alembic check
```

**实测（2026-11-17，已执行）**：

| 检查 | 结果 |
|---|---|
| `alembic_version` | `0152` ✅ |
| `workflow_instance` 列集合 | `id / definition_id / idempotency_key / status / request_json / error_message / created_by / created_time / updated_time`（3 个死列已不在）✅ |
| `uq_workflow_instance_idempotency` | `UNIQUE (definition_id, created_by, idempotency_key)` ✅ |
| `alembic check` 的漂移清单 | **不含**任何 `workflow_*` / `business_command` / `approval_task` 项 ⇒ **P5 漂移 = 0** ✅ |

> ⚠️ 开发库另有**与 P5 无关的历史漂移**（`alembic check` 仍会报）：
> `PORDER` / `PORDERQ` / `BPSUPPLIER` / `*_20260930` / `*_20260918` 等**备份表**
> （ORM 不声明、迁移也不管）+ `wiki_compile_task_created` 索引写法差异 +
> `wiki_token_usage` 的 FK 差异 ⇒ **不属于本批**（各有各的账本），**别把它
> 当成 P5 没修干净**。⇒ 本节的判据是「**P5 相关**漂移为 0」。

> ⚠️ **`docker exec` 少了 `-i` ⇒ heredoc 静默失效**：`docker exec -i … psql <<'SQL'`
> 不加 `-i` 时 psql **收不到脚本**（**退出码仍是 0、无任何输出**）⇒ 看着像
> 跑过了，其实一条 SQL 都没执行 ⇒ 上面的重建步骤**必须带 `-i`**。

> ⚠ `alembic_version` 手工改戳**只在开发库**做；**生产绝不可手改版本戳**。
> ⚠ 重建后开发库的 P5 表为空 ⇒ 本地 P5 演示数据要**重新造**（走 §8 探针的
> 「提交实例」接口现造，无脚本依赖）。

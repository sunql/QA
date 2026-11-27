---
name: thdeploy
description: Use when 为 qa-system 生产环境（CentOS7 ai-server）创建发版方案、执行部署/回滚、或需要写任何生产服务器上的 docker 命令时——包括用户说"发版""上线""部署到生产""出问题要回滚"
---

# thdeploy — 生产发版方案生成

一句话：沿用已实测多批的 **2026-11-22 runbook（Phase 0–8）结构**生成发版方案，把历史 runbook 中需要手工补的容器/镜像/网络名**全部预填**，并修正其中残留的 v2 命令。**升级而非另起炉灶：方案结构与 2026-11-22-prod-deploy/runbook.md 逐节对齐。**

权威范本（写方案前先对照）：`Harness/changes/2026-11-22-prod-deploy/runbook.md`

## 输入（创建方案前必须确认）

| 项 | 说明 | 示例 |
|----|------|------|
| 发版标识 | `MMDD` 或 `MMDD-<简称><2位流水>`；简称=功能英文缩写或 `fix`；流水同日同类型从 01 起 | `1124`、`1124-newfuture01`、`1124-fix02` |
| 范围 | backend / frontend / 双端 | backend |
| 迁移 | 涉及的 alembic revision 区间、每条可逆性（数据回填⇒必须 pg_dump）、回滚 downgrade 边界 | 0169→0170，边界 0169 |
| 改动摘要 | PR 列表 + 一句话，写入方案 §0 | — |

标识贯穿四处对齐：镜像 tag、`pre-` 回滚锚 tag、`pg_pre_` 备份文件名、`Harness/changes/` 方案文档，全部用同一个串。

## 环境事实（2026-11-24 实测；方案生成前可用 §校验命令 复核）

- CentOS 7.9 / Docker 19.03.9 / **docker-compose v1.29.2**——没有 `config --images`（v2 才有，**历史 runbook 里的 `qa_compose config --images` 是错的**，一律改用 `config | grep 'image:'`）；老 git 没有 `-C`
- 所有 compose 命令走包装函数（新会话先定义）：

```bash
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
```

- 服务：postgres / neo4j / milvus(etcd+minio+standalone) / qa-objects / mysql-demo / backend / agent-orchestration-worker / frontend
- backend `8000`、frontend `5173`；PG 宿主侧暴露 `127.0.0.1:5433`（5432 被 openmetadata 占）；DB 连接走服务名 `postgres:5432/qa_metadata`
- backend 容器 CMD 自带 `alembic upgrade head`（启动即迁移）；**旧镜像起不来新库**（`Can't locate revision`）⇒ 回滚必须先降库再换镜像
- compose 里 backend/worker 是 `image:` + `build:`，**镜像存在则 up 直接用镜像，不会重 build**（切换仍显式加 `--no-build` 防现场 build）
- mysql-demo 长期 Exit 1（卷内残留文件导致 re-initialize 失败）——**已知问题，冒烟时忽略**
- /opt/qa-system 不是 git 仓库；镜像靠开发机 build + `docker save/load`（生产机不 build）

## 固定值预填表（历史 runbook 里要手工补的名字，现在直接用，禁止再留占位符）

| 项 | 固定值 |
|---|---|
| backend / worker / frontend / PG 容器名 | `qa-backend` / `qa-agent-orchestration-worker` / `qa-frontend` / `qa-postgres` |
| backend 镜像引用名（compose 实际引用） | `qa-backend:offline` |
| frontend 镜像引用名（compose 实际引用） | `qa-system_frontend` |
| compose 网络（回滚 downgrade 用） | `qa-system_default` |
| worker 拓扑 | **与 backend 同镜像，切换时随 backend 一起 recreate**（2026-11-23 起在跑；旧 runbook「生产无 worker」已过时）。依据见 [[2026-11-23-copilot-prod-fixes]] runbook：「发版后 worker 不会自动换镜像（`image:` 是显式引用），换 backend 镜像时要一并 `--force-recreate agent-orchestration-worker`」 |
| psql 惯用法（凭据走容器 env，不写明文） | `docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "…"'` |
| 备份目录与命名 | `/opt/qa-system/backups/pg_pre_<标识>_$(date +%Y%m%d_%H%M).sql`（plain SQL，`docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"'` 导出；0 字节⇒重跑） |
| 切换命令 | `qa_compose up -d --no-build --no-deps --force-recreate <svc>`；**v1 禁止 `--remove-orphans`** |
| .env 纪律 | 禁改 `SECRET_KEY`/口令类；仅允许追加/修改 `ENABLE_*` 行；**先改 .env 后 recreate（顺序反了=改了没生效，restart 无效）**；改前备份 `.env.bak_时间戳` |
| compose 入口权威来源 | 从现役容器 label 读：`docker inspect qa-backend --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'`（不要靠 ls 猜） |
| **worker 与 backend 同镜像** | 两者跑同一份代码（本地：同一 tag；生产：compose 里 worker 只 `build:` 无 `image:`，镜像名由 compose 派生）。**换 backend 镜像必须同步 worker**，否则 worker 跑旧代码 ⇒ 启动 `warmUp` 时 `handler_ref 不在 BUILTIN 白名单` ⇒ ValueError ⇒ **restart 循环** ⇒ 编排 run 永远 QUEUED ⇒ 前端等结果 ⇒ **报「超时」**（2026-11-27 本地实证：到货跟踪全部超时，根因就是 worker 落后 backend 两个版本） |

## 方案结构（Phase 0–8，照 2026-11-22 runbook 逐节写）

0. **一句话说清这次改了什么**：PR 表 + 迁移表（含可逆性）+ 验收标准
1. **交付物**：镜像 tag / linux-amd64 / sha256 / 大小 + 开发机镜像内容自证（`docker run --rm --platform linux/amd64 … `探迁移文件、新代码、前端 bundle 文案）
2. **Phase 0 生产预检**（只读）：label 核对 compose 入口 → `config --quiet` → `config | grep 'image:'` → 现役容器/镜像 → 现役前端 bundle（记录备用）→ alembic 版本（**不是预期值 = 停**）→ health ×2 + 磁盘；每项带**停下条件**
3. **Phase 1 回滚锚点**（⚠️ 引用名自带 tag，锚点 tag 必须写成 `<repo>:pre-<标识>`，不能再拼一层 tag）：
   ```bash
   docker tag qa-backend:offline  qa-backend:pre-<标识>          # 不能写 qa-backend:offline:pre-<标识>（两个冒号非法）
   docker tag qa-system_frontend  qa-system_frontend:pre-<标识>
   ```
   + pg_dump + 记录 PRE_VER（= 回滚 downgrade 边界）；三者齐备才准进 Phase 2
4. **Phase 2 传输**：scp/加载后**先比 sha256** → `docker load -i` → `docker tag` 成上表引用名 → `docker images` 核对 Image ID 与 §1 一致
5. **Phase 3 切换**：先后端再前端，不碰数据容器；**worker 随 backend 一并切换**（见下方 §worker 同步铁律）；`docker logs -f qa-backend | grep -E "Running upgrade|…|Application startup complete|ERROR|blocking"`；迁移版本复核；按需 seed（`qa_compose exec backend python -m scripts.seed_*`，幂等）；.env 开关走「先改后 recreate」铁律并验证 404→401/403
6. **Phase 4 验收探针**：后端（logs / alembic 版本 / health / 新路由 401/403=已注册、404=未注册 / 新表 `\dt`）+ 前端（bundle 文件名与文案 grep 计数）+ 浏览器端到端（硬刷新）；**探针按本批特性定制，不是固定清单**
7. **回滚**（写死在方案里，不是事后现想）：`qa_compose stop backend` → 用**新镜像**降库（`docker run --rm --network qa-system_default -v /opt/qa-system/docker/.env:/app/.env:ro qa-system-backend:amd64-<标识> alembic downgrade <PRE_VER>`）→ 复核版本 → `docker tag …:pre-<标识> …` 换回 → recreate；前端可单独回滚（不动库）；替代：保库只回前端
8. **本批明确没做的事**：❌ 清单（不动哪些容器 / 不改哪些配置 / 不带未合 main 的改动）

## worker 同步铁律（换 backend 镜像时必做，漏了就出「假超时」）

**症状**（2026-11-27 本地实证）：采购 Copilot 的到货跟踪等**编排类**功能「任意查询都报超时」，
但数据库、网络、SQL、绑定全部正常。

**根因链**：worker 跑旧镜像 ⇒ 启动 warmUp 时校验 `agent_tool_config.handler_ref` 不在旧代码的
BUILTIN 白名单 ⇒ `ValueError` ⇒ 进程退出 ⇒ restart 循环 ⇒ **从不消费 QUEUED 的编排 run**
⇒ 前端等 run 结果 ⇒ 30s 超时。

**识别（30 秒定位）**：

```bash
# ① worker 是否在崩溃循环
docker ps --format '{{.Names}}\t{{.Status}}' | grep worker      # Restarting (N) ⇒ 中招
# ② worker 日志里的真因
docker logs qa-agent-orchestration-worker --tail 20 2>&1 | grep -iE "ValueError|不在 BUILTIN 白名单"
# ③ 两个容器的镜像是否同一个
docker inspect qa-backend qa-agent-orchestration-worker --format '{{.Name}} {{.Image}}'
# ④ 编排 run 是否全卡在 QUEUED（消费不掉的铁证）
#    SELECT status, count(*) FROM agent_orchestration_run GROUP BY status;   -- QUEUED 堆积
```

**修复**：

```bash
# 本地（compose v2）
docker tag qa-system-backend:latest qa-system-agent-orchestration-worker:latest
docker compose -f docker/docker-compose.yml up -d --no-build --force-recreate backend
docker compose -f docker/docker-compose.yml --profile agent-orchestration \
  up -d --no-build --force-recreate agent-orchestration-worker
```

```bash
# 生产（compose v1，见 §生产发版命令）
docker tag qa-backend:offline <compose 里 worker 的实际引用名>
qa_compose up -d --no-build --no-deps --force-recreate backend agent-orchestration-worker
```

**验收**：

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep agent-orchestration-worker   # Up（不是 Restarting）
docker inspect qa-backend qa-agent-orchestration-worker --format '{{.Name}} {{.Image}}'   # 两个 Image ID 相同
# 编排 run 的 QUEUED 堆积应在 1~2 个轮询周期内被消费
```

**与「本地栈多人并行操作」的配合**：本地有多个会话轮流重建镜像时，**任何一方重建 backend
都必须带上 worker**；否则 worker 落后 ⇒ 复现上面的「假超时」。若发现
`docker inspect qa-backend` 引用的 Image ID 在 `docker images` 里查不到（悬空引用，
镜像被删但容器还在跑），一并把 backend 拉回确定存在的 tag。


- 发版标识、PR/commit 列表、迁移区间与可逆性、PRE_VER、seed 脚本清单、验收探针（新路由/新表/新文案）、image ID / sha256 / bundle 文件名
- 除上述外**一切名字用固定值表**；固定值若与 Phase 0 label 核对输出冲突，以实测为准并回写本技能

## 校验命令（方案生成前 & 发版前各跑一次）

```bash
qa_compose config --quiet && echo COMPOSE_OK        # 配置+env 插值
qa_compose config | grep 'image:'                    # v1 无 --images，用这条看镜像引用名
qa_compose ps                                        # 基线状态；mysql-demo Exit 1 可忽略
docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' | grep amd64 | head   # 确认新 tag 未被占用
```

## 硬规则（违者方案作废）

- 生产机上**禁止裸写 `docker compose ...`**（v2 语法；必须 `qa_compose`）
- **没有回滚锚点不得动手**：pre- 标 + pg_dump + PRE_VER 三者齐备才开始
- **禁止给用户留 `<占位符>` 让其手工补容器/镜像/网络名**——一律用固定值表预填；唯一例外是 §每批生成的变量
- 任何含 `DATABASE_URL`/`POSTGRES_PASSWORD`/密钥的输出**必须脱敏**后才能进方案文档或对话
- 方案文档落在 `Harness/changes/<日期>-release-<标识>/`（runbook.md + summary.md）作为 SSOT
- 禁止用 `latest`/`offline` 当发版 tag（仅作 compose 对齐名中转）
- 历史 runbook 命令与本文冲突时（如 `config --images`、无 worker 拓扑），**以本文为准**

## Red Flags — 停下来检查

- 想写 `docker compose` / `config --images` / `git -C` → 生产机都不支持，回 §环境事实
- 想写 `<现役引用名>`、`<网络名>` 这类占位符 → 固定值表预填
- "先直接 up，备份回头补" → 锚点前置，无锚不发版
- "迁移失败改一下迁移文件" → 走 deploy-verify §0 撞名流程，禁止 IF NOT EXISTS
- "worker 不用动" → 与 backend 同镜像，必须一起 recreate
- "改了 .env restart 一下就行" → env 只在 create 时注入，必须 force-recreate
- `--remove-orphans` / 现场 build → 禁用，用 `--no-build --no-deps --force-recreate`
- 「只重建 backend，worker 回头再说」→ **worker 必须同步换镜像**（落后 ⇒ restart 循环 ⇒ 编排 run 永 QUEUED ⇒ 前端报「超时」，而 DB/网络/SQL 全部正常 ⇒ 极难自查）。见 §worker 同步铁律
- 「前端报超时 ⇒ 一定是慢查询/缺索引」→ 先查 worker 状态与编排 run 状态；2026-11-27 的「到货跟踪全超时」根因是 worker 落后，**不是** SQL 慢（实测聚合仅 2~3 秒）

## 相关

- [[deploy-verify]] — 迁移撞名修复、冒烟检查清单、本地（v2）部署
- `Harness/changes/2026-11-22-prod-deploy/runbook.md` — 权威结构范本
- `scripts/deploy_backend.sh` — docker cp 应急热修（非标准通道；容器重建即失效；三目录 app/+scripts/+alembic/ 必须一起灌，源路径带 `/.`）
- `scripts/backup_pg.sh` — 日常例行备份（cron 未安装，仅手工）；发版锚点用 pg_pre_ 命名，不要混用

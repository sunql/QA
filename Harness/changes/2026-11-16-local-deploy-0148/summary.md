# 变更：2026-11-16 本地部署拉平到 main（迁移链 0143 → 0148）

- **日期**：2026-11-16
- **作者**：Claude / phase3-reconciler
- **范围**：本地 docker 栈（compose 项目 `qa-system`）镜像重建 + 本地库迁移推进
- **生产**：**未部署**（用户指令：等 1109 需求整体完成后再统一上生产）
- **关联变更**：[`2026-11-16-p5-workflow-command`](../2026-11-16-p5-workflow-command/summary.md)（P5 计划 §5「开工前必须先落地的三件事」）、
  [`2026-11-16-ontology-discovery-ai`](../2026-11-16-ontology-discovery-ai/summary.md)（WP-ONT-AI A1/A2a，0146–0148 的来源）、
  [`2026-11-16-fix-ontology-bg-vector-sync`](../2026-11-16-fix-ontology-bg-vector-sync/summary.md)（1116 向量同步热修）

> **快照说明（2026-11-18 补）**：本记录是 **2026-11-16** 的执行快照。此后 **P5 已合入 main**
> （后端 PR #148 → `a217fef0`；前端两批 PR #150 → `1e04b46e`）⇒ 迁移链 `0149`–`0152` 进了 main 头，
> 本地 dev 库 `alembic_version` 已推进到 **`0152`**（= main head，2026-11-18 实测）；
> **生产仍 `0143`、仍未切镜像**（用户指令：1109 需求整体完成后再统一上生产）。
> 本记录里「拉平到 `0148`」指的是 **2026-11-16 当时**的 main（`86ff4a1f`），不是今天的 main。

---

## 1. 需求与边界

用户指令（2026-11-16）：

> 代码侧 main 已到迁移 0148（0144 会话状态 / 0145 Agent 快照 / 0146–0148 发现底座），当前全部未部署，
> 需要现在本地部署，拉平，等这个需求整体完成后，统一到生产部署，本地代码和 docker 运行的程序需要一致，是最新的。
> ① 迁移链 0144–0148 先上本地测试环境，② 1114/1116 部署线收口，③ 迁移号与 WP-ONT-AI 串行分配，避免冲突。
> 业务答复征集：Q3/Q4/Q5 已写成可直接发出去的清单（谁答什么，逐条列出）也可以单独征集，但是暂时不做，
> 征集完毕更新 Harness，等待指令再开始。

**边界（明确不做的事）**：

| 不做 | 理由 |
|---|---|
| **不动生产** | 「等这个需求整体完成后，统一到生产部署」⇒ 生产仍 `0143`、仍跑旧镜像 |
| **不发 Q3/Q4/Q5 业务征集** | 「暂时不做」⇒ 只把清单与暂缓决定记进 Harness，等指令 |
| 不改生产 `.env`（`SECRET_KEY` 是 Fernet 主密钥） | 硬约束 |
| 不对生产库做任何写操作 | 硬约束 |

## 2. 本地部署结果（①）

### 2.1 镜像

| 服务 | 镜像 | 说明 |
|---|---|---|
| backend | `qa-system-backend` ⇒ `sha256:f2ffc5e47b10…` | 从 `origin/main`（`86ff4a1f`）源码 `docker compose -p qa-system build backend` |
| frontend | `qa-system-frontend` ⇒ `sha256:3700974a6111…` | 同上；`VITE_API_BASE_URL` 取 `docker/.env` 的 `http://localhost:5173/api/v1`（走 nginx 同源反代） |

compose 文件（`docker/docker-compose.yml`）顶层 `name: qa-system`（项目名固定），
backend/frontend **只有 `build:` 没有 `image:`** ⇒ 镜像名由 compose 派生。

### 2.2 本地库迁移

| 迁移 | 内容 | 性质 |
|---|---|---|
| `0144` | `session_query_state.semantic_release_id`（Integer，可空） | 加法 |
| `0145` | `agent_orchestration_run` 三列（`semantic_release_id` / `_version` / `_manifest_hash`，全可空） | 加法 |
| `0146` | 发现域两张表 + 索引 | 加法 |
| `0147` | 快照不可变函数 + 库级不可变触发器 | 加法 |
| `0148` | 放宽 `ck_audit_log_action` CHECK，纳入 `'REUSE'` | 重建约束 |

推进后：`alembic_version = 0148`（本地 dev 库 `qa_metadata`，宿主端口 5433）。

### 2.3 探针（全部实测）

| 探针 | 结果 |
|---|---|
| `GET http://localhost:8000/api/v1/health` | **200** |
| `GET http://localhost:5173/`（frontend nginx） | **200** |
| `GET http://localhost:5173/api/v1/health`（同源反代到 backend） | **200** |
| 容器内代码 ↔ worktree 逐文件 md5 | **MATCH**（`ontology_service.py` / `database.py` / `kpi_vector_graph_sync.py` / `main.py` / `models.py`） |

> ⚠️ 端点是 `/api/v1/health`，**不是** `/health`（后者 404）。

## 3. 一次事故：0144 重复列（已修，值得留档）

**现象**：本地起 backend 时崩溃循环 ——
`asyncpg DuplicateColumnError: column "semantic_release_id" of relation "session_query_state" already exists`。

**根因**：本地 dev 库曾被一个**未合并**的语义分支镜像 `alembic stamp` 到 `0144`（stamp 只改版本号，
**不执行 DDL**），随后为了回到 `0143` 又执行了 `alembic stamp --purge 0143` ——
⇒ 结果是：库里**留着 0144 的列**，但 `alembic_version` 写着 `0143` ⇒ 再跑真迁移时撞列。

**处置（先取证再动 DDL）**：

1. 比对该列定义与 0144 的 `upgrade()` **逐字一致**（确认是 0144 的列，不是别的漂移）；
2. 确认 357 行数据中该列**非空行数 = 0**（可空列 ⇒ 存量语义不变）；
3. 确认 0145 / 0146 / 0148 的前置对象**不存在**（未半途应用）；
4. `docker stop qa-backend` ⇒ `ALTER TABLE session_query_state DROP COLUMN semantic_release_id;`
   ⇒ 重启 ⇒ 迁移链 `0143 → 0148` 干净应用。

**生产无此风险**：生产在 `0143` 且**没有**这一列（从未跑过 0144）。

**教训**：`alembic stamp` 只改版本号、不改库结构 ⇒ **绝不能用 stamp 回退一个已应用过 DDL 的库**；
回退只能用 `downgrade`（`alembic downgrade <rev>`）。

## 4. `alembic check` 的预存漂移（非本次引入）

本地 dev 库跑 `alembic check` 仍报漂移，**全部是 0144–0148 之前就存在**的 dev-only 残留，
与本次迁移链无关：

- 9 张 dev-only 表：`BPSUPPLIER`、`PORDER`、`PORDERQ`、`*_20260930`、`*_20260918`；
- 索引 `ix_wiki_compile_task_created` 的**表达式**差异；
- 外键 `fk_wiki_token_usage_compile_task`。

⇒ **本地 dev 库不是干净库**；判定迁移链健康要看 `alembic_version` + 迁移链自身，
不要把 `alembic check` 当本地绿灯判据（干净库须 `--strict` 全新建库验证，见 PR #97/#98）。

## 5. 迁移号串行分配（③）

两条线都要 `0149+` ⇒ 串行占号，避免 alembic 链分叉（**占号结果已定，2026-11-17**）：

| 线 | 迁移区间 | 说明 |
|---|---|---|
| **P5**（Workflow / Command / Approval） | **`0149` – `0152`** | 拆 **4** 个迁移：`0149` 定义/实例 · `0150` 步骤/命令 · `0151` 审批/outbox · `0152` 放宽 `audit_log` CHECK |
| **WP-ONT-AI**（A2 / A3 / A4） | **`0153` +** | A2a 已占 0146–0148；A2/A3/A4 从 `0153` 起顺延（**不得**回插 0153 以下） |

**占号规则**：先合并者占号；后合并者若已越过对方区间 ⇒ **改号 + 重排 `down_revision`**，不得分叉。
占号是**预留**，不是授权开工；WP 的 A2/A3/A4 仍各自卡在业务答复 Q3/Q4/Q5。

## 6. 1114 / 1116 部署线收口（②）

| 线 | 状态 | 证据 |
|---|---|---|
| **1114 生产发版** | 代码侧已合；**生产未再动**（按 §1 边界） | 交付文档已入库：[`../2026-11-14-prod-release-1114/deploy-prod-1114.md`](../2026-11-14-prod-release-1114/deploy-prod-1114.md)、[`../2026-11-14-prod-release-1114/fix-kpi-answer-prod.md`](../2026-11-14-prod-release-1114/fix-kpi-answer-prod.md) |
| **1115 类墓碑占名热修** | 代码已合（`main @ 10c79bb3`，PR #128），**零 DDL**；生产 runbook 入库 | [`../2026-11-14-fix-import-tombstone-name-guard/deploy-prod-1115.md`](../2026-11-14-fix-import-tombstone-name-guard/deploy-prod-1115.md) |
| **1116 向量同步热修** | 代码已合（PR #143 → `main @ 86ff4a1f`；MissingGreenlet 事故修复 + CI 闸门修复）；**生产未切镜像** | [`../2026-11-16-fix-ontology-bg-vector-sync/summary.md`](../2026-11-16-fix-ontology-bg-vector-sync/summary.md) |

⇒ 三条线的**代码与交付物**都已收口入库；**生产镜像统一切换**推迟到 1109 需求整体完成后一次性做。

## 7. 回滚锚点

| 镜像 | ID |
|---|---|
| **当前运行** backend / frontend | `sha256:f2ffc5e47b10…` / `sha256:3700974a6111…`（`main @ 86ff4a1f`） |
| 上一档 backend / frontend | `sha256:80964c359d44…`（11-16 14:22）/ `sha256:2aac322be798…`（11-16 12:10） |
| 更早一档（11-14）backend / frontend | `sha256:2c0474bd0b67…` / `sha256:83be9657c807…` |

**库回滚**：`alembic downgrade 0143`（逐级）—— **不要用 `stamp`**（见 §3 教训）。

## 8. 本记录不做的事

不部署生产、不发 Q3/Q4/Q5 征集、不改生产 `.env`、不新增镜像/中间件/依赖、
不预先授权 WP-ONT-AI A2/A3/A4 开工（只占号）。

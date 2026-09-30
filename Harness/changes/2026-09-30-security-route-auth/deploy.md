# 部署 runbook（2026-09-30）——**已执行**

> 用户指令：「需要部署，保证 docker 的 frontend/backend 的代码是最新的」
> 原定执行时机：**Task 1（46 条路由鉴权）合入 main 之后**，一次性部署最新，避免部署两次。
> **状态：已执行并复核**（2026-09-30 复核；见下面「四、复核结果」）。
> 本文从「待执行 runbook」转为**历史记录**——保留迁移安全性分析与备份清单（这两块
> `summary.md` 没有），把当时写的「部署前现状」标注为历史值，并补上实测的落地结果。

---

## 一、执行前现状（**历史值**，2026-09-30 早）

> ⚠️ 下表是**执行当时**的快照，不是现状。当时 prod 落后 7 个迁移，容器代码有漂移。
> 现状见「四、复核结果」。

| 项 | 当时实测值 |
|---|---|
| 代码 alembic head | **0104** |
| 生产库当时版本 | **0097**（落后 7 个迁移） |
| 后端容器 | `qa-backend`，`DATABASE_URL=postgresql+asyncpg://qa_user:<REDACTED>@postgres:5432/qa_metadata` |
| 前端容器 | `qa-frontend`，`5173:80` |
| 容器代码漂移 | 与工作树 5 个文件不一致（`chat.py` / `evidences.py` / `reports.py` / `wiki.py` / `wiki_compile.py`） |

> 口令一律从容器 env 读（`docker exec qa-backend printenv DATABASE_URL`），**本文件不落明文**。

**落后的 7 个迁移**：`0098` evidence.claim_id nullable、`0099` knowledge_claim.source_version、
`0100` knowledge_claim.confidence_level、`0101` session_query_state.inheritance_snapshot、
`0102` authority_department（knowledge_claim + wiki_page）、`0103` analysis_hypothesis、
`0104` report_instance。

### 迁移安全性（已逐个核对 upgrade 函数体）

**7 个迁移的 upgrade 全部是非破坏性的**：

- `0098` upgrade 只有 `alter_column(nullable=True)`；`DELETE FROM evidence WHERE claim_id IS NULL`
  在 **downgrade()** 里（第 45 行），upgrade 不执行
- `0099/0100/0101/0102` 的 `add_column` 均为 **nullable 列**（无 `nullable=False`）
- `0103/0104` 是 `create_table`（新表，`nullable=False` 无妨）
- 所有 `op.drop_column` / `op.drop_table` 均在 `downgrade()`

⇒ 可直接 `alembic upgrade head`，不会因「给有数据的表加 NOT NULL 列」而失败。

---

## 二、执行步骤

### Step 1：前置备份（项目策略：`qa-system-two-dbs-policy`）

受影响且**已存在**的表先备份（新表 `analysis_hypothesis` / `report_instance` 不存在，无需备份）：

```sql
CREATE TABLE evidence_20260930              AS SELECT * FROM evidence;
CREATE TABLE knowledge_claim_20260930       AS SELECT * FROM knowledge_claim;
CREATE TABLE session_query_state_20260930   AS SELECT * FROM session_query_state;
CREATE TABLE wiki_page_20260930             AS SELECT * FROM wiki_page;
```

**先记录每张表的行数**，迁移后对账。

### Step 2：后端

首选（正确方式，固化进镜像）：

```bash
docker compose -f docker/docker-compose.yml build backend
docker compose -f docker/docker-compose.yml up -d backend
```

容器 CMD 是 `alembic upgrade head && uvicorn`，**自动迁移到 head**。

退路（Docker Hub 不可达时，见脚本自身 docstring）：

```bash
./scripts/deploy_backend.sh          # 灌 app/ + scripts/ + alembic/ 三处，重启并等启动成功
./scripts/deploy_backend.sh --rollback   # 失败时回滚
```

⚠️ `docker cp` 是临时手段：容器一旦重建（compose up / build）灌进去的代码全部丢失。

### Step 3：前端

```bash
docker compose -f docker/docker-compose.yml build --no-cache frontend
docker compose -f docker/docker-compose.yml up -d frontend
```

两个必须项（均有踩坑记忆）：

- **`--no-cache` 必须**（`qa-system-frontend-deploy-build-required`）：`npm run build` ≠ 容器 bundle
- **npm 源必须换 npmmirror**（`qa-system-frontend-build-npm-registry`）：默认源 0.47MB/s 必撞 EIDLETIMEOUT；npmmirror 提速 21 倍

### Step 4：nginx upstream 刷新

后端容器重建会换 IP；若 nginx 缓存了旧 upstream IP，`/api` 会全 502、前端菜单退回 `FALLBACK_NAV`
（`qa-system-nginx-upstream-ip-stale`）。前端容器重建后其内置 nginx 会重新解析，若仍 502 则需显式重启。

---

## 三、验收标准

1. `docker exec qa-backend alembic current` → 与代码 head 一致
2. 4 张备份表行数与迁移前一致
3. **安全验收（本批次核心）**：原匿名路由无鉴权头 → **401/403**；
   应公开路由仍可达（`/health`、`/auth/login`、`/auth/password-policy`、`/data-quality/reports/share/{token}`）
4. `/api/v1/health` → 200
5. 前端菜单正常加载（非 `FALLBACK_NAV`）
6. 容器代码漂移消失：5 个文件与工作树一致
7. （Task 2 追加）`/mcp` 无鉴权头 → 403

---

## 四、复核结果（2026-09-30，本次实测）

| 验收项 | 结果 | 依据 |
|---|---|---|
| 1. alembic 到 head | ✅ | prod `alembic_version` = **0105**；容器 `alembic current` = `0105 (head)`；代码 head = 0105 |
| 2. 备份表存在 | ✅（**未验行数**） | `evidence_20260930` / `knowledge_claim_20260930` / `session_query_state_20260930` / `wiki_page_20260930` 均在（另有 `session_message_bak_20260930` 属另一操作）。**迁移前计数没有留存 ⇒ 行数对账这一项无法补做** |
| 3. 安全验收 | ✅（**抽样，未穷举 46 条**） | 无鉴权头 403：`/evidences`、`/reports`、`/ontology/classes`、`/term-dictionary`、`/data-quality/rules`、`/lineage`、`/agents`、`/sessions`、`/menu-config`；公开可达：`/health` 200、`/auth/password-policy` 200、`/auth/login` 422（可达、缺参） |
| 4. health | ✅ | `/api/v1/health` → 200 |
| 5. 前端菜单 | ⚠️ **未验** | 需浏览器；`/` 返回 200 仅证明容器在跑 |
| 6. 漂移消失 | ✅ | 7 个文件 `sha256sum` 逐一对齐（原 5 个漂移文件 + `session.py` + `session_guard.py`） |
| 7. `/mcp` 鉴权 | ✅ | 无鉴权头 `POST /mcp` → 403；`McpAuthMiddleware` 已接线（`main.py:557-561`）；`test_mcp_auth.py` + `test_app_wiring.py` = **7 passed** |

**结论**：本批次（Task 1 + Task 2 + Task 3）源码、迁移、容器三者已闭合，验收项无未通过项；
两项未验（备份表行数、前端菜单）已如实标注，不冒充已验证。

> 复核手法提示：容器内**没有** `shasum`（会输出 `OCI` 而非哈希，是假阴性）；比对文件用
> `sha256sum`。相关：`qa-system-docker-cp-merge`。

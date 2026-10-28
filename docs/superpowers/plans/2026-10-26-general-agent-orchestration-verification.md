# 通用多工具 Agent 编排 —— 验证报告

- **变更**：[feat-agent-orchestration](../../Harness/changes/feat-agent-orchestration/summary.md)
- **分支**：`feat/general-agent-orchestration`
- **BASE（Task 16 起算）**：`7ce07bc522d585d90093651c3c4ec61b8053e5f4`
- **基线对照**：`4d4114bb`
- **日期**：2026-10-28
- **执行者**：Task 16（验证记录 + SSOT 文档 + 交接）

> **总口径**：本报告只记录**实际执行过**的验证。未执行的项一律列在 §7，
> 不计入「通过」。**生产未部署**，灰度是人工执行的手册。

---

## 1. 验证范围

| # | 验证项 | 手段 | 结论 |
|---|---|---|---|
| V1 | 后端静态检查 | `ruff check`（14 个新文件） | ✅ 通过 |
| V2 | 后端集成测试 | 独立 pytest session | ✅ 107 passed |
| V3 | 后端单元测试 | 独立 pytest session | ✅ 231 passed |
| V4 | 新代码覆盖率 | 两个 session `--cov-append` | ✅ 94.60%（门槛 80%）；控制方 15 模块口径曾测得 94.44%（清单未存档、不可复算） |
| V5 | 前端类型检查 | `npx tsc -p tsconfig.json --noEmit` | ✅ exit 0 |
| V6 | E2E 类型检查 | `npx tsc -p e2e/tsconfig.json --noEmit` | ✅ exit 0 |
| V7 | 前端定向单测 | 6 个 Task-16 测试文件 | ✅ 78 passed |
| V8 | 前端全量单测 | `vitest run` | ⚠️ 7 failed（**净新增 0**，基线 9 failed） |
| V9 | Playwright（mock） | `npx playwright test` | ⚠️ **27 failed**（基线也是 27，净新增 0）—— **不可称「E2E 通过」** |
| V10 | compose 冒烟（特性关闭） | `docker compose config --services/--images` + `docker ps` | ✅ Worker **未**启动、**不**在默认服务清单 |
| V11 | `/openapi.json` 端点冒烟 | 进程内 + 真实 HTTP | ✅ 关闭时 9 条路径**全部缺席**（404） |
| V12 | 真实数据脚本 | `backend/scripts/feat_agent_orchestration_realdata.py`（+ `_lib.py` / `_db.py` 两个 helper 模块） | ✅ **连跑两次都是 exit 0**，PASS/SKIP 集合一致（20 PASS / 0 FAIL / 0 SKIP）；目标库 = 隔离测试库 `5436/qa_metadata_test`（见 §6.2） |

---

## 2. 后端验证（真实 PostgreSQL）

**环境**：容器 `qa-pg-orchestration`，`localhost:5436/qa_metadata_test`（**隔离测试库**）；
Neo4j `bolt://localhost:7688`。**未触碰** dev 库（5433）与生产。

> ⚠️ 下面命令里的 `qa_user:***@…` 是**打码**形态：`*` 代表真实口令。
> 实跑时把 `***` 换成隔离测试库的真实口令（或按 §6.2 的 `<user>:<pwd>` 占位符形态写），
> 照抄 `***` 会**认证失败**。口令**不写进**仓库、`.env` 或任何文档。

> **两个 pytest session 必须分开跑**：集成与单测的 seed fixture 会 TRUNCATE/INSERT
> **同一个共享测试库**。混在一个 session 里必然撞出 1 failed + 9 ERRORS
> （`UniqueViolationError` on `uq_feature_rule_scope_code`、
> `ForeignKeyViolationError` on `fk_business_object_primary_domain`）。
> 两位评审独立复现过同一现象 ⇒ 是**测试库争用**，不是分支缺陷。

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:***@localhost:5436/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
./.venv/bin/python -m pytest app/tests/integration -q
```

```
107 passed, 24 warnings in 91.06s
```

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:***@localhost:5436/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
./.venv/bin/python -m pytest app/tests/unit -q
```

```
231 passed in 91.27s
```

**静态检查**：

```bash
cd backend
./.venv/bin/ruff check app/services/agent_orchestration_*.py app/domain/agent_orchestration_*.py \
  app/api/v1/agent_orchestrations.py app/api/v1/agent_runs.py app/workers/agent_orchestration_worker.py \
  scripts/feat_agent_orchestration_realdata.py \
  scripts/feat_agent_orchestration_realdata_lib.py \
  scripts/feat_agent_orchestration_realdata_db.py
```

```
All checks passed!
```

### 2.1 覆盖率

```bash
cd backend
# integration session
./.venv/bin/python -m pytest app/tests/integration -q --cov=app.services.agent_orchestration_* \
  --cov=app.domain.agent_orchestration_* --cov=app.api.v1.agent_orchestrations \
  --cov=app.api.v1.agent_runs --cov=app.workers.agent_orchestration_worker --cov-append
# unit session（同样参数，再 --cov-append）
```

> **覆盖率口径**：上面这条命令展开是 **17 个模块** ⇒ 产出 **94.60%**；控制方另按
> **15 个模块**测得 **94.44%**，但**该 15 模块清单未随仓库存档** ⇒ **不可复算**；
> 两者差异**只来自模块集合**，都远超 80% 门槛，**可复算的门禁口径是上面这条命令的
> 94.60%**。

```
TOTAL 2204 stmts / 119 miss / 95%
Required test coverage of 80.0% reached. Total coverage: 94.60%
```

> **两个口径**：控制器口径（15 个模块）= `2083 stmts / 117 miss / **94.44%**`
> （**该 15 模块清单未随仓库存档 ⇒ 不可复算**，见上方口径说明）；
> Task 16 实施者口径（17 个模块）= `2204 stmts / 119 miss / 95%（94.60%）`。
> 差异来自**模块集合不同**（多算 2 个模块），**不是回归**。两者都远超 80% 门槛。

---

## 3. 前端验证

```bash
cd frontend
npx tsc -p tsconfig.json --noEmit      # exit 0
npx tsc -p e2e/tsconfig.json --noEmit   # exit 0
```

> ⚠️ 简报里的 `npm run typecheck` **不存在**；`npx tsc -b --noEmit` 会报 `TS6310`
> （多项目引用不能配 `--noEmit`）。可用命令是 `npx tsc -p tsconfig.json --noEmit`。

**定向单测**（6 个 Task-16 测试文件）：

```
Test Files  6 passed (6)
Tests       78 passed (78)
```

**全量 vitest**：

```
Test Files  3 failed | 192 passed (195)
Tests       7 failed | 1844 passed (1851)
```

7 条失败：`client.test.ts`(1) / `kpiCatalogApi.test.ts`(1) / `ontologyApi.test.ts`(5)。

**基线对照**（`4d4114bb`）：191 文件 / 1773 测试 / **9 failed**。
⇒ 本分支 7 条 ⊂ 基线 9 条 ⇒ **净新增失败 = 0**。

---

## 4. Playwright（mock）—— **27 红，不是绿的**

```bash
cd frontend
npx playwright test
```

| | 分支 | 基线 `4d4114bb` |
|---|---|---|
| spec 数 | 11 | 10 |
| 测试数 | 33 | 29 |
| passed | 5 | 1 |
| skipped | 1 | 1 |
| **failed** | **27** | **27** |

- **失败用例名清单前后完全一致（27 = 27，diff 为空）**；
- 本分支新增的 4 条 `e2e/agent-orchestration.spec.ts` **全绿**；
- **净新增失败 = 0**。

> **口径声明**：本仓 Playwright 是**浏览器级 mock**
> （`e2e/support/mockApi.ts` 拦截 `/api/v1/**`，`playwright.config.ts` 只起 Vite）。
> ⇒ **真实栈浏览器冒烟未执行**（见 §7）。
> **绝不能**把这条记成「E2E 通过」—— mock 套件本身 27 红。

---

## 5. 部署形态冒烟（特性关闭）

```bash
cd <含 docker-compose.yml + docker-compose.centos.yml 的目录>
# .env 用合成假值，不读真 docker/.env

docker compose -f docker-compose.yml -f docker-compose.centos.yml config --services
docker compose -f docker-compose.yml -f docker-compose.centos.yml --profile agent-orchestration config --services
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep agent-orchestration
docker compose -f docker-compose.yml -f docker-compose.centos.yml config --images
```

```
=== ① 默认 config --services ===
neo4j / ollama / postgres / qa-objects / milvus-minio / milvus-etcd / milvus-standalone / backend / frontend / mysql-demo
--- worker 命中数 ---
0
=== ② --profile agent-orchestration config --services ===
postgres / milvus-etcd / milvus-minio / milvus-standalone / neo4j / backend / frontend / qa-objects / agent-orchestration-worker / mysql-demo / ollama
--- worker 命中数 ---
1
=== ③ docker ps agent-orchestration ===
0
=== ④ config --images（不带 profile）===
0
```

⇒ 默认 `up` **永不**包含 Worker；Worker 只在显式 `--profile agent-orchestration` 时出现，
且**不发布宿主端口**、**不在 `depends_on`** 里。

---

## 6. 端点与真实数据冒烟

### 6.1 `/openapi.json`

**进程内**（`createApp().openapi()`）：

```
=== feature-OFF ===
flag = false
total_paths = 252
orchestration_paths = 0
json_dump = []

=== feature-ON ===
flag = true
total_paths = 261
orchestration_paths = 9
   /api/v1/agent-orchestrations ['get', 'post']
   /api/v1/agent-orchestrations/tool-options ['get']
   /api/v1/agent-orchestrations/{agentCode} ['get', 'put']
   /api/v1/agent-orchestrations/{agentCode}/toggle ['post']
   /api/v1/agent-runs ['get', 'post']
   /api/v1/agent-runs/{runId} ['get']
   /api/v1/agent-runs/{runId}/cancel ['post']
   /api/v1/agent-runs/{runId}/resume ['post']
   /api/v1/agent-runs/{runId}/steps ['get']
```

**真实 HTTP**（uvicorn `127.0.0.1:8099`，连隔离库 5436，`AUTH_MODE=stub`）：

```
=== feature-OFF openapi ===
total_paths = 252
orchestration_paths = 0
[]
=== feature-OFF 端点冒烟（期望 404）===
/api/v1/agent-orchestrations -> 404
/api/v1/agent-runs -> 404

=== feature-ON openapi ===
total_paths = 261
orchestration_paths = 9
=== feature-ON 端点冒烟（期望 200，stub auth）===
/api/v1/agent-orchestrations -> 200
/api/v1/agent-runs -> 200
=== tool-options body ===
[]
```

> **判据**：特性关闭必须是 **404**（路由未注册，不进鉴权层），不是 401。
> `tool-options == []` 正确 —— 隔离库里 7 个工具的 `input_schema` 全是 `{}`（阶段 0 之前）。

### 6.2 真实数据脚本

**目标库（canonical）= 隔离测试库 `localhost:5436/qa_metadata_test`** —— **不是** dev 库
`5433/qa_metadata`，**更不是**生产。脚本的 DSN 解析顺序（先命中先用）：
`AGENT_ORCHESTRATION_REALDATA_DATABASE_URL` → **`TEST_DATABASE_URL`（本次用的就是它）** →
`DATABASE_URL` → `PG*`（默认端口 **5436** / 默认库 **`qa_metadata_test`**）。

⚠️ **裸跑默认落到隔离库只在「环境里没有 `DATABASE_URL`」时成立**：`DATABASE_URL`
会按上面的顺序被命中，而开发者的 shell 里通常正导出着 dev 库的
`DATABASE_URL=…@localhost:5433/qa_metadata`。脚本因此在**接触数据库之前**有硬守卫：
目标库名**不以 `qa_metadata_test` 开头** ⇒ **拒绝运行**（写 stderr、非零码退出、
一个字节都不写）；只有用 `AGENT_ORCHESTRATION_REALDATA_DATABASE_URL` 显式覆盖时才放行。

**前置条件与 SKIP 语义**：库种子不满足前提的步骤输出 `SKIP  <步骤> — <缺什么>`，
**不是** `FAIL`，也**不进**失败清单（库的种子状态不是脚本的缺陷；退出码只由 `FAIL` 决定）。
`S7` 需要一个 `status='active'` 且尚无编排 profile 的 `agent_definition`（没有则脚本**自建**，
退出时删除），`S8` 需要一个 `is_active` 的 `llm_config`（没有则脚本**自建**，退出时删除）。
`S4` 的禁用分支拿**真实行的副本**置 `enabled=False` 来验证（`assertOrchestrationToolValid`
是纯函数，**不写库**），输出里明说这一点。`S5` 的「写入 N 行」按 UPDATE 的**实际命中
行数**汇报（第一波只写 `enabled=true` 的行；某工具被禁用 ⇒ 该行没写成 ⇒ 按
「未写入 + 原因」汇报，`S6` 随后会 `FAIL`，运行**不是**绿的）。**SKIP 与 FAIL 的
分类依据是异常的具体类型**：`NotFoundError` / `ConflictError` / `ValidationError`
⇒ SKIP 并写明缺什么；其它异常 ⇒ FAIL——这条规则覆盖 **5 处分类点**，分两类：
① **服务调用**（建 profile / toggle / 建 run）——`realdata.py` 的 3 处
（`:474` / `:519` / `:572`）；② **准备前置条件**（自建 ACTIVE Agent / 启用的
模型配置）——`_db.py` 的 2 处（`:365` / `:394`）。`IntegrityError` /
`OperationalError` 这类真缺陷在这里会记 FAIL 并让退出码非零，**不会**被当成
「库种子不满足」蒙混成 SKIP。**准备前置条件**不满足时输出 `SKIP`，
**不会**抛未捕获的 traceback。
其它步骤（S1/S2/S3/S4/S5/S6/S9/S10 与退出前的清理块）
**没有**这一层分类——那里冒出未预期异常会**以未捕获的 traceback 直接冒出来**
（`main()` 只有 `try/finally`、**没有** `except`），这是**刻意的**：脚本不吞异常。
（`S3` / `S4` 只捕获 `ValidationError` 当作**预期被拒的断言**，**不是** SKIP/FAIL
分类；那里冒出**其它**异常，同样以未捕获的 traceback 冒出来。）
`S6` 断言的是「**脚本没有翻转任何 `enabled`**」，
判定文本报出**观测到的运行前状态**（不同库种子不同）。

⚠️ **有跳过步骤的运行不会说「all steps passed」**：结尾汇总会另外打印
`NOTE  M step(s) skipped — 见上方 SKIP 行（SKIP ≠ 通过，也不算失败）` 并逐项列出
被跳过的步骤；**只有 0 失败且 0 跳过时**才会打印 `OK  all steps passed`。
退出码仍然**只由 `FAIL` 决定**（`SKIP` 不影响退出码）——「跳过不是通过」由**汇总
文案**表达，而不是靠改退出码（幂等口径「退出码相同 + PASS/SKIP 集合相同」依赖这一点）。

```bash
cd backend
TEST_DATABASE_URL='postgresql+asyncpg://<user>:<pwd>@localhost:5436/qa_metadata_test' \
  ./.venv/bin/python scripts/feat_agent_orchestration_realdata.py
```

> ⚠️ `<user>` / `<pwd>` 是**占位符**：把它换成隔离测试库的真实账号与口令。
> 口令**不要**写进仓库、`.env` 或任何文档；下面 stdout 里出现的 `qa_user:***@…` 是
> **打码**形态（脚本自己的 `TARGET` 输出也是这个形态），照抄 `***` 会**认证失败**。

**完整 stdout（逐行未删改）**：

```
TARGET  postgresql+asyncpg://qa_user:***@localhost:5436/qa_metadata_test  (from TEST_DATABASE_URL)
MARKER = feat-agent-orchestration-realdata
------------------------------------------------------------------------
PASS  S1 迁移版本 — version_num=0135 (期望 0135)
PASS  S1 五张新表 — 存在 5/5
PASS  S2 既有表基线行数 — agent_definition=0, agent_access_policy=0, agent_tool_config=7, agent_schedule=0, agent_run_log=0, session_token_usage=0
PASS  S3 空 schema 被拒 — ValidationError: tool input schema root type must be 'object'
PASS  S3 合法 schema 通过 — supplier_360 的 §2.3 schema 被接受
      wiki_search 真实行 enabled=True（脚本全程不写 enabled）
PASS  S4 禁用工具被拒 — message=工具已禁用：wiki_search (期望 '工具已禁用：wiki_search')；针对真实行的副本置 enabled=False 验证禁用分支（未写库）
      supplier_360: input_schema {} -> {"type": "object", "properties": {"key": {"type": "string"}}, "additionalProperties": false}（enabled 保持 True 未改）
      supplier_risk: input_schema {} -> {"type": "object", "properties": {"key": {"type": "string"}}, "additionalProperties": false}（enabled 保持 True 未改）
      graph_traverse: input_schema {} -> {"type": "object", "properties": {"key": {"type": "string"}}, "additionalProperties": false}（enabled 保持 True 未改）
PASS  S5 schema-only 修复 — 实际写入 3 行（enabled 未翻转；退出前会还原）
PASS  S6 三个已启用工具可编排 — supplier_360=True, supplier_risk=True, graph_traverse=True
PASS  S6 脚本没有翻转任何 enabled — wiki_search: pre enabled=True → now enabled=True；wiki_read: pre enabled=True → now enabled=True；rule_evaluate: pre enabled=True → now enabled=True；coverage_status: pre enabled=True → now enabled=True（脚本全程不写 enabled；观测到的运行前状态如上，不同库种子不同——enabled=true 的库说明该库把 wiki 工具种成了启用态）
PASS  S6 只读白名单仍是 7 项 — READ_ONLY_HANDLER_REFS=['coverage_status', 'graph_traverse', 'rule_evaluate', 'supplier_360', 'supplier_risk', 'wiki_read', 'wiki_search']
      使用 Agent：REALDATA_ORCHESTRATION_AGENT（脚本自建（退出时删除））
PASS  S7 profile 创建后为停用 — agent=REALDATA_ORCHESTRATION_AGENT enabled=False revision=1
PASS  S7 绑定的是三个只读工具 — bindings=['graph_traverse', 'supplier_360', 'supplier_risk']
PASS  S7 toggle 后启用且 revision=2 — enabled=True revision=2
      使用模型配置：id=4（脚本自建（退出时删除））
PASS  S8 run 创建为 QUEUED — run_id=343cab6b-0f61-4c41-8c70-d844070b7dfc status=QUEUED
PASS  S8 run_id 非空 — run_id=343cab6b-0f61-4c41-8c70-d844070b7dfc
PASS  S8 快照只含只读白名单工具 — snapshot_tools=['graph_traverse', 'supplier_360', 'supplier_risk']（无任何写工具）
      run_id=343cab6b-0f61-4c41-8c70-d844070b7dfc（由本脚本创建；Worker 若在跑会接手该 run）
PASS  S9 feature-off 时新路由缺席 — /openapi.json 中的编排路径 = []
      删除自建 run: run_id=343cab6b-0f61-4c41-8c70-d844070b7dfc status=QUEUED
      删除自建 profile: agent_id=4 enabled=True revision=2
PASS  S10 自建行已清理 — 自建 run / profile / Agent / 模型配置均已删除
PASS  S10 阶段 0 schema 已还原 — 3 行 input_schema 已写回运行前的原值
PASS  S2 既有表行数前后一致 — 六张既有表行数无变化
------------------------------------------------------------------------
OK  all steps passed
```

**退出码 `0`**（stderr 为空）。

**幂等的口径**：**连续两次运行 ⇒ 退出码相同 + PASS/SKIP 集合相同**（`FAIL` 集合为空）。
上面这段是**运行 1**；**运行 2** 用同一条命令紧接着再跑一次，**退出码同样是 `0`**，
stdout 与运行 1 **逐行一致**，只有三个每次必然不同的自增/随机值不同
（模型配置 id `4`→`5`；`run_id` `343cab6b-…`→`137c8f7f-…`；S10 打印的 `agent_id` `4`→`5`）。
两次的 PASS/SKIP 集合 `diff` **为空**（各 20 PASS / 0 FAIL / 0 SKIP）。

> **本次两次运行都没有出现 `SKIP` 行** —— 隔离库已种好 7 个内置工具，脚本又能自建
> ACTIVE Agent 与启用的模型配置，所以 S4/S6/S7/S8 都**真正执行**了。若在别的库上跑
> 而出现 `SKIP`，那一行会写明缺什么（见上方「SKIP 语义」）。

> **前置条件不满足时的行为（单独实测，非 canonical）**：在同一隔离测试库上临时把
> `supplier_360` 置成 `enabled=false` 跑了一次，**跑完立刻改回 `enabled=true`**
> （改前改后都核验过：7 个工具全部 `enabled=true`、`input_schema` 全部 `{}`、
> 其余表零残留）。结果是 **S5 `SKIP`（如实写「实际写入 2 行，未写入 1 行」）、
> S6 `FAIL`、S7 `SKIP`（写明「工具已禁用：supplier_360」）、S8 `SKIP`（原因**照抄
> S7 的真实原因**，不再臆断成「没有可用的 ACTIVE Agent」）**，
> **无 traceback、stderr 为空、退出码 1** ⇒ 运行**不是**绿的。
> 结尾汇总是 `FAILED  1 step(s):` + `NOTE  3 step(s) skipped …`（逐项列出），
> **没有** `OK  all steps passed`。

> **非 canonical 观察（旧记录）**：交付时曾在 **dev 库 `5433/qa_metadata`** 上跑过一次并
> 得到全 PASS。该次运行**不是** canonical 证据——脚本自己的规矩是「绝不碰 dev 库」，
> 而那次运行**永久改写了 3 行 `input_schema` 且没有还原**（当前版本的脚本会在退出前
> 把写过的 `input_schema` 写回原值，不再留残留）。
> 该次记录**已被上面的隔离库证据取代**。
>
> ⚠️ **dev 库至今仍带着那次旧运行留下的残留**：那 3 行
> （`supplier_360` / `supplier_risk` / `graph_traverse`）的 `input_schema` 仍是当时
> 写入的值。**这是刻意不清理的**——本特性不碰 dev 库，清理动作超出本变更范围
> （残留本身是良性的：schema-only 正是灰度手册阶段 0 规定的动作）。

---

## 7. 未执行项（**不得**当作已完成）

| 项 | 状态 | 原因 |
|---|---|---|
| 生产部署 | ❌ 未执行 | 人工操作（[灰度手册](../../Harness/changes/feat-agent-orchestration/deploy-gray-rollout.md)）。本次**没有**碰生产机、**没有**改 `/opt/qa-system/docker/.env` |
| 生产 `ENABLE_AGENT_ORCHESTRATION=true` | ❌ 未执行 | 同上；PRE-ENABLE CHECKLIST 三项未关闭 |
| 真实栈浏览器冒烟 | ❌ 未执行 | Playwright 是浏览器级 mock（`e2e/support/mockApi.ts` 拦 `/api/v1/**`），打不出真栈 |
| Worker 端到端跑完一条真实 run | ❌ 未执行 | 需要启用的 profile + 可用 LLM + 起 Worker 进程。本次只验证到「建 run ⇒ QUEUED ⇒ 快照正确 ⇒ 清理」 |
| 菜单播种在生产执行 | ❌ 未执行 | 人工操作（手册阶段 6） |

---

## 8. 残留风险

### 8.1 生产启用前必须关闭（PRE-ENABLE CHECKLIST，详见手册 §2.5 / §3.5）

| # | 风险 | 级别 |
|---|---|---|
| P1 | 只读白名单 TOCTOU（两次读之间 `handler_ref` 可被改） | MEDIUM |
| P2 | 租约只在轮边界续；LLM 读超时可达 600s 而租约 60s ⇒ 丢租约重排队、重复 LLM 花费 | MEDIUM |
| P3 | Planner 工具目录无上界（`description` 无界 Text / `tool_names` 无个数上限） | MEDIUM |

### 8.2 已记账的已知限制（11 项）

见 [summary.md §7.1](../../Harness/changes/feat-agent-orchestration/summary.md)。

### 8.3 超出设计稿范围的改动

`frontend/src/api/client.ts` 的 `normalizeError` 现在把响应体顶层 `error` 抬升进
`AppError.message`（共享模块，影响全站）。**这是设计稿 §9.3 允许范围之外的改动**，
已在 summary.md §4.4 记账。

### 8.4 部署纪律：阶段 0 不可翻 `enabled`

4 个 wiki 工具（`wiki_search` / `wiki_read` / `rule_evaluate` / `coverage_status`）绑着
4 个 `status=active` 的既有 legacy Agent（id 6/7/8/9）。legacy registry 只装载
`enabled=true` 的行（`agent_tool_config_registry.py:33,79`），否则 409
（`agent_runtime_service.py:165-178`）⇒ **翻开关 = 让 4 个既有 Agent 从 409 变成真的执行**。
第一波**只补 `input_schema`**（对 legacy 路径无副作用）；翻开关推到后续波次单独审批。

---

## 9. 结论

- **净新增失败 = 0**（后端 / 前端 / Playwright 三套均为 0）。
- **新代码覆盖率 94.60%**（门槛 80%）；控制方 15 模块口径曾测得 94.44%（清单未存档、不可复算）。
- **默认关闭、Worker 不自启、路由不注册**三条安全姿态均已实测。
- **真实数据脚本 exit 0 且幂等**，证明只读约束与「不改既有功能」。
- **3 个 MEDIUM 安全项刻意挂账** ⇒ 已进 PRE-ENABLE CHECKLIST（现对应 P1–P3），作为生产启用前的硬闸门。
- **生产未部署**；灰度由人按手册执行。

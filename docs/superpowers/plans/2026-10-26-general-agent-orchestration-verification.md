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
| V4 | 新代码覆盖率 | 两个 session `--cov-append` | ✅ 94.44%（门槛 80%） |
| V5 | 前端类型检查 | `npx tsc -p tsconfig.json --noEmit` | ✅ exit 0 |
| V6 | E2E 类型检查 | `npx tsc -p e2e/tsconfig.json --noEmit` | ✅ exit 0 |
| V7 | 前端定向单测 | 6 个 Task-16 测试文件 | ✅ 78 passed |
| V8 | 前端全量单测 | `vitest run` | ⚠️ 7 failed（**净新增 0**，基线 9 failed） |
| V9 | Playwright（mock） | `npx playwright test` | ⚠️ **27 failed**（基线也是 27，净新增 0）—— **不可称「E2E 通过」** |
| V10 | compose 冒烟（特性关闭） | `docker compose config --services/--images` + `docker ps` | ✅ Worker **未**启动、**不**在默认服务清单 |
| V11 | `/openapi.json` 端点冒烟 | 进程内 + 真实 HTTP | ✅ 关闭时 9 条路径**全部缺席**（404） |
| V12 | 真实数据脚本 | `backend/scripts/feat_agent_orchestration_realdata.py` | ✅ exit 0，全部 PASS |

---

## 2. 后端验证（真实 PostgreSQL）

**环境**：容器 `qa-pg-orchestration`，`localhost:5436/qa_metadata_test`（**隔离测试库**）；
Neo4j `bolt://localhost:7688`。**未触碰** dev 库（5433）与生产。

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
  scripts/feat_agent_orchestration_realdata.py
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

```
TOTAL 2204 stmts / 119 miss / 95%
Required test coverage of 80.0% reached. Total coverage: 94.60%
```

> **两个口径**：控制器口径（15 个模块）= `2083 stmts / 117 miss / **94.44%**`；
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

```bash
cd backend
./.venv/bin/python scripts/feat_agent_orchestration_realdata.py
```

```
DSN  = postgresql+asyncpg://qa_user:***@localhost:5433/qa_metadata
PASS  S1 迁移版本 — version_num=0135 (期望 0135)
PASS  S1 五张新表 — 存在 5/5
PASS  S2 既有表基线行数 — agent_definition=9, agent_access_policy=12, agent_tool_config=7, agent_schedule=0, agent_run_log=0, session_token_usage=2413
PASS  S3 空 schema 被拒 — ValidationError: tool input schema root type must be 'object'
PASS  S4 禁用工具被拒 — message=工具已禁用：wiki_search (期望 '工具已禁用：wiki_search')
PASS  S5 schema-only 修复 — 实际写入 3 行（enabled 未翻转）
PASS  S6 三个已启用工具可编排 — supplier_360=True, supplier_risk=True, graph_traverse=True
PASS  S6 四个 wiki 工具仍未启用 — 全部 enabled=false
PASS  S6 只读白名单仍是 7 项 — READ_ONLY_HANDLER_REFS=['coverage_status', 'graph_traverse', 'rule_evaluate', 'supplier_360', 'supplier_risk', 'wiki_read', 'wiki_search']
PASS  S7 profile 创建后为停用 — agent=PROCUREMENT_COPILOT enabled=False revision=1
PASS  S7 绑定的是三个只读工具 — bindings=['graph_traverse', 'supplier_360', 'supplier_risk']
PASS  S7 toggle 后启用且 revision=2 — enabled=True revision=2
PASS  S8 run 创建为 QUEUED — run_id=37647bd9-3368-492d-a519-f96b088a2f6d status=QUEUED
PASS  S8 快照只含只读白名单工具 — snapshot_tools=['graph_traverse', 'supplier_360', 'supplier_risk']（无任何写工具）
PASS  S9 feature-off 时新路由缺席 — /openapi.json 中的编排路径 = []
PASS  S10 自建行已清理 — 残留行数=0
PASS  S2 既有表行数前后一致 — 六张既有表行数无变化
OK  all steps passed
```

退出码 `0`；**幂等**（第二次运行同样 OK）。

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
- **新代码覆盖率 94.44%**（门槛 80%）。
- **默认关闭、Worker 不自启、路由不注册**三条安全姿态均已实测。
- **真实数据脚本 exit 0 且幂等**，证明只读约束与「不改既有功能」。
- **3 个 MEDIUM 安全项刻意挂账** ⇒ 已进 PRE-ENABLE CHECKLIST，作为生产启用前的硬闸门。
- **生产未部署**；灰度由人按手册执行。

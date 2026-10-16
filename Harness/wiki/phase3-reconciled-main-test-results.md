---
created: 2026-10-15
updated: 2026-10-15
sources:
  - PR #19 body (`/tmp/phase3-pr-body.md`)
  - memory `qa-system-phase3-reconciled-main.md`
  - memory `qa-system-p0-3-vs-f1-f11b-recon.md`
tags: [aichat, phase3, test-results, cherry-pick, alembic-0127]
---

# Phase 3 Reconciled-Main 全栈测试结果（2026-10-15）

把 6 个 P1/P2 PR（#12-#17）cherry-pick 到 `origin/main` @ `bcf2fac0` 后的全栈 integration 实跑结果固化。

## 上下文

- **分支**：`phase3/reconciled-main` @ `817688f2`（12 commits ahead of origin/main @ `bcf2fac0`）
- **PR**：[#19](https://github.com/sunql/QA/pull/19)
- **alembic head**：0127（单一，无多 branch）
- **测试 DB**：`qa-pg-a1` @ 5434（`qa_metadata_test`）
- **测试 Neo4j**：`qa-neo4j-test` @ 7688
- **测试 Milvus**：19530

## 全栈 integration 实跑结果

| 套件 | 跑 | 过 | 红 | 红原因（已全部验证 pre-existing on bcf2fac0） |
|---|---|---|---|---|
| chat integration (25 files) | 211 | 183 | 28 | embedding API key 缺失 → `PLAN_EMPTY × 3` |
| ontology integration (13 files) | 163 | 154 | 9 | Milvus `dim(1024)` vs embedding 1536 mismatch |
| wiki integration (19 files) | 376 | 352 | 24 | `*_requires_auth` 403 = container `AUTH_MODE=real`（runbook §19）|
| research integration (7 files) | 123 | **123** | 0 | 全绿 |
| 合流新加单测 | 50 | 50 | 0 | 全绿 |
| 旧 `test_chat_service_stream.py` | — | — | 10 | pre-existing on bcf2fac0（Python 3.14 asyncio Strict）|

**合流引入 0 个失败**。所有 71 个红均已在 `/tmp/bcf2-baseline` 临时 worktree `git checkout bcf2fac0` 复跑确认 = pre-existing。

## 1 个真 merge-caused bug（已修）

`P1-4 MultiStepDependencyResolver.validate` / `topological_groups` / `apply_failure` 按 tuple 位置取 `plan.steps[idx]`，但 `step.index` 是业务 index（单步聚合 `StepPlan(index=1, ...)` 即踩中）。

`TestB5MultiStepInheritance::test_execute_multistep_writes_inheritance_snapshot` 在 bcf2fac0 PASS，在 phase3/reconciled-main FAIL。

**修法**：建 `step_by_index: dict[int, StepPlan]` 查表；越界检查从 `dep >= n` 改为 `dep not in step_by_index`。

Commit: `361cd529 fix(multistep): P1-4 dep resolver 用 step.index 查表（index != tuple 位置）`

## alembic 链（5434 test DB 实跑）

```
0124_session_query_state_scope.py   ← P0-3 (#11 b81bf1e0)
0125_kpi_catalog_datasource_fk.py    ← F2a (#18 bcf2fac0)
0126_session_token_usage_cost_source.py  ← P1-2 (#13 d4bd9dae)
0127_analysis_hypothesis_verification.py ← P1-5 (#16 ef458bbd, rename from 0116)
```

**Why** 改号：P1-2 在 phase3 cherry-pick 时由 phase3-reconciler 把 `0115` 改号为 `0126`（commit message 仍误写 0115；实际落盘是 0126；0115 号段已被 main head chain 外的 `0115_multi_step_session_text.py` 占用）。P1-5 是真 rename（`ef458bbd` 1 file 5+/4-）。**How to apply**：合流前必看 `ls backend/alembic/versions/0*.py | sort | tail -10`，确认当前 head；新 PR 报 "down_revision=0114" 而 main 已是 0124+ ⇒ 改到 head+1。

## 关键 git grep 命中数（R1 recon 2026-10-15）

P0-3 (`9503aa09f`) 加 SessionQueryState 三元绑定列，F1.1b (`940d76da`) 修 userId 类型 bug，F1 (`bcf2fac0`) 补 4 流式 scope 写点。**互补关系，非双写**。

- `chat_stream.py` 写 `datasourceId`：**6 处**（P0-3 L584/L626；F1 L268/L825/L1086/L1361）
- `chat_stream.py` 写 `userId=user.dbUserId`：**8 处**（P0-3 + F1.1b 修正 L583/L625/L929/L1239；F1 L267/L824/L1085/L1360）
- `backend/app/` 整体 `datasource_version` 写：**2 处**（仅 `chat_context.py::_saveQueryState` L627/L644，F1 未触碰）

每个分支只写一次 ⇒ 无 schema 双写、无值冲突。**PR #19 合并前无需清理**。

## 测试装配兜底

- `app/services/__init__.py` 是空文件，`unittest.mock.patch("app.services.chat_multistep.X")` 走 `pkgutil.resolve_name` 属性 walk 会失败。`test_chat_multistep_dep.py` 顶部加 `import app.services.chat_multistep  # noqa: F401`。
- `test_chat_multistep_dep.py` 是 P1-4 写的，P1-4 base tree 没有 P0-3 方法。合流后 `_executeMultiStep` 实际调用这些 → AttributeError。`_build_mixin()` 加 8 个 AsyncMock 兜底 + `session=MagicMock()` 改 `AsyncMock()`（否则 `await session.commit()` → TypeError）。

## 验证方法（复制即用）

```bash
# 1. 起 5434 测试库（如未起）
docker start qa-pg-a1

# 2. 切到 PR #19 head worktree
cd /tmp/qa-phase3-pr19  # 已建

# 3. 跑全栈（注意 conftest 缺 env 会 fail-fast）
export TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test
export TEST_NEO4J_URI=bolt://localhost:7688

# 单套件
cd backend
uv run pytest -xvs app/tests/integration/test_chat/ -k 'not test_chat_service_stream' 2>&1 | tail -50

# 跑全栈不写 alembic
uv run pytest app/tests/integration/ 2>&1 | tail -30
```

**红线**：缺 `TEST_DATABASE_URL` 或 `TEST_NEO4J_URI` 任一 → conftest fail-fast RuntimeError（mock aliyun-style 警告）。5434 是测试库 ≠ 生产 5433。

## 已知 pre-existing 失败清单（PR #19 合后仍会红，不阻塞）

| 套件 | 失败数 | 根因 | 修法归属 |
|---|---|---|---|
| chat integration | 28 | `PLAN_EMPTY` = embedding API key 缺失 | 后端 / runbook 配置 |
| ontology integration | 9 | Milvus `dim(1024)` vs 1536 embedding dim | 后端 / Milvus 重建 |
| wiki integration | 24 | `*_requires_auth` 403 = AUTH_MODE=real | runbook §19 / 容器配置 |
| `test_chat_service_stream.py` | 10 | Python 3.14 asyncio Strict（`StopIteration`） | 升级到 Python 3.14 友好写法 |

## 后续动作

- [x] PR #19 推 origin + 建 PR
- [x] 6 旧 PR (#12-#17) 标 superseded / close（已关）
- [x] 全栈测试结果固化为 wiki（本条 + commit `817688f2` SSOT）
- [x] R1 recon（§1.1 双写检查） = DONE，互补无需清理
- [x] PR #19 reviewer 内部走 whole-branch review（sonnet subagent）= ⚠️ APPROVE WITH MINOR（0 CRITICAL / 0 HIGH / 2 MEDIUM / 1 LOW）
- [x] MEDIUM 修复：§3 alembic 描述改对 + wiki 文件补到 phase3 worktree
- [ ] Reviewer human review on PR #19
- [ ] PR #19 merge 后 alembic 0125→0126→0127 在生产库 5433 跑通

## 相关条目

- [[chat-service-assessment]] — Chat Service 整体评估
- [[chat_multi_step_persistence]] — 多步 run-persist 落库机制
- [[chat-service-capabilities]] — 模型能力路由 + 流式 usage 双收
- memory `qa-system-phase3-reconciled-main` — 合流结果分支详情
- memory `qa-system-p0-3-vs-f1-f11b-recon` — R1 recon 互补判定细节
- memory `qa-system-p1-p2-pr-base-fake-diff` — 350 文件 diff 假象解释
- memory `qa-system-alembic-merge-number-collision` — alembic 撞号修法
- memory `qa-system-bcf2fac0-baseline` — #18 squash 后 main head

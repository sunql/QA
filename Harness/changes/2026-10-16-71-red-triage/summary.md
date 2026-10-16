---
created: 2026-10-16
updated: 2026-10-16
sources:
  - Harness/changes/2026-10-15-r234-recon/summary.md
  - memory/qa-system-phase3-reconciled-main.md
  - memory/qa-system-chat-suites-stale-doubles-45-red.md
  - memory/qa-system-rbac-api-stale-tests.md
  - memory/qa-system-router-auth-mandatory.md
  - memory/qa-system-vectors-endpoint-dim-bug.md
  - memory/qa-system-milvus-test-drops-prod.md
  - memory/qa-system-integration-env-contract.md
  - memory/qa-system-mixed-suite-truncate-hazard.md
  - memory/qa-system-no-ci-main-can-go-red.md
  - memory/qa-system-frontend-suite-baseline.md
tags: [triage, 71-red, A1, fix-branches]
status: draft
---

# 71 红 test backlog triage（2026-10-16）

phase3 close-out 时记录 71 红 pre-existing on `bcf2fac0`；本轮 recon 跑
`/tmp/qa-red-triage` worktree（branch `fix/71-red-triage` @ d5c8a429）确认
**实际 ~83 红**（多 12：RBAC 11 + route auth guard 1）。

## 状态总览

| 类别 | 预期 | 实测 | 根因类 | 修法 |
|---|---|---|---|---|
| chat integration | 28 | 28 | RC-1 `_buildService` PLAN_EMPTY | 代码：fixture 补默认 classes（NAME/QTY） |
| ontology integration | 9 | 9 | RC-2 Milvus dim 1536 vs 1024 | 代码：test 集合 schema 改 dim |
| wiki integration | 24 | **20 + 1 errored = 21**（实测 2026-10-16 全跑 19 文件） | RC-5 + 集中 1 文件 11 红（待细分） | env/doc + header + link_admin 集中修 |
| chat stream unit | 10 | 10 | RC-1 同 chat | 同 RC-1 |
| RBAC API | 0 (memory 未列) | **11** | RC-3 `_mkUser` 漏 password 字段 | 代码：test helper 补 password |
| route auth guard | 0 (memory 未列) | **1** | RC-4 `_IncludedRouter` 已从 FastAPI 移除 | 代码：import 改公开 API |
| 前端套件 | ~10 | ~10 (per memory) | pre-existing on `d1af4d4` | 独立 track，不在本批 |

**总计 backend ~83**（chat 28 + ontology 9 + wiki 24 + stream 10 + rbac 11 + auth guard 1）。

## 根因拆解

### RC-1: `_buildService()` fixture PLAN_EMPTY 饥饿（chat 28 + stream 10 = 38 红）

**症状**: `nl2sql_plan.py:295` PLAN_EMPTY，3 次 retry 耗尽 ⇒ Nl2SqlError。
**根因**: `_buildService()` fake double 返回的 plan 只有 `target`，无 classes/properties；
`_isEmptyPlan`（M3 a6371de commit 引入）不把 `target` 当内容 ⇒ 整 plan 被判空。
**修法**: test fixture 补默认 classes（如 `SELECT NAME, SUM(QTY) FROM ZJTH.PRECEIPT` 对应的 NAME + QTY）。
**Why 1 fixture 影响 38 红**: chat follow-up cascade / semantic routing / hypothesis
multistep / session context / query state 整片都过 `_buildService` ⇒ 全军覆没。
**How to apply**: 修 1 个 helper 消 38 红 = 46% backend backlog。

### RC-2: Milvus embedding dim 不匹配（ontology 9 红）

**症状**: `MilvusException: length(1536) of float data should divide the dim(1024)`，
出在 `ontology_service.py:1539` 同步 embedding 时。
**根因**: test collection schema `dim=1024`，但当前 embedding provider 返回 1536 维向量。
**修法**: test 集合创建处改 dim 与 embedding provider 输出对齐（或反过来缩 embedding 维度）。
**风险**: 低（test-only schema 改动），不污染 prod。

### RC-3: RBAC `_mkUser` 漏 `password` 字段（rbac 11 红）

**症状**: `AssertionError: 422 == 201`，`loc: ["body","password"]` at `test_rbac_api.py:67`。
**根因**: `app/schemas/rbac.py:32` 的 `password: str = Field(min_length=8)` 改为必填后，
`_mkUser` (line 61) 只发 username/display_name/email，没发 password。
**修法**: `test_rbac_api.py:61` `_mkUser` 加 password 字段（如 `f"{username}Pass123"`）。
**风险**: 极低（单 test helper），不碰 prod code。

### RC-4: FastAPI `_IncludedRouter` 已被移除（route auth guard 1 红，但 block 全套件 collection）

**症状**: `ImportError: cannot import name '_IncludedRouter' from 'fastapi.routing'`
在 import 时抛出 ⇒ `pytest app/tests/integration/` **整个 suite collection 失败**，
200+ 测试无法 collect 出来。
**根因**: 新版 FastAPI 移除 `_IncludedRouter` 内部符号；测试在 module 顶层 import 它。
**修法**: `app/tests/integration/test_route_auth_guard.py:6` 改用公开 API 或去掉该 import。
**关键**: 修这一个 import error 之后，所有 200+ integration tests 才能被 collect + run，
否则现状下根本无法"完整跑套件"。

### RC-5: wiki 环境 + auth header + link_admin 集中 11 红（实测 20+1）

**2026-10-16 用真 PG (5434) + 真 Neo4j (7688) + TEST_DATABASE_URL 全跑 19 个 wiki 文件**。

#### 真实分布

| 文件 | passed | failed | 备注 |
|---|---|---|---|
| test_wiki_api.py | 37 | — | 1 deadlock（test setup 期间）= errored |
| test_wiki_structure_api.py | 58 | 0 | 全绿 |
| test_wiki_relation_api.py | 20 | 0 | 全绿 |
| test_wiki_conflict_api.py | 17 | 0 | 全绿 |
| test_wiki_coverage_api.py | 20 | 0 | 全绿 |
| test_wiki_dedup_api.py | 14 | 0 | 全绿 |
| test_wiki_dedup_p1_writers.py | 5 | 0 | 全绿 |
| test_wiki_agent_tools_api.py | 21 | 0 | 全绿 |
| test_wiki_claims_force_extract.py | 6 | 0 | 全绿 |
| test_wiki_gap_actions_api.py | 11 | 0 | 全绿 |
| test_wiki_graph_api.py | 10 | 0 | 全绿 |
| test_wiki_graph_insights_api.py | 8 | 0 | 全绿 |
| test_wiki_batch_delete_api.py | 7 | 0 | 全绿（5min timeout 一次过） |
| **test_wiki_import_api.py** | 40 | **3** | |
| test_wiki_import_catalog.py | 7 | 0 | 全绿 |
| **test_wiki_import_retry_api.py** | 8 | **2** | |
| **test_wiki_link_admin_api.py** | 1 | **11** | **集中大头（先看是不是同 root cause）** |
| test_wiki_link_injector_e2e.py | 10 | 0 | 全绿 |
| **test_wiki_reclassify_api.py** | 19 | **2** | |
| **test_wiki_suggestion_api.py** | 33 | **2** | |
| **TOTAL** | **352** | **20** | +1 errored = 21 实际 |

#### 已知 RC-5 子类

- `test_wiki_dedup_api.py:19` docstring 写 `localhost:5433`，正确是 5434（test 库）
- 部分 wiki 测试可能撞 RC-1 PLAN_EMPTY
- 部分可能需要 `X-User-Id` header（`qa-system-router-auth-mandatory`）

**修法策略**（**先看 link_admin 11 红是不是同 root cause**）：

- 若是同根因（1 个 fix 灭 11 红）→ 1 个 branch `fix/wiki-link-admin-core` + 另开 `fix/wiki-import-and-misc`
- 若非同根因 → 拆 `fix/wiki-link-admin-1` / `fix/wiki-link-admin-2` / ...

**Why 优先 link_admin**: 11/20 占 55% wiki 红；集中单文件 = 风险隔离；1 个 fix 高概率灭 11 红。
**How to apply**: 进 `fix/wiki-env-and-auth` branch 第一步：跑 `test_wiki_link_admin_api.py` 11 个红看 trace，
按 trace 同/异根因分组。

## 分支分组（5 个独立 branch + 1 独立 track）

| branch | 红数 | 根因 | 风险 | 估时 |
|---|---|---|---|---|
| `fix/rbac-mkuser-password` | 11 | RC-3 | 极低 | 1-2 h |
| `fix/route-auth-guard-fastapi-compat` | 1 (+unblock 200+) | RC-4 | 低 | 30 min |
| `fix/ontology-milvus-dim-mismatch` | 9 | RC-2 | 低 | 2-3 h |
| `fix/chat-stream-plan-empty-fixture` | 38 | RC-1 | 中 | 1-2 天 |
| `fix/wiki-env-and-auth` | 20+1 | RC-5（11 集中 link_admin） | 中 | 1-2 天（先看 link_admin 同/异根因） |
| 前端套件 ~10 红 | — | pre-existing on `d1af4d4` | — | 独立 track |

**Why 5 branch 而非 1**: 各自根因独立、风险差异大、可以并行 + 独立 review。
**How to apply**: 5 branch 都基于 `main` @ `d5c8a429` 切（不是 `fix/71-red-triage`
汇总 branch），每 branch 单独 PR。

## 推荐顺序

agent 建议从 rbac（最低风险）起。我**调整优先级**为先开 `fix/route-auth-guard-fastapi-compat`：

1. **route-auth-guard** — 修 1 个 import error 解锁 200+ integration tests collection。
   后续 4 个 branch 都要跑 integration 套件，没这个修 = 验证靠手数。
2. **rbac-mkuser-password** — 极低风险，1-2 h，11 红净收。
3. **ontology-milvus-dim** — 隔离改动，2-3 h，9 红净收。
4. **chat-stream-plan-empty** — 38 红最大块，1-2 天，要理解 plan validation。
5. **wiki-env-and-auth** — 跑全 suite 后再细分，1-2 天。

**Why 改 agent 顺序**: agent 按"风险低"排，我把"解锁价值"放第一 —
route-auth-guard 修完才有 baseline 跑全部 integration。

## Follow-up: 修 route-auth-guard 浮出的新红

PR #20 修完 `_IncludedRouter` import error（4 tests 3 绿 1 新红），
**ImportError 修完**才暴露 1 个**路由配置 vs 白名单不一致**的真问题：

> `test_whitelistRoutesAreActuallyPublic` 失败：
> `('GET', '/api/v1/data-quality/reports/share/{token}')` 现在带了鉴权

**含义**: 路由在 `PUBLIC_ROUTES` 白名单（应公开），但实际挂了
`Depends(getCurrentUser)`（带鉴权）。两者矛盾。

**两个修法方向**（需用户裁定，不属 route-auth-guard branch scope）：

| 方向 | 含义 | 改哪里 |
|---|---|---|
| A：路由应公开 | share link 本来就匿名可访问，鉴权是误加 | 移除 `data-quality/reports/share/{token}` 上的 `Depends(getCurrentUser)` |
| B：白名单应剔除 | 实际需要鉴权（防 share token 滥用），白名单过时 | 从 `PUBLIC_ROUTES` 删除该 tuple |

**SSOT 关联**: 建议在新的 `fix/data-quality-share-auth-policy` branch
（基于 `main` @ 新 HEAD，即 d5c8a429 → d5c8a429 + PR #20 merge 后）开搞。

**Why 独立 branch**: 这是一个**安全/产品策略决策**，不是 test 修；
单 branch = 单决策 = 1 PR 走 review，跟 71 红其他 fix 性质不同。

## 关注点 / 意外

1. **test_route_auth_guard.py ImportError block 整套 collection** — 必须最先修，
   否则 200+ tests 数都数不出来。
2. **71 → 83 红漂移** — phase3 close-out 时 memory 漏算 rbac 11 + auth guard 1
   ⇒ 实际 backlog 比 close-out 报告大 17%。**修订后 83 红**，不是 71。
3. **PLAN_EMPTY 一因吃 38 红**（46% backend backlog）— 修 1 个 fixture helper
   清近一半 backlog，价值密度最高。
4. **wiki 24 红已实测 = 20 failed + 1 errored = 21 实际**（2026-10-16 全跑 19 文件）。
   link_admin 11 红集中，待开 branch 第一步看同/异根因。
5. **Milvus dim 漂移** — embedding provider 可能改过输出维度但 test schema 没跟上，
   **检查 prod** `qa_metadata.ontology` 表对应 collection 当前 dim 是否也是 1024
   （如果是 = prod 写不进新数据；如果不是 = test 与 prod 不一致，看哪种是 ground truth）。
6. **前端 ~10 红** pre-existing on `d1af4d4`，不在 backend 83 内，独立 track。
7. **没 prod code 改动**: 83 红里**全部是 test 侧**（fixture / schema / import / helper），
   不需要触碰业务代码；这是好消息。
8. **PR #20 浮出 F1 新红**: `/api/v1/data-quality/reports/share/{token}` 路由
   vs 白名单不一致（见 §Follow-up），需用户裁定方向后再开 `fix/data-quality-share-auth-policy`。

## 关联

- [[qa-system-phase3-reconciled-main]] — phase3 close-out (71 红 来自这里)
- [[qa-system-chat-suites-stale-doubles-45-red]] — chat 套件 45 红（部分已合）
- [[qa-system-rbac-api-stale-tests]] — rbac_api 11 红
- [[qa-system-router-auth-mandatory]] — read-only 必须 `Depends(getCurrentUser)`
- [[qa-system-vectors-endpoint-dim-bug]] — Milvus dim bug
- [[qa-system-integration-env-contract]] — env 契约
- [[qa-system-no-ci-main-can-go-red]] — 71 红 pre-existing 警示
- [[qa-system-frontend-suite-baseline]] — 前端 baseline
- [[qa-system-future-dev-separate-branch]] — 5 branch 都独立开 + PR

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
| ontology integration | 9 | 0 (PR #27) | RC-2 Milvus dim 1536 vs 1024 + RC-7 mock 老名 + RC-8 property 描述重刷断言 | test fixture 改 dim + 补 4 个新 mock |
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

### RC-2: Milvus embedding dim 不匹配（ontology 9 红 → PR #27）

**症状**: `MilvusException: length(1536) of float data should divide the dim(1024)`，
出在 `ontology_service.py:1539` 同步 embedding 时。
**根因**: test collection schema `dim=1024`，但当前 embedding provider 返回 1536 维向量。
**修法**: test 集合创建处改 dim 与 embedding provider 输出对齐（或反过来缩 embedding 维度）。
**风险**: 低（test-only schema 改动），不污染 prod。

**2026-10-16 PR #27 实际根因是 3 个**（不全 = dim）：
- **RC-α (dim)**: 上文。3 处 test stub `* 1536` 改 `* 1024`。
- **RC-γ (mock 老名)**: M0-P0.4 refactor 之后 ontology_service 走
  `insertEmbeddingsDual` / `deleteByOntologyIdDual` /
  `searchEmbeddingsByTypeRouted` / `listEmbeddingsAcross3Collections`。
  原 test fixture 只 mock 老名 ⇒ prod 走真 milvus_client 路径或撞
  AttributeError（fixture 默认 lambda 静默吞），5 个同步/搜索测试假绿失败。
- **RC-δ (property 描述重刷断言)**: `createProperty` 也调
  `_syncPropertyEmbeddingBestEffort` (line 729)，create+update 流下 ≥2 条
  property 记录；create 的后台 task 读到 update 已 commit 的 state，两条
  都带新 description。原断言 `len == 1` 不成立。

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
| **test_wiki_link_admin_api.py** | 1 | **11** | **集中大头 — 100% 同根因 MUST_CHANGE_PASSWORD**（2026-10-16 recon 确认）|
| test_wiki_link_injector_e2e.py | 10 | 0 | 全绿 |
| **test_wiki_reclassify_api.py** | 19 | **2** | |
| **test_wiki_suggestion_api.py** | 33 | **2** | |
| **TOTAL** | **352** | **20** | +1 errored = 21 实际 |

#### 已知 RC-5 子类

- `test_wiki_dedup_api.py:19` docstring 写 `localhost:5433`，正确是 5434（test 库）
- 部分 wiki 测试可能撞 RC-1 PLAN_EMPTY
- 部分可能需要 `X-User-Id` header（`qa-system-router-auth-mandatory`）

**修法策略**（**先看 link_admin 11 红是不是同 root cause**）：

- **2026-10-16 recon 确认**：11 红 100% 同根因 — 都是 403 `MUST_CHANGE_PASSWORD`（admin 用户
  `must_change_password=True` 没清；与 rbac test fixture 缺同一段 fix 同款）
- 1 个 fix 灭 11 红 → `fix/wiki-link-admin-must-change-password` branch（test-only 1 行）
- 另开 `fix/wiki-import-and-misc` 处理剩下 9 红（4 文件各 2-3 红）

**2026-10-16 PR #25** (`fix/test-fixture-item-profile-seed`)：合并 3 个 fixture fix，
- wiki_link_admin 11 红（MUST_CHANGE_PASSWORD）= 1b1f24a4
- identity_create_user_password 2 红（hidden M2）= 1b1f24a4
- user_clearance M1 latent fix（防 MenuConfigStructureError）= 1b1f24a4
- 测试 18/18 passed（4 min total）
- **真实 backlog 85 → 72 (-13)**

**2026-10-16 PR #26** (`fix/wiki-import-and-misc`)：合并 4 文件 fixture fix，
- test_wiki_import_api 3 红（RC-A password + M2 item.profile + M3 must_change_password）= 2fbab748
- test_wiki_import_retry 2 红（RC-A + M2 + M3）= 2fbab748
- test_wiki_reclassify 2 红（RC-A + M2 + M3）= 2fbab748
- test_wiki_suggestion 2 红（RC-B AUTH_STUB_ENABLED cache_clear）= 2fbab748
- 测试 123 passed (7m 30s) + 全 wiki suite 166 passed (0 回归)
- **真实 backlog 72 → 63 (-9)**
- **3 个 root cause**:
  - **RC-A**: UserCreate schema password 必填（feat-admin-user-password 引入），
    5 个测试 helper 漏 password
  - **RC-B**: getSettings 是 @lru_cache，monkeypatch.setenv 改 env 后必须
    cache_clear 才会重读；本仓已有同款模式 (test_rate_limit_chat_route.py:44)
  - **M2+M3**: 与 PR #25 同款 (item.profile FK seed + must_change_password clear)

**2026-10-16 PR #27** (`fix/ontology-milvus-dim-mismatch`)：合并 2 文件 mock 修，
- test_ontology_api 7 红（RC-γ mock 老名 + RC-α dim 1536→1024）= ac90bff7
- test_ontology_property_service 2 红（RC-γ `_FakeMilvus` 缺 `insertEmbeddingsDual`
  + RC-δ create+update 双同步记录断言 = 1 fail）= ac90bff7
- 测试 13 个 ontology 文件 163 passed (2:47)
- **真实 backlog 63 → 54 (-9)**
- **3 个 root cause**:
  - **RC-α (dim 1536→1024)**: 3 处 test stub embedding 改 dim 与 prod bge-m3 对齐
    (milvus_client.py:38 `_DIM=1024`)
  - **RC-γ (mock 老名)**: M0-P0.4 3-collection refactor 之后 ontology_service
    统一走 `*Dual` / `*Routed` 入口（`insertEmbeddingsDual` /
    `deleteByOntologyIdDual` / `listEmbeddingsAcross3Collections` /
    `searchEmbeddingsByTypeRouted`）；原 test fixture 只 mock 老名
    （`insertEmbeddings` / `searchByEmbedding` / `listAllEmbeddings`）⇒ prod
    不再调老名 ⇒ mock 形同虚设 ⇒ 5 个同步/搜索测试假绿失败。修法：autouse
    fixture 补 4 个新 mock + 3 个 search 测试改 patch 新名 + 1 同步测试
    改 patch `listEmbeddingsAcross3Collections` + `_FakeMilvus` 加
    `insertEmbeddingsDual` 同形方法
  - **RC-δ (property 描述重刷断言)**: `createProperty` 也调
    `_syncPropertyEmbeddingBestEffort` (line 729) ⇒ create+update 流下
    ≥2 条 property 记录；create 的后台 task 读到 update 已 commit 的 entity
    state，两条都带新 description。原断言 `len == 1` 不成立；改
    `≥1 + 全检 ontology_id + 全检带新 description`

**Why RC-γ 隐藏深**：prod ontology_service 走新名，旧 wrapper 函数（`insertEmbeddings`
等）仍存在只是不再被调用。fixture 默认 `lambda *a, **kw: None` 静默吞 AttributeError，
旧 mock 替换老名后调老名 = 还是 none —— **看起来过了，实际 prod 走真 milvus_client
代码路径**，8/9 红全是假绿。

#### 后续发现的 hidden reds（不在原 83 红统计内）

- **`test_identity_create_user_password.py` 2 红**（2026-10-16 rbac review M2 发现）：
  `test_create_user_hashes_password` + `test_create_user_grants_profile_menu` 都撞
  `IntegrityError fk_permission_grant_menu` — 同款 item.profile 缺 seed 问题，但走
  IdentityService 直接调用（不经 HTTP）。修法：与 wiki_link_admin 一并开
  `fix/test-fixture-item-profile-seed` PR（同 root cause class），11 + 2 = 13 红净收。
- **`test_user_clearance.py` 潜在隐患**（M1）：line 53 把 item.profile 顶层插入
  （path="/profile", parent_id=None），与 rbac 修后的 child leaf 模式不兼容 — 该文件
  当前不调 /menu-config 所以不报错，但合并后若有测试同时跑两边会偶发结构错误。
  修法：M1 建议同 `fix/test-fixture-item-profile-seed` 统一改 child leaf。

**Why link_admin 改 1 行**: rbac test 已有同款 fix（line 106-111 段，admin.must_change_password = False），
wiki_link_admin fixture 漏 copy 这一段 ⇒ 1 行 import + 4 行 select/update/commit = 灭 11 红。

## 分支分组（5 个独立 branch + 1 独立 track）

| branch | 红数 | 根因 | 风险 | 估时 |
|---|---|---|---|---|
| `fix/rbac-mkuser-password` | **0 (PR #24)** | RC-3 + item.profile 配套 | 极低 | **1h done** |
| `fix/route-auth-guard-fastapi-compat` | 1 (+unblock 200+) | RC-4 | 低 | 30 min |
| `fix/ontology-milvus-dim-mismatch` | **0 (PR #27 ac90bff7)** | RC-2 + RC-7 + RC-8 | 低 | 2 h done |
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
2. **rbac-mkuser-password** — **PR #24 已开 (a6be76f7)** — 极低风险，1h 净收 11 红。
   wiki_link_admin 11 红同根因（must_change_password）待开 `fix/wiki-link-admin-must-change-password`。
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
   link_admin 11 红 100% MUST_CHANGE_PASSWORD 同根因（已 recon 确认），与 rbac 配套 fix 同步。
5. **隐藏红 2 个**（2026-10-16 rbac review M2 发现）：test_identity_create_user_password.py
   2 红（`test_create_user_hashes_password` + `test_create_user_grants_profile_menu`）—
   同款 item.profile FK 缺 seed，但走 IdentityService 直接调用（不经 HTTP）。**真红数 83 → 85**。
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

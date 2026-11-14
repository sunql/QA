# KPI P4b 编译器 + P5 治理收口

- 分支：`feat/kpi-p4b-p5-compiler`，base = main `30c4097f`（PR #124 合并点）
- 状态：代码完成、待评审/合并；未部署生产（生产仍 alembic 0140 + 旧代码）
- 关联：设计 `docs/superpowers/specs/2026-11-14-kpi-p4b-p5-design.md`、
  实施计划 `docs/superpowers/plans/2026-11-14-kpi-p4b-p5.md`；
  运维步骤见 `Harness/wiki/operations-runbook.md`「KPI 治理」

## 背景

P2 Tier A 原先只执行 `ESCAPE_HATCH` 指标的 `metric.formula`；`COMPILED` 指标即使
已有结构化 numerator / denominator / filter / grain，也一律降级 Tier B
（`compiled_not_supported`），结构化列没有被利用。P4b 增加 measure→SQL 编译器：
把 COMPILED 指标编译成**单值只读 SELECT**，与 ESCAPE_HATCH 同走 Tier A；编译失败
fail-closed 回落 Tier B，不编数、不静默。

P5 补治理收口：KPI / 指标状态流转矩阵（非法流转 409 且零副作用）、发布闸门开启前
的人工检查口径、admin 治理看板（PG 计数 + 近 7 天对账 + 三库同步健康），以及
向量/图同步补偿的运维入口。**零 DDL（无新迁移）**。

## 变更

以下为 `git log --oneline 30c4097f..HEAD` 的实际提交（本变更记录自身由 Task 7 的
docs commit 追加）。

### Backend

| Commit | 内容 |
|---|---|
| `f59942a4` | 新增 `kpi_sql_compiler.py`：measure→SQL 编译器（fail-closed、单值只读 SELECT）+ 284 行单测 |
| `350e6fc8` | 编译器修复：schema 限定表名、跨表值列限定、WHERE 谓词括号化 |
| `959d3295` | COMPILED 指标路由到 Tier A；对账不合格仍降级 B；`kpi_diagnose._tierOf` 与运行时同口径 |
| `472e585e` | chat Tier A 分支接入编译器；编译失败 fail-closed 降级 Tier B（跳过 L4） |
| `8d6371b5` | 新增 `kpi_status_machine.py`：KPI/指标状态矩阵，非法流转 409 `INVALID_STATUS_TRANSITION`（零 DDL） |
| `8ab3993a` | 状态矩阵适配既有测试契约、收紧流转调用（去掉 `.value` 耦合） |
| `850dd4c9` | 钉住指标流转计数的 `metric_id` 谓词并订正注释 |
| `f75283fe` | 治理看板聚合 API：`GET /api/v1/admin/kpi/governance-summary`（admin-only，best-effort 计数） |
| `24f4a52c` | 治理看板契约测试：7 天窗口 / 实体过滤、`NONE` 桶、同步健康降级 |

### Frontend

| Commit | 内容 |
|---|---|
| `94765c36` | 新增治理看板页 `KpiGovernancePage.tsx` + `kpiGovernance` API/types + 路由 `/kpi-governance` + 菜单入口与 i18n（含 `seed_menu_config.py`） |
| `26587a14` | 看板补 `syncHealthError` i18n 键，并加请求竞态 / loading 守卫与测试 |

### Docs

| Commit | 内容 |
|---|---|
| `d81a526a` | P4b 编译器 + P5 治理收口设计与实施计划（904 行计划 + 198 行设计） |
| `13976cea` | 实施计划按 recon 校正路由分歧、精确路径与错误形状 |

## 验收

环境：本 worktree 无 `.venv`，先 `cd backend && uv sync --frozen --extra dev`；
定向测试使用专用测试库与 Neo4j（**只跑定向，未跑全量**）：

```bash
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
```

后端 unit（编译器 / 执行计划 / 状态矩阵 / Tier parity）：

```
$ .venv/bin/python -m pytest app/tests/unit/test_kpi_sql_compiler.py \
    app/tests/unit/test_kpi_execution_plan.py app/tests/unit/test_kpi_status_machine.py \
    app/tests/unit/test_kpi_tier_parity.py -q
......................................................................   [100%]
70 passed in 36.93s
```

后端 integration（诊断 / Tier A chat）：

```
$ .venv/bin/python -m pytest app/tests/integration/test_kpi_diagnose_api.py \
    app/tests/integration/test_kpi_tier_a_chat_api.py -q
................................                                         [100%]
32 passed, 21 warnings in 27.23s
```

前端治理看板页：

```
$ cd frontend && npx vitest run src/tests/KpiGovernancePage.test.tsx
 ✓ src/tests/KpiGovernancePage.test.tsx (6 tests) 217ms

 Test Files  1 passed (1)
      Tests  6 passed (6)
```

文档校对：

```
$ grep -rn "compiled_not_supported" Harness/wiki/ || echo NONE
NONE
```

三条命令均为 0 失败，因此无需与 base `30c4097f` 做失败集对照。

## 未做与后续

- **三项已知能力边界（本批不修）**：
  1. 显式时间问句 + COMPILED 走不到编译器：路由门要求 `time_param_ready`
     （formula 含 `{time_scope}`），COMPILED formula 为空 ⇒ 恒降级 Tier B
     `explicit_time_scope`；编译器本身支持 timeScope（Oracle 方言）。
  2. `COUNT(*)` 形态的 COMPILED 指标进不了 Tier A：对账层 2 返回
     `technical_measure_unjudgeable` / `UNKNOWN` ⇒ `isTierAEligible=False`；
     要进 Tier A 须写成 `COUNT(<列>)`（治理/产品决策项）。
  3. `reconcile.passCount` **不代表通过率**：P1 只为 findings 非空的对账落审计 ⇒
     干净 pass 不落库；但层 1/3 mismatch + 层 2 pass 的 degraded pass 会以
     `verdict="pass", degraded=true` 落库。`passCount` 既漏干净 pass 又混入 degraded
     pass，严重低估真实 pass；闸门前须查 `after_json->>'degraded'='true'` 行数为 0，
     并逐 KPI diagnose 确认 `degraded=false`（不能只看 `mismatchCount`/`unknownCount`）。
- **延后 minor**：`KpiGovernancePage.tsx` 组件体积偏大，未拆分/未抽卡片公共组件；
  中英文 i18n 键一致性仍靠人工，未加 parity 测试。
- **零 DDL**：本批无迁移；不部署生产。
- **生产发版硬闸门（顺序不可换、不可自动执行）**：
  1. `alembic upgrade head`（0140 → 0143）；
  2. `scripts/kpi_backfill_time_grain.py --apply --allow-prod-write`，预期
     `changes=11 noop=1`，幂等复跑 `changes=0 noop=12`；
  3. 通过后再部署代码（backend 先、frontend 后）。
- **发布闸门保持默认关**：开启前按 runbook「发布闸门开启前检查」核对
  `mismatchCount` / `unknownCount` 与 datasource 绑定；回滚只需删除
  `system_config` 对应行。
- **运维 / 回滚入口**：六个开关、发布/废弃/恢复、`INVALID_STATUS_TRANSITION`
  处理、同步补偿、`kpi_catalog_history` / `audit_log` 审计查询全部见
  `Harness/wiki/operations-runbook.md`「KPI 治理」。代码回滚用 `git revert`
  本批 commit（零 DDL，无迁移回滚）。

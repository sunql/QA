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
| `f59942a4` | 新增 `kpi_sql_compiler.py`：measure→SQL 编译器（fail-closed、单值只读 SELECT）+ 编译器单测（终审修复波后 581 行） |
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
| `feb15626` | KPI 治理 runbook + P4b/P5 变更记录 |
| `7f95a4c7` | 订正对账 pass/degraded 口径（runbook/变更记录/注释） |
| `51a8facd` | 看板 pass 提示与对账口径一致化 |
| `893f9ca5` | 统一 passCount 口径（类型注释/runbook/en-US 措辞） |

### 终审修复波（whole-branch review）

| Commit | 内容 |
|---|---|
| `331e0e21` | 编译器角色感知 measure→class 解析；FILTER 源 / LEFT 反向遍历 / 多表复合表达式 fail-closed；编译 warning 落日志 |
| `9d1df4d9` | 诊断 Tier 建模 `formula_empty` 静态门；COMPILED 空 formula 不看 formula；parity 参数化 + wiki 分歧如实登记 |
| `e3ee358f` | 指标 PUT 显式 `status: null` ⇒ 422（`MSG_KPI_METRIC_STATUS_NULL`），不再落 NOT NULL 500 |

本变更记录自身的 docs commit 紧随其后（不列自身 SHA）。

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
     （formula 含 `{time_scope}` **且 `metric.time_column` 非空**），COMPILED
     formula 为空 ⇒ 恒降级 Tier B `explicit_time_scope`；编译器本身支持 timeScope
     （Oracle 方言）。
  2. `COUNT(*)` 形态的 COMPILED 指标进不了 Tier A：对账层 2 返回
     `technical_measure_unjudgeable` / `UNKNOWN` ⇒ `isTierAEligible=False`；
     要进 Tier A 须写成 `COUNT(<列>)`（治理/产品决策项）。
  3. `reconcile.passCount` **不代表通过率**：P1 只为 findings 非空的对账落审计 ⇒
     干净 pass 不落库；但层 1/3 mismatch + 层 2 pass 的 degraded pass 会以
     `verdict="pass", degraded=true` 落库。`passCount` 既漏干净 pass 又混入 degraded
     pass，严重低估真实 pass；闸门前须查 `after_json->>'degraded'='true'` 行数为 0，
     并逐 KPI diagnose 确认 `degraded=false`（不能只看 `mismatchCount`/`unknownCount`）。
- **终审修复波新增的 fail-closed 能力边界**（编译器拒绝而非猜）：
  1. `FILTER` 角色源类若与分子/分母类不同 ⇒ `compiled_filter_source_unsupported`
     （1:N join 无法判定基数，join 会放大聚合）；
  2. `LEFT` 边被从 target 往 source 反向遍历 ⇒ `compiled_join_direction_unsupported`
     （翻转会静默保留错误的一侧；`INNER` 方向无关，不受影响）；
  3. 多表查询中的复合值表达式（如 `A * B`）⇒ `compiled_expr_cross_table_unsupported`
     （裸列可能被 DB 解析到错误的表；单表仍允许）。
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

## 追加：M4 运行时指标（2026-11-14）

设计 §13 M4 要求「看板可看到命中率、Tier 分布、失败原因」。此前 Tier 只在响应/SSE
里出现，**不落库**：Tier A/C 回答存 `routing_layer="L1"`、Tier B 存 L2，`L1B` 是
进程内哨兵（刻意不持久化），`execution_tier`/`degraded_reason` 无列可存 ⇒ 看板
无法回答「昨天有多少问句降级、为什么」。本追加补上这条观测链路。

### 实现（零 DDL）

- **事件流**：复用 append-only `audit_log`（与 `kpi_metric_reconcile` 同一通道），
  `entity_type="kpi_runtime_event"`、`action="CREATE"`、`entity_id=kpi_catalog.id`、
  `after_json={kpi_code, tier, reason, match_method}`。无新表、无迁移。
- **写入点**：`chat_stream_output._runL1AndL4Preamble`（普通与流式入口共用）在每个
  决策分支记**实际服务**的 Tier：A 成功记 A；**A 执行失败降级记 B + 降级原因**；
  B/C 各记自身；LEGACY 仅在旧路径**实际构答成功**时记（`l1 is None` ⇒ 交给 L4，
  不记，否则虚增 Tier 分布）。
- **写入方式（独立会话自提交）**：`recordKpiRuntimeEvent` 用
  `getSessionFactory()` 开独立会话并立即 `commit`，best-effort（失败只 warning）。
  原因：流式终结分支（Tier A/C/LEGACY）只 yield SSE 帧就 `return`，从不 commit
  请求会话（`getDb` 收尾只关闭/回滚），请求会话里的审计行会被静默丢弃 —— 实测
  Tier A 流式问句 0 行、非流式 1 行，看板会把 Tier 分布系统性偏向 Tier B。与 H4
  断连兜底同理由：观测数据不该依附业务事务的提交时机，多一条连接换「必达」。
- **看板**：`GET /api/v1/admin/kpi/governance-summary` 增加 `runtime` 段：
  `byTier`（近 7 天，空值归 `UNKNOWN`）、`degradedReasons`（Top 5，次数倒序/同频按名）、
  `chatAnswers`（`channel="chat"` 的 assistant 消息数）、`hitRate = total / chatAnswers`
  （**近似**事件/回答比；分子不含澄清问句与「KPI 命中但旧路径构答失败」的问句，
  故系统性偏小；精确匹配命中率仍以 50 题语料评测为准，分母 0 时为 null）。
  查询失败全 0 + `runtimeError`，不 500。
- **前端**：看板新增「运行时路由」卡片（事件数 / 近似命中率 / 回答数 / Tier 标签 /
  降级原因标签 / 查询失败提示），i18n 双语。
- **运行代价**：每个 KPI 问句一行审计（与 KPI 流量同阶），看板聚合在库里
  `GROUP BY`（不把 7 天事件拉进内存）。`audit_log` 是 append-only，本批不引入清理；
  若 KPI 流量上来需按保留策略归档/分区（与安全审计同表，排期时一并评估）。

### 测试

- 单测（并入 `test_kpi_metric_audit.py`）：payload 形状、审计行形状、NULL reason、
  **独立连接可读到事件**（证明自提交，不依赖调用方 commit）、服务层失败与真实
  DB 失败（不可序列化负载）均被吞且零残留。
- 聚合（并入 `test_kpi_diagnose_api.py`）：Tier 计数、窗口 8 天诱饵、他类 entity
  诱饵、6 个原因的真实 Top-5 截断、回答数三向过滤、分母 0 ⇒ hitRate=null、降级路径。
- 端到端（并入 `test_kpi_tier_a_chat_api.py`）：A/C/LEGACY 与「A 失败⇒B」四条路径
  各落一条正确 tier/reason 的事件；**流式 Tier A 事件落库**（证明终结分支不 commit
  请求会话也不丢）；**旧路径构答失败（l1=None）不记事件**。
- 前端：`KpiGovernancePage.test.tsx` 增运行时卡片三例（含 hitRate=null 占位符与
  无事件/无降级原因空态）。

## CI 验收（2026-11-14）

- **判据**：失败集合对差净新增 0（main 基线本身为红，不等 CI 变绿）。
- **PR round 2**（run 43556123906，head `23d47c08`）对 **main push 基线**（run 43430969673，
  `e85f1ff8`，9 个 job 全部可用）逐分片对差：unit 0/1/2/3 = 0 净新增
  （10/6/1/3 条与基线完全相同）；integration 0/1/2/3 = 0 净新增（26/21/32/23 条与基线完全相同）；
  frontend 通过。
- **round 1 发现并修复的 1 条净新增**：Task 4 把 `details` 通道提升到 `DomainError` 基类后，
  `test_validation_error_details::test_other_domain_errors_have_no_details_attr_semantics`
  仍断言 `ConflictError` **没有** `details` 属性 ⇒ 语义过期。`23d47c08` 改为断言
  `ConflictError.details is None`（默认 None、行为不变），定向单测 17 passed。
- **基线不可用修正**：最近一次 main push run 43457350459（`30c4097f`）的
  integration shard 3 撞 90 分钟超时被 `cancelled` ⇒ 闸门判 `UNUSABLE_JOBS`
  （该分片失败集合为空，27 条「净新增」全为假阳性）。按既定恢复流程整轮复跑该基线 run
  （attempt 2），复跑收敛后推送本提交产生新的 PR CI 事件，闸门重新判定。

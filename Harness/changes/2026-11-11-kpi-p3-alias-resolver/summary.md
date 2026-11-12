# P3 别名解析层 + 对账改造 + 时间粒度 — 变更总结

- 分支：`feat/kpi-p3-alias-resolver`（worktree `/private/tmp/qa-kpi-p3`），base = main `ec1c37a1`（PR #121 P2 合入后）
- 计划：`plan.md`（裁决 R1-R8 + DoD 10 条）；recon：`.superpowers/sdd/p3-recon.md`（2026-11-11 实测）
- 状态：**代码完成、评审通过、未合并、未部署、生产库未动**

## 背景

P2（Tier A 执行器）之后，层 2 对账（治理侧 ↔ 技术形态侧）仍直接比较中文字符串，且 4 条
NONE KPI 的 unknown verdict 不带 degraded 标注。P3 交付：

1. **TimeGrain 枚举**（`day/week/month/quarter/year`，独立于 `time_scope_resolver.granularity`）。
2. **迁移 0143**：`kpi_catalog.time_grain VARCHAR(20) NULL`、`ontology_metric.time_grain VARCHAR(20) NULL`、
   `ontology_metric.time_column VARCHAR(100) NULL`（全 nullable，downgrade 对称）。
3. **`kpi_alignment_resolver.py` 纯函数**：结构化治理 token → 物理列（唯一命中→列名；多候选→unknown；
   无候选→missing；纯时间词→`TimeGrain` 并从列比较剔除）；别名分支按现状数据为未来准备。
4. **层 2 对账改造**：`resolution` 可用时粒度比较 = time_grain 枚举相等 + 维度列集合相等；
   `resolution=None` 时与 P2 行为逐字相同（恒等回退）。
5. **degraded 扩展**：`verdict==unknown ⇒ degraded=True`（放行注入不变，mismatch 仍阻断）。
6. **DTO/API**：KPI 目录与本体指标两个 admin 写入口读写 `timeGrain`/`timeColumn`；
   `time_grain` 进 `_GOVERNANCE_KEYS` 发布闸门。
7. **回填脚本**（dry-run 默认 + `--allow-prod-write` 四道闸 + 只填 null）。
8. **前端**：KPI 表单 + 指标技术形态表单新增两个字段，i18n 中英齐全。

## 裁决（R1-R8，与设计稿的显式偏差/补充）

| # | 裁决 |
|---|---|
| R1 | resolver 输入域 = 结构化 token（`numerator_columns`/`denominator_columns`/`grain_keys`）；自由文本不进 resolver |
| R2 | 源类 = `ontology_metric_source` class_id 并集；`target_class_id` 不用；无 source 行 ⇒ unknown（非 mismatch） |
| R3 | `time_column` 唯一化：grain_keys 日期 token 唯一 → 用；否则已建模源列唯一 → 用；多候选/零候选**不写**，报告候选由治理侧人工指定（「不猜」优先于自动化率） |
| R4 | unknown ⇒ `degraded=True`（无条件，不受恒等回退豁免——controller 确认）；pass/mismatch 不受影响 |
| R5 | 别名数据补录不在本批（`business_aliases` 全 null）；别名分支做到位+测试覆盖，不设命中率验收 |
| R6 | `time_grain` 回填来源 = `kpi_catalog.grain` 文本确定性推导（含「月」→month；无时间词→null） |
| R7 | 对账函数新增**可选**参数 `resolution`；`None` = 现状恒等行为 |
| R8 | 纯时间词集合 `月/月份/季度/年/周/日/天`（含 `按X` 形态）映射 TimeGrain 并从列比较剔除 |

**执行中追加的 controller 裁决**：

- 跨阶段命中（property_name 一行 / property_alias 另一行）走阶段优先（①→④ 有序列表）。
- ambiguous token 不经集合差集升级为 mismatch（`_hasAmbiguousTokens` 门控三处比较）。
- D5 修复轮：`time_column` 回填候选收紧到 `ontology_property.source_column` 已建模列
  （formula 引用的日期命名表名/CTE 不再可写入；未建模日期列降级只报告）。
- 回填前部署窗口（生产 0140 尚未上 0143）：登记为发版 runbook 前提（见「未做事项」）。

## 逐 commit 清单（全部 2026-11-11 计划 / 11-12 实现）

| commit | 内容 |
|---|---|
| `da7b9ef7` | 计划（R1-R8 + DoD 10 条） |
| `150a6c4d` | D1：TimeGrain 枚举 + 0143 迁移 + ORM 3 列 |
| `82305861` | D2：resolver 纯函数（57 passed、覆盖 94%、变异 3/3） |
| `33f3ed75` | D3：层 2 接 resolution + grain 枚举比较 + degraded 扩展 |
| `42a985c7` | D3 修复：无源类⇒unknown、歧义 token 不升 mismatch、接线夹具补回填后状态 |
| `a4af4ef8` | D4：DTO/API 字段 + `_GOVERNANCE_KEYS` + admin 写测试 |
| `eb2333dd` | D5：回填脚本（service 415 行 + CLI 428 行，默认 dry-run） |
| `c9f9b5f7` | D5 修复：候选仅限已建模源列 + 显式值保留回归测试 |
| `ed8b2542` | D6：前端治理表单两字段（47/47 targeted 绿 + tsc 0 错误） |

## DoD 10 条逐项

| # | 条款 | 结果 |
|---|---|---|
| 1 | 迁移 0143 三列 + ORM 同步 + 全新库 --strict exit 0 | ✅（D1；全新空库 upgrade head → drift --strict exit 0 取证在 D1 报告） |
| 2 | TimeGrain 枚举独立于 time_scope_resolver | ✅（enums.py，`time_scope_resolver` 未动） |
| 3 | resolver 纯函数四态 + 别名分支 | ✅（D2；别名分支按 R5 只测结构不计命中率） |
| 4 | 层 2 resolution 语义 + 恒等回退 | ✅（D3；P2 parity 测试断言零修改仍绿） |
| 5 | 存量 12 verdict 不变（8 pass/4 unknown） | ✅（回归测试钉死 + dev 5433 复算探针 PASS，`evidence/stock-verdict-after.txt`） |
| 6 | unknown 放行 + degraded；mismatch 阻断；Tier A 资格不变 | ✅（D3；三态矩阵单测锁死） |
| 7 | DTO/API 双入口 + 闸门 + 403 回归 | ✅（D4） |
| 8 | 回填脚本 dry-run 默认 + 确定性推导 + R3 报告 | ✅（D5；dev 实跑 changes=11 noop=1） |
| 9 | 前端两表单 + i18n + 测试 | ✅（D6） |
| 10 | 变异取证 ≥4 组 + 覆盖率 ≥80% + 定向全绿 + 净新增 0 | ✅（resolver 3/3、reconcile 5 循环、backfill 5 组取证；新模块覆盖 94%；定向回归见 PR CI；邻近套件仲裁在 PR 描述） |

## dev 元数据库执行记录（2026-11-12，用户批准「两项都执行」）

1. `alembic upgrade head`：0142 → 0143，EXIT=0（3 nullable 列）。
2. 回填 `--apply --allow-prod-write`：**changes=11 noop=1**（11 条 month 双侧写入；
   metric=5 额外 `time_column=INVOICE_DATE`；metric=2/3/7/11 time_column 多候选不写；
   metric=10「供应商（快照）」全 noop）；幂等复跑 **changes=0 noop=12**。
3. 证据：`evidence/dev-backfill-before.txt`、`dev-backfill-after.txt`、`dev-backfill-run.md`、
   `backfill-report.txt`（写前 dry-run）、`stock-verdict-after.txt`（回填后 verdict 探针）。

## 测试与评审证据

- 每任务 TDD（RED→GREEN）+ 任务评审（D1-D6 全部 Approved；D3/D5 各经一轮修复 + scoped re-review）。
- 变异取证：`evidence/mutation-resolver.txt`（3/3）、`mutation-reconcile.txt`（5 循环）、`mutation-backfill.txt`。
- 定向回归：unit `-k kpi` 全量 + 邻近集成套件（diagnose/tier_a_chat/contract_injection×2/reconcile/
  stock_verdict/tier_a_execution_pg/metric_backfill_apply/time_grain_*）。
- 整分支双评审：code-reviewer + security-reviewer（结论见 `final-review.md` / `security-review.md`）。

## 未做事项（登记）

1. **别名数据补录**：`business_aliases` 3717 行全 null、`property_alias` 仅 3 条非空 —— 独立数据工作项（R5）。
2. **time_column 治理侧人工指定**：metric=2/3/7/11 多候选（PROMISED_RECEIPT_DATE vs RECEIPT_DATE）、
   其余零候选，须业务确认后经 admin API 指定（R3）。
3. **P4 参数化/编译器**：按序启动；编译器可先吃 time_grain（time_column 允许为 null）。
4. **部署**：生产仍在 alembic 0140 + 旧代码。发版 runbook 必须按序执行：
   1. **迁移先于新代码**：`alembic upgrade head`（→0143）必须先于新代码上线 ——
      ORM 已声明 `time_grain` / `time_column` 三列，旧库上跑新代码会 `UndefinedColumn`。
   2. **迁移后立即回填（硬闸门）**：执行
      `scripts/kpi_backfill_time_grain.py --apply --allow-prod-write`；预期
      **`changes=11 noop=1`**，幂等复跑必须 **`changes=0 noop=12`** 才算回填成功。
   3. **时间轴语义（Round 2，C 裁决）**：双侧 `time_grain` 都未声明 ⇒ 跳过粒度轴
      （不产生 unknown；快照类 KPI 不被时间轴拦）。仅单侧缺失 ⇒ `time_grain_absent_*`
      unknown；双侧都声明且不等 ⇒ mismatch。
   4. **回填前窗口的真实影响**：治理侧多由 `grain` 文本推导为 month、技术侧仍为 NULL ⇒
      **单侧缺失** ⇒ 原始 verdict **12/12 unknown**，8 条基线 pass KPI 暂失 Tier A
      （degraded **注入**，不是故障、不阻断注入）；回填后 raw 恢复
      **8 pass / 4 unknown / 0 mismatch**（见 `evidence/stock-verdict-after.txt`，已按
      0143+回填后状态重新生成）。注意：不是「resolution=None 恒等回退」——恒等路径会
      忽略时间字段而保持 pass；本分支已接线 resolution，「双侧未声明」的跳过不等于
      「单侧缺失」的放行。
5. **Minor 归集**：D1-D6 各任务评审的 Minor 记录于 SDD ledger（`.superpowers/sdd/plan/progress.md`），
   全部为非阻塞项（注释措辞、共享 helper、类型建模等），随下次触达重构。

## 回滚

- 代码：`git revert` 本分支 9 个 commit。
- 迁移：`alembic downgrade 0142`（drop 3 列；列均 nullable 新增，无数据风险；0143 写入的值随列删除消失）。
- dev 5433 已写入的 11 条 month / 1 条 time_column：downgrade 即消除；如需保留列仅清值，
  用回填脚本思路反向置 null（当前无此需求）。

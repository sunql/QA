# KPI Tier A 执行器（P2）— 变更记录

> 分支：`feat/kpi-tier-a-executor`（base `origin/main` `00f3b13e`）
> 设计稿：`docs/superpowers/specs/2026-11-11-kpi-definition-consumption-design.md` §6 / §9 / §13-M2
> 计划：`Harness/changes/2026-11-11-kpi-tier-a-executor/plan.md`
> 范围：只做 P2。**未部署、未建生产开关行、零 DDL。**

## 背景

P0 补齐了对账与治理字段，P1（#119 matcher v2 + #120 写 ACL）让「问句 → KPI」的命中可信。但命中之后
`_buildL1Response` 仍按 `FeatureDefinition.calculation_logic` 执行，且该匹配用**散文列等值**比对技术公式，
dev 库 12 KPI 实测命中 0 ⇒ 即使认出了指标，数字也不是按指标定义算的。

P2 把命中后的回答拆成四档，让「有可执行定义」的指标走**服务端确定性执行**，其余分档降级并如实标注原因：

```
L1 hit ──┬─ 开关关 ─────────────────────────────► LEGACY（旧行为逐字不变）
         ├─ metric 缺失 / 来源 NONE / 无数据源 ──► C（明确文案，不执行、不编数）
         ├─ COMPILED / 未知来源 / 显式时间限定 /
         │  formula 空 / 对账不通过 ─────────────► B（带降级原因，走 LLM 但跳过 L4）
         └─ 其余（ESCAPE_HATCH + 可对账）────────► A（执行 metric.formula，0 token）
```

## 路由决策表（`kpi_execution_service.planKpiExecution`，纯函数，按序短路）

| # | 条件 | Tier | `degradedReason` |
|---|---|---|---|
| 1 | 开关关 | `LEGACY` | `tier_a_disabled` |
| 2 | 未命中 KPI | `LEGACY` | `kpi_not_found` |
| 3 | `ontology_metric` 缺失 | `C` | `metric_missing` |
| 4 | 来源为空 | `C` | `execution_source_missing` |
| 5 | 来源 `NONE` | `C` | `execution_source_none` |
| 6 | 来源 `COMPILED` | `B` | `compiled_not_supported` |
| 7 | 来源为未知值 | `C` + warning | `execution_source_unknown` |
| 8 | 无 `datasource_id` | `C` | `datasource_missing` |
| 9 | 问句含显式时间限定 | `B` | `explicit_time_scope` |
| 10 | `formula` 为空 | `B` | `formula_empty` |
| 11 | 对账 `isTierAEligible=False` | `B` | `reconcile_not_eligible` |
| 12 | 其余 | `A` | `null` |

执行器失败另有四个 reason：`formula_not_read_only` / `execution_timeout` / `execution_error` / `empty_result`。

## 关键实现

- **开关双闸 fail-closed**：env `KPI_METRIC_TIER_A_ENABLED`（默认 `true`，紧急杀闸）+ `system_config`
  同名行；行缺失 / 非法 / 读异常 ⇒ 关。生产**没有该行** ⇒ 生产仍走旧行为。
- **执行器**：`_assert_read_only`（第一道）→ 业务库 adapter（只读事务、30s 超时、`fetchmany(maxRows+1)`）
  → 截断至 1000 行并标 `truncated` → `firstPresentValue`（首行第一个非 None）。`tokensUsed=0`、`cost=0.0`。
- **Tier B 跳过 L4**：L4 不注入 KPI 契约块。用路由层哨兵 `L1B` 返回 `PreambleResult`，两个入口在
  L1/L4 分支**之前** `splitTierBPreamble` 拆包。
- **可观测**：`ChatResponse.executionTier` / `degradedReason`；流式两处 meta 帧同名字段，按意图门控
  （QUERY / NEW_QUERY / REFINE / FOLLOW_UP / CLARIFY），重分类到早退意图时第二帧显式发 `null`。
- **诊断对齐**：`kpi_diagnose._tierOf` 与运行时同口径（`COMPILED ⇒ B`），并由 parity 测试钉住。

## 变更清单（按天）

| 天 | Commit | 内容 |
|---|---|---|
| D1 | `dbb2037c` | 双闸开关（fail-closed） |
| D1 | `c5c73160` | 路由决策纯函数 |
| D2 | `ce786629` | adapter 支持单次 `maxRows` 上限 |
| D2 | `a5a67f20` | 只读执行器（Guard/超时/行数/归一化/证据） |
| D2 | `a74bc4e1` | 修复：`maxRows` 转发兼容 + adapter 构造降级 |
| D3 | `6f163228` | 显式时间限定检测（一期只判有无） |
| D3 | `bde469a1` | 计量/日志接线测试 |
| D3 | `eb77f238` | 修复：补齐时间形态漏判 |
| D4 | `e83ec781` | preamble 接入 A/B/C/LEGACY 路由（Tier B 跳过 L4） |
| D4 | `d25831a2` | 修复：preamble 调用纳入降级边界 |
| D5 | `88565774` | `ChatResponse` Tier 字段 + hint 单测 |
| D5 | `a9d5301b` | 两入口接线 `executionTier`/`degradedReason` |
| D5 | `5e230a9b` | 全链路集成（真实 PG + 完整 API） |
| D5 | `bdea823f` | 修复：观测字段按意图门控 |
| D5 | `88885b9a` | 修复：CLARIFY 归入门控集合 |
| D6 | `97b54940` | 变异取证 + 覆盖率 + 真实数据探针 |
| D6 | `23e4f281` | 修复：探针护栏拒绝 query 参数、抑制隐式证据写入 |
| D7 | `49cbb705` | 终审修复：parity / `/*!` Guard / 时间模式 / 探针目标确认 / 脱敏 |
| D7 | `9e440c0b` | 复审修复：H1/H2 边界、清空字段按 hint 键派生、诊断 `tierAEligible` 与 `_tierOf` 一致性、`/*M!` 拒绝、证据命令、wiki 图对齐 |

## 验收证据

证据目录：`Harness/changes/2026-11-11-kpi-tier-a-executor/evidence/`

### M2 验收（设计 §13）

| 条目 | 结论 | 证据 |
|---|---|---|
| 8 条 ESCAPE_HATCH KPI 确定性回答，`tokensUsed=0` | ✅ | Tier A 路径不调用 LLM；集成用例断言响应 `tokensUsed=0`、`cost=0.0`；`tier-a-realdata.txt` 8 条全部 `tier=A` 并真实执行 |
| 结果与手工 SQL 对比容差 0.01 | ✅ 6/8，如实记录 2/8 | `tier-a-realdata.txt`：6 条 `diff=0.0`；2 条两端均 NULL ⇒ `empty_result` 降级 Tier B（未伪造数值） |
| 执行失败 100% 带 `degraded_reason`，无静默空答 | ✅ | 非只读 / 超时 / 异常 / 空结果四类用例均断言 Tier B + reason；warning 带 `kpi_code`/`datasource_id`/cause 链 |
| 4 条 NONE 明确返回 Tier C 文案 | ✅ | `planKpiExecution` 矩阵用例 + 真实数据 dry-run 4/4 `tier=C / execution_source_none` |

### Definition of Done（计划 §0，10 条）

1. ✅ 双闸 fail-closed，关 ⇒ 旧行为逐字不变（`test_row_missing_is_false` + 变异 #1）
2. ✅ ESCAPE_HATCH 唯一命中 ⇒ 执行 `metric.formula`，`tokensUsed=0`、`cost=0.0`
3. ✅ `COMPILED ⇒ B`；`NONE`/空 ⇒ C；未知值 ⇒ C + warning（`49cbb705`）
4. ✅ 异常/超时/空结果不静默：warning + Tier B + `degradedReason`
5. ✅ 显式时间限定 ⇒ Tier B（`explicit_time_scope`），覆盖 近 N 天/月/年、季度、半年、财年、Q1–Q4、
   H1/H2、同比/环比、FY、TTM/LTM、月初/月底
6. ✅ 只读三重：执行器 Guard → adapter 再验 → 库侧只读事务；非 SELECT 抛 `SqlSafetyError` 且
   adapter 一次都不调用（变异 #3）。MySQL 可执行注释 `/*!` 已被拒绝
7. ✅ ≤1000 行、超限截断并标 `truncated`；单值取首行第一个非 None
8. ✅ `executionTier`/`degradedReason` 非流式两路径 + 流式 meta 帧都带值
9. ✅ Tier A 成功调度 `METRIC_RESULT`（best-effort，失败只 warning）
10. ✅ 覆盖率 100%（`kpi_execution_service` / `kpi_metric_switches`，见 `coverage.txt`）；
    终态定向 **10 文件 159 passed / 0 failed**；回归失败集合对差 **净新增 0**（`regression.txt`）

### 变异取证

`evidence/mutation-summary.txt`：5 个变异逐个「改 → 红 → 还原 → 复绿」，共 232 行；另有 D7 新增
`/*!` 拒绝、parity、时间模式的定向 RED 取证。

### 已知红基线（非本次引入）

- `test_datasource_pool.py` 2 条 Oracle 文案断言漂移（实现发 `SET TRANSACTION READ ONLY`，用例仍断言
  `ALTER SESSION SET READ ONLY`）—— 在 base `23e4f281` 上同样红。
- 6 个邻近套件的 5 个红 nodeid 同因：夹具插 `BusinessObject` 缺 0133 必填 `header_class_id`，
  `origin/main` `00f3b13e` 上同样红（`regression.txt` 证明净新增 0）。建议另立 issue。

## 未做事项（明确边界）

- **P3 别名词典解析**、**P4 参数化/编译器**（显式时间区间解析）—— 一期时间问题一律降级 Tier B。
- **生产开关行**：`KPI_METRIC_TIER_A_ENABLED` 与 `KPI_MATCH_CONTAINMENT_ENABLED` **均未创建**，
  生产仍走旧行为；**未部署**。上线需另行批准（含灰度量与回滚演练）。
- **前端渲染**：只透传 API 字段，不新增渲染；「未按指标定义口径直接计算」的可见提示留 P5。
- **Tier 分布看板 / routing 指标**：P5；取数可直接读 `executionTier`，无需新增表。
- **KPI 读授权模型**（Tier A 使用 KPI 配置者数据源，未校验调用者对该数据源的读权限）：产品决策，P5。
- **诊断与运行时两处已知分歧**（`datasource_missing`、来源空/未知时诊断判 B 运行时判 C）：
  登记在 `test_kpi_tier_parity.py` 的已知分歧用例，P5 统一。
- **Oracle 第三层只读兜底为 best-effort**（`SET TRANSACTION READ ONLY` 失败不阻断，Guard 仍是主防线）。
- **行数截断 under-report**：若运维把 `QUERY_ROW_LIMIT` 配到 ≤1000，截断可能被少报（已在代码注释与
  风险表登记）。
- **时间检测的残余边缘（已知、非阻塞）**：`1H10` / `2024H10` 会被判为含时间限定（过度检测，按「宁可多判」
  原则只多一次 LLM）；小写 `h1` 未命中（既有缺口，未加 IGNORECASE 以免匹配 HTML 标题标签）。
- **诊断 `tierAEligible` 对「来源空/未知 + 对账 pass」仍为 True**（与 `tier=B` 并存）：与运行时 C 的
  分歧一并留 P5 统一。
- `firstPresentValue` 与 `chat_service._firstPresentValue` 重复，本批不合并。

## 回滚

关开关即回旧行为：env `KPI_METRIC_TIER_A_ENABLED=false`，或删除/改 `system_config` 同名行（fail-closed）。
零迁移、零数据变更；需要彻底回退时整支 revert 即可，无 DB 副作用。

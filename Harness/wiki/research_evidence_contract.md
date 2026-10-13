# 研究会话证据契约（环节一）

**日期**：2026-10-13 · **变更**：[feat-research-evidence-contract](../changes/feat-research-evidence-contract/summary.md)
· **计划**：`docs/superpowers/plans/2026-10-12-research-session-evidence-and-platform.md`（环节一，Task 1–9）

研究会话（`/research/*`）从「跑一次就丢」升级为**可恢复、可复现、可引用**：问题范围在续跑中不丢失、
假设按**数据支持度**判定而不是按「有没有报错」、每一步的真实结果落库并能被最终报告引用。
本页是这层契约的**SSOT 摘要**；实现细节以代码与 `changes/` 变更记录为准。

## 为什么需要这层契约

改前的三个确定性缺陷（全部有对应回归测试）：

1. **范围丢失**：`rebuildState` 从 `input_seed` 还原问题，续跑改写的问题（追问）不回写 ⇒ 恢复态回退到最初问法。
2. **执行权无主**：同一会话可被并发触发多次，两个状态机同时写同一份 state。
3. **读数不可信**：报告里的「已验证」来自 `supporting_data["error"] is None`（**没报错**），
   而不是「数据支持」——零支持的假设照样标「已验证」（见 [[qa-system-two-verified-notions]]）。

## 数据模型

三张表 / 一列，见 [data-model.md §研究会话证据契约](data-model.md#phase-环节一-增量研究会话证据契约表)。

| 对象 | 作用 | 关键点 |
|---|---|---|
| `research_run_state` | 本轮问题**版本化**快照 | 续跑**新增**一行（新 UUID）而不是改旧行；`parent_run_id` 串版本链；`turn_id` 跨版本保持不变 |
| `research_step_result` | 每步真实结果落库 | 挂 `run_state_id`（**不挂** `turn_id`）⇒ 跨版本读取要 `join research_run_state` |
| `research_session.active_run_id` | 会话级执行权互斥 | FK → `research_run_state.id`，`ON DELETE SET NULL` |
| `research_session.idempotency_key` | 重复触发去重 | 原子条件 UPDATE 认领 |

**版本化是承重设计**：`updateRunState` 造新 UUID，`turn_id` 复制过去 ⇒ 同一 turn 名下有 N 个版本。
读侧若只按单个 `run_state_id` 读就**必然漏**早期版本的步结果（这正是 Task 6 留给 Task 8 的洞）。

## 执行权互斥与幂等

- 认领是**原子条件 UPDATE**（不是「先查后写」）⇒ 无 TOCTOU 窗口。
- 释放判定按**会话终态**而不是按 run-id —— 因为 `updateRunState` 会换行，按 run-id 判会漏。
- 两条释放路径：`_guardedRun` 的 `finally` + `markFailed` 重放。
- 幂等键查询**自排除**当前 run。
- 终态会话不可续跑；`abort` 端点只允许 `awaiting_user`（堵住「打断正在跑的状态机」的新缺口）。
- `409` 文案只广告**真有前端入口**的动作（不再广告没有入口的 abort）。

## 序列化契约（SSE）

`research_json_encoder.py` 是研究流的**唯一** SSE JSON 生产者，口径：

| 输入 | 输出 | 为什么 |
|---|---|---|
| `Decimal` | **str** | 与线上契约一致（前端把每个 Decimal 当字符串） |
| `date` / `datetime` | ISO 字符串 | |
| `UUID` | str | |
| 非有限 float（`NaN`/`Infinity`） | **抛错**，不静默 | 裸 `NaN` 是非法 JSON，会**炸整帧**；实测 `SELECT 'NaN'::float8` 经 asyncpg 真能返回 `nan` |
| 白名单外类型 | **抛错**（不是 `default=str`） | 保持改动前行为；`default=str` 才是新引入的静默损坏 |

> 本仓有**两套** JSON 安全映射，别混：`_coerceJsonSafe`（Decimal→**float**，未知类型**静默**字符串化）
> vs `research_json`（Decimal→**str**，未知类型**TypeError**）。见 memory `qa-system-two-json-safe-mappings`。

**已知缺口（遗留）**：`_sseFrame` 上的 `research_json` 是**无守卫**调用 ⇒ 若 `EVENT_STEP_DATA`
的 `data` 里混进 `bytes` 行（驱动层可产生），该帧会抛错断流。落库路径已逐行容错（`_jsonSafeRowsPerRow`），
SSE 路径没有。已登记，未修。

## 证据摘要与假设判定

- `EvidenceSummary`（`research_evidence_summary.py`）：把一步的原始行压成
  `{total, baseline, current, delta, deltaRatio, topN, timeRange, ...}`，
  数值标量**串化**（Decimal→str 口径）。判列类型时要区分**度量列**与**时间轴列**——
  整数型时间轴（会计年度/统计年月）与整数度量在类型上不可分，只能靠**取值形状 + 名字**判。
- 假设判定（`research_hypothesis_test.py`）产出 `verdict ∈ {supported, inconclusive, refuted, ...}`。
  报告里的 `findingsRef[].verified` 现在 = `verdict == "supported"`；**缺 verdict 的历史行**回落
  `error is None`（向后兼容，见 [[qa-system-two-verified-notions]]）。
- 否定式方向词（「没有下降」）不得被读成「下降」：I1 门 = **否定字 + 情态词（含 `是`）· 距方向词 ≤ 4 字窗口**。

## 报告引用步结果

`ReportPlanner.compose` **自加载**该 turn 名下**所有版本**的步结果
（`loadStepResultsByTurn`：`research_step_result ⋈ research_run_state ON run_state_id WHERE turn_id = :t`，
排序 `(run_state.created_at, run_state.id, step_index)`），装配成：

```
payload["evidence"]["stepResults"][i] = {index, type, sql, status, error, summary, schemaVersion}
```

**顶层恰好 7 个字段**：**不含** `rows`（体积）、**不含** `raw_result_ref`、**不含** `scope`。

> ⚠️ **顶层没有原始行 ≠ 整条目没有**：`summary` 是整块 `EvidenceSummary`，其
> `sampleRows` 带 **≤5 行真实业务行样例**（D14 有意设计——报告要能给人看几行样例）。
> 即报告 payload 里**确实**存在业务行副本（`research_step_result.evidence_summary`
> 一份 + report payload 一份）。owner-scoped，不是越权；但属于**留存面扩张**，
> 收紧与否是产品/合规裁定（见变更记录 §7 遗留）。
降级：读取失败 ⇒ `[]` + 警告，不杀 turn（`begin_nested()` savepoint 隔离）。
前端 `EvidencePanel.tsx` 渲染（缺席/空 ⇒ 不渲染），`key = {index}-{position}` 避免跨版本重复键。

## 迁移

| 文件 | 内容 |
|---|---|
| `alembic/versions/b1ea6cb32588_run_state.py` | `research_run_state` 建表 |
| `alembic/versions/0118_research_active_run.py` | `research_session.active_run_id` + FK + 索引 |
| `alembic/versions/0119_research_step_result.py` | `research_step_result` 建表 |

三个文件名均 ≤ 32 字符（含 `.py`）；前两个 2026-10-13 由超限名改短，文件内 `revision` 未动。

## 启动期漂移门（`schema_drift.py`）

应用启动时解析 `alembic/versions/*.py` 的 revision 图，校验「单一 head」+ ORM↔DB 一致。
**2026-10-13 部署冒烟踩到**：`_DOWN_REVISION_LINE` 只认双引号，而 `--autogenerate` 模板用**单引号**
⇒ `0117 -> b1ea6cb32588` 这条边被漏解析 ⇒ `0117` 被误判成未闭合 head ⇒
`heads=['0117','0119']` ⇒ **拒绝启动**。修法：值两侧 `["']`（见 memory `qa-system-alembic-targets-prod`）。

## 相关条目

- [[data-model]]（表结构与字段）
- [[api-reference]]（`/api/v1/research/*` 端点）
- [[chat_multi_step_persistence]]（同源的「续跑」机制，在 chat 侧）
- [[architecture]]（进程内 BackgroundTasks 与状态机分层）

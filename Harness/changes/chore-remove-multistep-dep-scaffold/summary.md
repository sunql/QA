# 变更：chore-remove-multistep-dep-scaffold

- **日期**：2026-10-18
- **作者**：Claude
- **Phase**：Phase 1 多步 NL2SQL（L1 线性计划）
- **状态**：in-review
- **关联变更**：[chore-chart-service-deadcode](../chore-chart-service-deadcode/summary.md)（同为死代码清理批次）、[chore-l3-deadcode-and-prior-cte-contract](../chore-l3-deadcode-and-prior-cte-contract/summary.md)
- **迁移版本**：无
- **MEMORY**：[../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-multistep-dep-scaffold-removed.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-multistep-dep-scaffold-removed.md)
- **commit**：`e492b68b`

---

## 1. 需求

L1 多步计划声称支持「显式步间依赖」，但 `StepPlan` 上的 `depends_on` / `input_refs` /
`output_schema` / `execution_group` / `on_failure` 五个字段**全仓没有任何生产者**：

- `StepQueryPlanner` 构造 `StepPlan` 时从不传这些字段（全部走默认值）；
- 因此 `MultiStepDependencyResolver.validate()` 每次校验的都是空依赖集，恒不抛错；
- 依赖失败 → `StepResult(skipped=True, skip_reason=...)` 的分支永不触发 ⇒
  `runStatusFor` 的 `partially_failed` 分支、`StepResult.skipped` 读侧（降级收尾统计）
  全都是死路径；
- `on_failure="abort"` 同理无生产者，提前 break 永不发生。

用户视角：无功能变化（这些路径本来就不可达），但代码把「线性执行」伪装成「依赖 DAG」，
让后续维护者误以为改了 `depends_on` 会生效、误以为 `partially_failed` 会出现。

**验收标准**：删除上述字段、resolver、skip 分支与专属测试；`runStatusFor` 只产出
`succeeded` / `failed`；全量相关套件无净新增失败；历史 `partially_failed` 行的续跑 /
清理语义保持不变。

## 2. 设计评审

### 候选方案

| # | 方案 | 优点 | 否决理由 |
|---|---|---|---|
| A | 接线（让 planner 真填 `depends_on`，实现 DAG 调度） | 兑现字段承诺 | **YAGNI**。2026-09-28 诊断已证伪「多步之间需要步间数据依赖」（见 [[qa-system-multistep-no-data-dependency]]）；L1 的 prompt 注入已覆盖「后续步骤参考前序结果」的实际需求。引入 DAG 调度是净新增复杂度，无需求来源 |
| B | 只删执行逻辑，保留字段作 DTO 占位 | diff 最小 | 字段仍在、仍无人填 ⇒ 下一个人还会问同一个问题；占位字段本身就是文档 rot 源 |
| C | **整块删除（选定）** | 代码与真实语义一致；少一个 resolver + 2 个测试文件（623 行删除 / 16 行新增） | 唯一代价：若 L2 真要做并行组需重新引入 —— 届时按真实需求设计，而不是复活一份没人用过的草稿 |

### 最终决定

方案 C。用户明确选择「删除死代码」。`RUN_STATUS_PARTIALLY_FAILED` / `STEP_STATUS_SKIPPED`
**常量保留** —— 历史行仍可能带这两个取值，续跑（`RESUMABLE_STATUSES`）与清理任务需要识别它们。

## 3. 数据模型变更

无。表结构、列、索引、迁移脚本均不变。`multi_step_run.status` / `multi_step_step.status`
是自由字符串列，历史行中的 `partially_failed` / `skipped` 仍可读。

## 4. 接口契约变更

无 API / DTO 字段变化。唯一可观察差异：新产生的 run **不再可能**出现
`status="partially_failed"`（此前该值同样不可达）。前端无任何代码引用该值。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/domain/multi_step_plan.py` | `StepPlan` 删除 5 个字段；`StepResult` 删除 `skipped` / `skip_reason` |
| `backend/app/services/chat_multistep.py` | 删除 resolver import / `validate()` / `failed_indexes` / 依赖失败 skip 分支 / `on_failure=="abort"` break；降级收尾的 `r.skipped` 统计收敛为 `r.sql is not None` |
| `backend/app/services/chat_stream.py` | 删除 `anySkipped` 及两处传参 |
| `backend/app/services/multi_step_persist_hooks.py` | `runStatusFor(completed, total, anyFailed)`：去掉 skipped 项与 `partially_failed` 产出；删除未用 import |
| 删除 | `app/services/multi_step_dependency.py`、`unit/test_multi_step_dependency.py`、`unit/test_chat_multistep_dep.py` |

**不动的边界**：`app/domain/chained_step_plan.py` 是研究链路的独立子系统（字符串
`step_id` 依赖），与本处无关，保留原样。

行为等价性论证：旧 `anyFailed and not anySkipped → failed`；`anySkipped` 恒 False ⇒
等价于 `anyFailed → failed`。降级收尾同理（skipped 结果永不出现在 `completed` 中）。

## 6. 测试

删除 2 个只测试已删行为的文件（resolver 单测 17 例、`on_failure` 分支 2 例），**无存活用例失去覆盖**
（无测试直接断言 `runStatusFor`；终态由集成测试间接钉死）。

本轮验证（真实 PG 5434 / `qa_metadata_test_solo`）：

| 套件 | 结果 |
|---|---|
| `unit/test_multi_step_plan` · `test_step_aggregator` · `test_multi_step_persist_hooks` · `test_multi_step_compressor` · `test_multi_step_retry` · `test_chat_multistep_rewrite_hook` · `test_chat_multistep_security` · `test_chat_service` | 262 passed，4 failed |
| `unit/test_chat_service_stream` · `test_stream_events*` | 40 passed，2 failed |
| `integration/test_multi_step_persist_wiring` · `_persist_repo` · `_resume_api` · `_cleanup` | 36 passed |
| `integration/test_chat_multi_step` | 38 passed |

6 个失败全部在 **main 基线同样红**（4 个 `TestBuildAnswerPrompt` 提示词文案漂移 + 2 个流超时
错误分类漂移），已在 `origin/main` 派生的 worktree 复跑确认，**净新增失败 0**。

## 7. 安全审查

未触发 security-reviewer：改动为纯删除，不涉及认证、SQL、用户输入、文件操作、外部 API。
code-reviewer 结论：CRITICAL 0 / HIGH 0 / MEDIUM 1（即本变更记录缺失，已补齐）/ LOW 1（文档措辞，已修）。

## 8. 部署验证

重构类可省略（[变更记录强制规范 §三](../../rules/变更记录强制规范.md)）。无迁移、无配置变更。

## 9. 关联

- Wiki：[chat_multi_step_persistence.md](../../wiki/chat_multi_step_persistence.md) §2（状态机已标注 legacy）、[nl2sql-engine.md](../../wiki/nl2sql-engine.md)（2026-08-17 回归叙述已改为实际机制）
- Rules：[变更记录强制规范](../../rules/变更记录强制规范.md)、[测试规范](../../rules/测试规范.md)
- Memory：`qa-system-multistep-dep-scaffold-removed`、[[qa-system-multistep-no-data-dependency]]
- 关联变更：P1-4 引入的依赖脚手架（见 [2026-10-15-phase3-reconciled-main](../2026-10-15-phase3-reconciled-main/summary.md)）

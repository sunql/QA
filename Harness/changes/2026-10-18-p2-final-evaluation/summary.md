# 变更：2026-10-18-p2-final-evaluation

- **日期**：2026-10-18
- **作者**：Claude / phase3-reconciler
- **Phase**：Phase 3 P1/P2 收尾 — 最终评估
- **状态**：final
- **类型**：评估记录（无代码变更）
- **迁移版本**：无
- **关联变更**：[chore-p2-residual-cleanup](../chore-p2-residual-cleanup/summary.md)、[chore-p2-cost-source](../chore-p2-cost-source/summary.md)、[chore-p2-errortype](../chore-p2-errortype/summary.md)、[chore-p2-jsonsafe](../chore-p2-jsonsafe/summary.md)、[2026-10-15-phase3-reconciled-main](../2026-10-15-phase3-reconciled-main/summary.md)

---

## 1. 背景与验收口径

承接 AIChat 服务优先级计划（P0/P1/P2）。P0-3/P0-4、P1-1/P1-2/P1-4/P1-5、P2-1 主体已在
[2026-10-15-phase3-reconciled-main](../2026-10-15-phase3-reconciled-main/summary.md) 合流；本批处理收尾审计发现的
「已实现但未接线 / 只写不读 / 重复实现」残余项。

**验收口径**：

1. 每项有独立分支、独立 PR、SSOT 变更记录、Wiki 与 MEMORY 更新；
2. 后端测试用真实 PostgreSQL（`qa_metadata_test_solo`），禁用 sqlite；
3. 全量回归与 `origin/main` 基线逐条对比，**净新增失败必须为 0**；
4. 前端测试断言真实文案并反向断言无 i18n 裸 key（`returnNull: false` 假绿陷阱）。

## 2. 本批交付清单

| 项 | 分支 | PR | 内容 |
|---|---|---|---|
| P1-3 收尾 | `fix/chat-assistant-message-id-lifecycle` | #60 | chatStore `currentAssistantMessageId` 生命周期收严 + doc-QA 轮次所有权 |
| P2 死代码 | `chore/p2-dead-code-cleanup` | #61 | 删除未接线模块（turn_result_repository / request_deadline / ModelCapability / Hypothesis.verify）；HypothesisPanel 状态白名单 + i18n，修裸 key 假绿 |
| P1-2 收尾 | `chore/p2-cost-source` | #62 | `cost_source` 全写入方标注（measured/estimated/unknown）+ 读契约 `costSource` + UsagePage「成本口径」白名单列 |
| P2-1 收尾 | `chore/p2-errortype` | #63 | SSE `errorType` 解析 → chatStore → MessageItem 7 类白名单提示；未知类型静默 |
| P2 去重 | `chore/p2-jsonsafe` | #64 | `chat_chart_persist._jsonSafe` 收敛进 `utils.json_safe.jsonSafe` SSOT |

## 3. 数据模型变更

无新增。`session_token_usage.cost_source`（0126）由「只写不读」补齐为端到端字段。

## 4. 接口契约变更

| 接口 | 变更 | 兼容性 |
|---|---|---|
| `GET /api/v1/sessions/{id}/usage/list` | 新增 `costSource: "measured" \| "estimated" \| "unknown" \| null` | 纯新增；旧行 null |
| SSE `error` 帧（前端消费） | `onError(message, detail?, errorType?)` 新增第三参 | 前端内部契约；后端早已发送 |

## 5. 实现要点

见各分支 SSOT §5。共性决策：

- **系统边界字符串一律白名单收窄**：`errorType`、`costSource`、verification status 均不拼 i18n key、不直接渲染；
- **不谎报口径**：研究链路查不到 `llm_config` 时 `cost_source=unknown`（成本占位 0），历史 NULL 不补写；
- **同名不合并**：`stream_events._jsonSafe`（SSE 契约）与 `research_session_service._coerceJsonSafe`（研究契约）刻意保留。

## 6. 测试与回归

### 各分支

| 分支 | 结果 |
|---|---|
| cleanup | 后端受影响单测 59 passed；前端 12 passed + tsc；ruff 通过 |
| cost-source | 后端 30 passed（真实 PG）；审查补丁后扩展回归 247 passed / 4 failed（全部为 `TestBuildAnswerPrompt` 存量 rot，与基线同名）；前端 8 passed + tsc |
| errortype | 前端 3 文件 127 passed + tsc（含原型链键回归） |
| jsonsafe | 后端 99 passed（真实 PG）；ruff 通过 |

### 全量 unit 回归（cleanup 分支，真实 PG）

- **4232 passed / 28 failed / 53 warnings**（1001s）。
- 28 个失败在 `origin/main`（6bb757ae）基线 worktree 上用同一组用例复跑：**同样 28 failed**，逐条同名 ⇒ **净新增失败 0**。
- 失败分布：`test_refine_shortcuts.py`(15)、`test_chat_service.py`(4)、`test_rate_limit.py`(3)、`test_chat_service_stream.py`(2)、`test_datasource_pool.py`(2)、`test_dependencies.py`(1)、`test_intent_classify_order.py`(1)、`test_no_sqlite_fallback.py`(1)。全部为存量 rot，无一触及本批改动模块。

## 7. 独立审查

| 审查 | 范围 | 结论 |
|---|---|---|
| python-reviewer | 3 个后端分支 | **PASS**。1 个 Important：失败补账路径（报告总结 / 假设生成 / chat fallback）把异常 token 标成 `measured`，与常量定义矛盾 ⇒ 三处改 `estimated` 并加用例；1 个 Minor：`COST_SOURCES` 是死代码 ⇒ 写入边界按词表校验 fail fast、默认值改 `unknown`。均已修复（PR #62 `4e1f1c50`）。 |
| typescript-reviewer | 3 个前端分支 | 1 个 Important：`errorType` / `costSource` 白名单用下标 + `??`，`constructor`/`toString` 原型链键会命中继承值（`t(fn)` 抛错且无 ErrorBoundary）⇒ 两处改 `Object.prototype.hasOwnProperty.call` 并加回归用例。若干 Minor（类型注释、测试命名）已顺手处理。已修复（PR #62 / #63）。 |
| security-reviewer | 4 个分支全量 diff | **Critical 0 / High 0 / Medium 0**。2 个 Low（纵深防御建议）已采纳：写入口口径词表校验、白名单查表原型链隔离。无新增输入面、无 SQL/权限/密钥变更。 |

审查结论：无 CRITICAL/HIGH 遗留；Important 2 项已修复并重新跑测试（见 §6）。

## 8. 剩余风险与明确不做项

1. **存量 28 个 unit 红**：main 上已红，本批未修（非本批引入）。仓无 CI（memory `qa-system-no-ci-main-can-go-red`），红基线会持续漂移。
2. **hypothesis 验证状态链路未接线**：前端「验证」把 SQL 当普通问题发送，后端无写入路径 ⇒ 标签恒为「待验证」。需要新 API + 前端改调用，属新特性而非收尾。
3. **`llm_config` 无模型能力字段**：按任务能力路由需先加列与编辑 UI（`ModelCapability` 已删除，避免半成品接口）。
4. **超大文件拆分（line-count splits）明确延后**：`ontology_service.py`(2119)、`chat_service.py`(2033)、`wiki.py`(1432)、`chat_stream.py`(1398)、`data_quality` 页(1640)、`chatStore.ts`(1155)、`chat_multistep.py`(991) 超 800 行上限。拆分会大幅扩大 diff 且与本次无行为关联，记录豁免，另开独立重构批次。
5. **部署顺序**：cost-source 后端先发、前端随后；其余为纯前端或纯内部重构。前端发布必须 `docker compose build`（memory `qa-system-frontend-deploy-build-required`）。

## 9. 关联

- 各分支 SSOT（见 §1 关联变更）。
- Rules：[变更记录强制规范](../../rules/变更记录强制规范.md)、[测试规范](../../rules/测试规范.md)。
- MEMORY：`qa-system-cost-source-labeling`、`qa-system-errortype-end-to-end`、`qa-system-jsonb-json-safe-ssot`、`qa-system-p2-dead-code-cleanup`。

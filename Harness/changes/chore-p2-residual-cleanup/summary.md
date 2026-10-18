# 变更：chore-p2-residual-cleanup

- **日期**：2026-10-18
- **作者**：Claude / phase3-reconciler
- **Phase**：Phase 3 P2 收尾（死代码清理 + hypothesis 验证状态 i18n）
- **状态**：in-review
- **关联变更**：[2026-10-15-phase3-reconciled-main](../2026-10-15-phase3-reconciled-main/summary.md)（P2-1 / P1-5 初版）、[aichat-p2-followups-2026-10-16](../aichat-p2-followups-2026-10-16/remaining-work-ssot.md)
- **迁移版本**：无
- **MEMORY**：[qa-system-p2-dead-code-cleanup.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-p2-dead-code-cleanup.md)

---

## 1. 需求

Phase 3 收尾审计发现四块「已实现但从未接线」的死代码，以及一个假绿测试：

1. **`TurnResultRepository`**（`app/services/turn_result_repository.py`）：只有自己的单测引用，生产代码零调用。
2. **`RequestDeadline`**（`app/services/request_deadline.py`）：P2-1 预留 dataclass，唯一引用是自己的单测；`POST /api/chat/error/classify` 的 deadline 语义从未实现。
3. **`ModelCapability` + `selectModel(required_capabilities=…)`**：能力过滤分支只在单测里被触发；`LlmConfig` 实体与 DTO 都没有 `capability` 字段，生产调用点全部不传，`getattr(c, "capability", None)` 恒为 `None`。
4. **`Hypothesis.verify()` / `VerificationResult` / `_recordVerification`**：验证判定与落库方法只有单测引用；前端「验证」按钮是把 `verificationSql` 当普通问题重发，后端从不写 `verification_status`。
5. **`chat.hypothesis.status.*` i18n 缺失 + 假绿测试**：`HypothesisPanel` 拼 `chat.hypothesis.status.<status>`，中英文资源都没有该块，缺 key 时 i18next 回落成 key 本身；旧用例用 `/pending/i` 这类子串断言，恰好命中回落 key，等于没测。

**验收标准**：

- 删除上述死代码及其单测，全仓无残留引用（历史变更记录除外）。
- 验证状态标签显示真实中英文文案；未知状态回落「待验证」，不渲染原始 key。
- 测试断言真实文案，并显式断言不出现 `chat.hypothesis.status.` 原始 key。

## 2. 设计评审

### 删除 vs 实现（YAGNI）

四者都是「预留接口 + 单测」而没有生产调用点。按 YAGNI 与 P0-2 的先例（已删死代码，PR #59），选择删除而不是补完：

| 对象 | 若要「补完」需要的改动 | 决定 |
|---|---|---|
| `TurnResultRepository` | 需要把 turn_results 写进 `ResearchReport.payload` 的调用点 + 回放读取 | 删；当前报告链路用其他字段承载 |
| `RequestDeadline` | 需要给 chat 请求链路接 deadline 并逐层透传 | 删；无既定需求 |
| `ModelCapability` | 需要 `llm_config` 加 capability 列 + admin 编辑 UI + 各调用点声明需求 | 删；需求未立项 |
| `Hypothesis.verify` | 需要新 API（执行 SQL + 写回 status）+ 前端改调用 | 删；现有「验证」走普通 QUERY 链路，语义不同 |

保留但暂不消费的字段（`AnalysisHypothesis.verification_status/support_count/rows_sample`、`HypothesisRead` 对应字段）**不删**：API 契约已有、历史数据可能非空，删除需要迁移且无收益。它们现在是「永远为 null 的只读字段」，在 §7 已知遗留中明确。

### i18n 收窄放在渲染边界

`verificationStatus` 是数据库字符串（系统边界）。渲染前用白名单收窄，未知值回落 `pending` —— 与仓库其他枚举（`chartType` / `isIntent`）同口径，避免把未知枚举拼进 i18n key 后显示原始 key。

## 3. 数据模型变更

无迁移。删除纯代码。

## 4. 接口契约变更

无对外 API / DTO 变更。

- 内部 `ModelRouterService.selectModel` 删除可选参数 `required_capabilities`：全仓生产调用点均为两参/三参调用，无调用方传该参数。
- `Hypothesis` dataclass 删除 `verify()` 方法：无生产调用点。
- `HypothesisRead.verification_status/support_count/rows_sample` **保留**（见 §2）。

## 5. 实现要点

- `backend/app/services/turn_result_repository.py`、`request_deadline.py` 删除；对应单测删除。
- `model_router_service.py`：删除 `ModelCapability` dataclass 与能力硬过滤分支；保留路由主逻辑不变。
- `hypothesis_service.py`：删除 `VerificationResult`、`Hypothesis.verify()`、`_recordVerification()`；清理随之无用的 `asyncio` / `field` / `Callable` / `Any` / `Literal` / `update` 导入，以及 `buildHypothesisMessages` 里一个未被使用、ruff 报错的 `neutralizeFence` 导入。
- `frontend/src/components/chat/HypothesisPanel.tsx`：验证状态白名单 `KNOWN_VERIFICATION_STATUSES` + 颜色表 + `asVerificationStatus()`；未知状态回落 `pending`。
- `frontend/src/i18n/{zh-CN,en-US}.ts`：新增 `chat.hypothesis.status.{pending,supported,contradicted,inconclusive,execution_failed}`。
- `frontend/src/tests/HypothesisPanelVerification.test.tsx`：断言真实文案（待验证/支持/相反/无法判定/执行失败），每条用例断言 `queryByText(/chat\.hypothesis\.status\./)` 为空；新增未知状态回落用例。

## 6. 测试

- 后端：受影响单测（`test_hypothesis_security` / `test_hypothesis_service` / `test_model_router` / `test_research_hypothesis_adapter` / `test_research_hypothesis_rewrite`）→ **59 passed**。
- 后端全量 unit 套件：见下方「验证结果」。
- 前端：`HypothesisPanelVerification.test.tsx`（6 例）+ `HypothesisPanel.test.tsx`（6 例）→ **12 passed**；`npx tsc --noEmit` 通过。
- RED 证据：把 `HypothesisPanel.tsx` 与两份 i18n 资源换回 `HEAD`（改前）版本后，新用例 **6 failed**；恢复后 **6 passed**。

验证结果：

- 全量 unit 套件（本分支，真实 PG `qa_metadata_test_solo`）：**4232 passed / 28 failed / 53 warnings**（1001s）。
- 28 个失败**全部为存量红**（rot）：在 `origin/main`（6bb757ae）基线 worktree 上跑同一组 28 个用例，同样 **28 failed**（8.3s），逐条同名 ⇒ **净新增失败 0**。失败集中在 `test_refine_shortcuts.py`(15) / `test_chat_service.py`(4) / `test_rate_limit.py`(3) / `test_chat_service_stream.py`(2) / `test_datasource_pool.py`(2) / `test_dependencies.py` / `test_intent_classify_order.py` / `test_no_sqlite_fallback.py`；**无一**触及 `hypothesis_service` / `model_router_service` / 本次删除的模块。
- `uv run ruff check app/services/hypothesis_service.py app/services/model_router_service.py` → All checks passed。

## 7. 安全审查

不触发 security-reviewer：删除的均为无调用方代码；i18n 为纯展示层；无新增输入、查询、网络或存储出口。删除 `Hypothesis.verify` 不降低安全性（它不执行 SQL，只做静态判定；真正执行验证 SQL 的仍是既有 QUERY 链路 + SQL Guard）。

已知遗留（明确不修）：

- hypothesis 验证状态链路**仍未接线**：前端「验证」把 SQL 作为普通问题发送，后端没有任何路径写 `verification_status` ⇒ 标签恒为「待验证」。若要流转，需要新 API + 前端改调用（另有设计）。
- `llm_config` 无模型能力字段；按任务能力路由需要先加列与编辑 UI。

## 8. 部署验证

- 无迁移、无后端行为变更（删除的都是无调用方代码）。可只发前端，后端随下次发布即可。
- 前端仍需 `docker compose build`（memory `qa-system-frontend-deploy-build-required`）。
- 冒烟：触发「分析原因」问题 → 假设卡片的状态标签显示「待验证」而不是 `chat.hypothesis.status.pending`。

## 9. 关联

- Wiki：[Harness/wiki/frontend.md](../../wiki/frontend.md)（HypothesisPanel 状态白名单与 i18n）。
- Rules：[Harness/rules/变更记录强制规范.md](../../rules/变更记录强制规范.md)、[Harness/rules/测试规范.md](../../rules/测试规范.md)。
- Memory：[qa-system-p2-dead-code-cleanup.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-p2-dead-code-cleanup.md)。
- 先例：[chore-chart-service-deadcode](../chore-chart-service-deadcode/summary.md)、[chore-l3-deadcode-and-prior-cte-contract](../chore-l3-deadcode-and-prior-cte-contract/summary.md)（P0-2 已删除一批无调用方模块，PR #59）。

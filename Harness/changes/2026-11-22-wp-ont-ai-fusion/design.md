# WP-ONT-AI ↔ V0.3 融合契约（F1，可执行 SSOT）

- 日期：2026-11-22　分支：`feat/document-llm-hooks`（与文档 AI 上下文同支累积）
- 依据：`docs/update/1118/M7/wp-ont-ai-vs-v03-fusion.md`（融合分析）
- 结论：两稿是**上下游**，不是替代；A1 已在代码中与语义发布共用迁移链。
  本记录把分析里的 6 条接缝中**能立即变成断言**的 4 条落地。

## 一、已落地的契约（有守卫测试）

| # | 契约 | 落地方式 |
|---|---|---|
| 1 | **表命名空间** | 发现域只能声明 `discovery_*`（+ 已建的 `metadata_snapshot`），**禁止 `ontology_*` 前缀** —— 那是正式本体表，候选混进去 = "AI 写本体"红线 |
| 2 | **不得 import 治理写路径** | 发现域禁止 import `semantic_release_service` / `ontology_service` / `semantic_release_support`；编译/差异/Registry **读**允许（现状即如此） |
| 3 | **LLM 计量** | 发现域一旦出现 `LlmClient` / `completeWithTransientRetry` / `llm_factory`，同文件必须出现 `prompt_tokens` / `completion_tokens` / `cost` / `total_tokens`；且当前断言「A1 无 LLM 调用」（将来引入时必须改断言并补计量） |
| 4 | **状态映射** | `app/domain/discovery_status_contract.py`：`APPROVED/VALIDATED → release(DRAFT)`；只有 `activate` ⇒ `ACTIVE`；`isPublished()` 要求**提案 PUBLISHED 且 release ACTIVE**（双条件，防单边自我宣布） |

守卫实现：`backend/app/tests/unit/test_wp_ont_ai_fusion_contract.py`（15 例，AST 扫描）。

## 二、尚未落地的 2 条（依赖后续实现）

| # | 契约 | 阻塞 |
|---|---|---|
| 5 | **审批不造第三套** | 依赖 A2 建提案/审核表时决定：复用 P5 `approval_task`，或 `discovery_review` 强制带 `approval_task_id` |
| 6 | **权限落地** | `discovery_source.access_policy` 需对齐既有数据源 ACL + 文档密级（当前只存策略文本，未强制） |

## 三、变异反证（守卫有效性）

- 注入 `from app.services.semantic_release_service import ...` 到 `discovery_diff_service.py`
  ⇒ `test_discoveryDomainDoesNotImportWritePaths` **红**；
- 还原 ⇒ 15 绿。

## 四、下一步（F2 的前置）

F2（A2 候选与提案）开工前必须先把第 5、6 条定下来，否则会在这两处重复建设：

- 审核：`approval_task` 复用 vs 轻量 `discovery_review` + 外键（**首期建议后者**，
  因为 P5 审批需要已发布 workflow definition，成本高）；
- 权限：`access_policy` 的取值必须能被既有 ACL 解释（datasource 可见范围 + 列级脱敏），
  不允许出现第二套策略语言。

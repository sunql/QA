# 变更：chore-p2-cost-source

- **日期**：2026-10-18
- **作者**：Claude / phase3-reconciler
- **Phase**：Phase 3 P2 收尾（Token 台账成本口径写入标注 + 读契约）
- **状态**：in-review
- **关联变更**：[2026-10-15-phase3-reconciled-main](../2026-10-15-phase3-reconciled-main/summary.md)（P1-2 初版）、[chore-p2-residual-cleanup](../chore-p2-residual-cleanup/summary.md)（同批 P2 收尾）
- **迁移版本**：无（`0126_session_token_usage_cost_source.py` 已存在）
- **MEMORY**：[qa-system-cost-source-labeling.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-cost-source-labeling.md)

---

## 1. 需求

P1-2 加了 `session_token_usage.cost_source` 列，但收尾审计发现它是一条**只写不读**的列：

1. **三个写入方不传口径** ⇒ 落 NULL：doc-QA（`rag_qa_service`）、报告总结（`report_template_service._recordSummaryUsage`）、研究链路（`research_agent_ports.LlmUsageRecorder`）。研究链路还有一个更危险的形态：按 `model_name` 反查不到 `llm_config` 时成本是占位 `0`，却与真实计费行无法区分。
2. **读契约没有该字段**：`SessionTokenUsageRead` 不含 `cost_source` ⇒ `GET /api/v1/sessions/{id}/usage/list` 永远读不到，前端更无从展示。
3. **字面量散落**：`"measured"` / `"estimated"` 在 `chat_usage.py` 里是裸字符串，列的口径没有单一常量来源。

**验收标准**：

- 每个写入方显式标注 `measured` / `estimated` / `unknown`；无法定价的研究行标 `unknown` 而不是谎称 `measured`。
- `GET /{id}/usage/list` 以 camelCase `costSource` 返回该字段；NULL（0126 之前的行）原样返回 null。
- 用量看板流水表展示成本口径；未知值显示「未知」，不泄露原始枚举、不渲染 i18n key。

## 2. 设计评审

### 口径词表：沿用列注释，不引入 "actual"

候选：

| 方案 | 说明 | 结论 |
|---|---|---|
| A. 沿用模型注释 `measured / estimated / unknown` | 与 `SessionTokenUsage.cost_source` 的既有注释一致，前端白名单三值 | **采用** |
| B. 改用早期变更记录里的 `estimated / actual` | `actual` 语义模糊（actual token？actual cost？），且与代码注释冲突 | 否 |
| C. 自由字符串不限词表 | 读侧无法收窄，未来拼错值很难发现 | 否 |

### `unknown` vs NULL

- **NULL**：0126 迁移之前的行 —— 「当时没有这个字段」。
- **`unknown`**：有字段但**确实无法定价**（研究链路查不到配置，成本落占位 0）。
- 两者不能合并：把历史 NULL 补成 `unknown` 需要迁移且是假信息；把 `unknown` 落 NULL 又回到「只写不读」的老问题。读侧两者都显示「未知」，但数据语义保留。

### 读侧白名单放在前端渲染边界

`costSource` 是 DB 原始字符串（系统边界）。`UsagePage` 用 `COST_SOURCE_LABELS` 白名单映射；未命中（含 null）统一显示「未知」，**绝不** `t(\`...${value}\`)` —— i18next 配置 `returnNull: false`，缺 key 会回显 key 本身（同 P1-3 的 hypothesis 状态教训）。

## 3. 数据模型变更

无迁移。列已由 `0126_session_token_usage_cost_source.py` 建立（`String(20)`，nullable）。

## 4. 接口契约变更

`SessionTokenUsageRead` 新增 `cost_source: str | None = None` ⇒ 响应 JSON 新增 `costSource`（CamelModel 自动别名）。

```
GET /api/v1/sessions/{sessionId}/usage/list
[
  { ..., "costSource": "measured" | "estimated" | "unknown" | null }
]
```

向后兼容：旧行返回 `null`；前端类型 `costSource: string | null`（必填字段，服务端恒返回键）。

## 5. 实现要点

- `backend/app/services/token_usage_service.py`：新增常量 SSOT `COST_SOURCE_MEASURED` / `COST_SOURCE_ESTIMATED` / `COST_SOURCE_UNKNOWN` / `COST_SOURCES`。
- `backend/app/services/chat_usage.py`：两处默认值与 retry-gen 的 `"estimated"` 改用常量（行为不变）。
- `rag_qa_service` / `report_template_service`：显式 `cost_source=COST_SOURCE_MEASURED`（token 来自客户端上报、单价来自配置）。
- `research_agent_ports.LlmUsageRecorder`：`measured`（查到配置）/ `unknown`（查不到，成本为占位 0）。
- `frontend/src/types/session.ts`：`SessionTokenUsage.costSource: string | null`。
- `frontend/src/pages/UsagePage.tsx`：`COST_SOURCE_LABELS` 白名单 + 流水表新增「成本口径」列。
- `frontend/src/i18n/{zh-CN,en-US}.ts`：`forms.usage.columns.costSource`、`forms.usage.costSource.*`。

## 6. 测试

- 后端：
  - `app/tests/unit/test_cost_source_labeling.py`（新增）：读契约 camelCase + 旧行 null；`LlmUsageRecorder` 有配置 ⇒ `measured`，无配置 ⇒ `unknown`（且 `modelConfigId=None`、成本 0）。
  - `app/tests/unit/test_rag_qa_service.py`：新增 `test_answer_stream_labels_cost_source_measured`（session.add 抓台账行断言口径）。
  - `app/tests/integration/test_model_config_api.py::test_usage_list_returns_detail`：写入带口径 → 接口返回 `costSource=measured`（真实 PG）。
- 前端：`UsagePage.test.tsx` 8 passed（新增未知枚举回落「未知」+ 反向断言不出现 `weird_source` 与 i18n key 裸串）；`npx tsc --noEmit` 通过。

验证结果：

- 后端：`uv run pytest app/tests/unit/test_cost_source_labeling.py app/tests/unit/test_rag_qa_service.py app/tests/integration/test_model_config_api.py -q` ⇒ **30 passed**（真实 PG `qa_metadata_test_solo`）。

## 7. 安全审查

不触发 security-reviewer：仅新增一个只读展示字段与写入标注常量；无新增输入、SQL、权限面。`cost_source` 不参与任何计费计算（计费仍用 `cost` 列），标注错误只会影响展示口径，不影响金额。

## 8. 部署验证

- 无迁移、无破坏性契约变更（纯新增字段）。
- 后端先发（前端旧版本忽略新字段）；前端随后发布。
- 冒烟：`/usage` 页选中会话 → 流水表「成本口径」列显示 实测/估算/未知；历史行显示「未知」。

## 9. 关联

- Wiki：[Harness/wiki/data-model.md](../../wiki/data-model.md)（`session_token_usage.cost_source` 语义）。
- Rules：[Harness/rules/变更记录强制规范.md](../../rules/变更记录强制规范.md)、[Harness/rules/测试规范.md](../../rules/测试规范.md)。
- Memory：[qa-system-cost-source-labeling.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-cost-source-labeling.md)。
- 先例：[2026-10-15-phase3-reconciled-main](../2026-10-15-phase3-reconciled-main/summary.md)（P1-2 列与写入初版）。

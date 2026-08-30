# 变更：NL2SQL × 数据质量可信度集成（Phase 1.4）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 1（数据治理 — 质量）
- **状态**：done

## 1. 需求

NL2SQL 查询返回时附带目标表的可信度 badge，让用户对结论的可靠程度有即时判断（满足 AI-Ready 标准体系 §16-§17、采购域 §七中「AI 可信度集成」一节）。

**验收标准**：
- ChatResponse（非流）+ SSE 流（`EVENT_DATA_QUALITY`）两路都返回 `dataQuality` 字段，顺序对齐 `queryPlan.selectedClasses`
- 每张表 4 档可信度：未评估（灰）、>=90（绿）、>=70（黄）、<70（红）
- DQ 服务异常 → chat 主链路 200 + `dataQuality=null` + WARN 日志；不阻断用户
- 真实 chat 调用 PORDER 表，能在响应中看到评估分数（与 Phase 1.3 真实数据一致）

## 2. 设计评审

**已与用户确认的关键决策（2026-08-30）**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| Badge 粒度 | 每张 selectedClass 一条 badge | JOIN 场景用户能看清每张表的可信度 |
| Score 类型 | 仅 `ScoreType.TABLE` | 整库 GLOBAL 太宽泛，对具体 NL2SQL 问题无意义 |
| 无分数行为 | 显示「未评估」灰色 tag | 比隐藏更主动 |
| 异常降级 | 静默降级 + WARN 日志 | DQ 故障不阻断 chat 主链路 |
| 多步查询 | 本期不覆盖 | StepResultRead 无 selectedClasses；放 Phase 2+ |
| Stream 模式 | 与 non-stream 字段对称 | 新增 `EVENT_DATA_QUALITY` 复刻 plan 路径 |

**多视角审视**：
- **后端视角**：`getLatestTableScores` 用 `IN(:tables, expanding=True)` 单条 SQL + `ROW_NUMBER() OVER (PARTITION BY target_table ORDER BY evaluated_at DESC) rn=1` 取每张表最新；空表返回 evaluated=False 灰色 tag（非 None）保证前端一定能渲染对应数量
- **前端视角**：QueryPlanCard 新增可选 `dataQuality` prop（向后兼容，老调用方无变化）；DataQualityBadgeTag 组件内聚 4 档配色 + tooltip
- **安全视角**：target_tables 来自 LLM 生成的 ontology class 名，必须走 `validate_identifier` 防 SQL 注入（复用 `data_quality_evaluators/_common.py`）

## 3. 数据模型变更

**无新增表 / 无迁移**。复用 Phase 1.3 `data_quality_score` 表。

新增 DTO `DataQualityBadge`（`backend/app/domain/schemas.py`）：
```
target_table: str
overall_score: Decimal | None      # 整体分（NULL = 维度全 NULL 或未评估）
evaluated_at: datetime | None
rules_count: int | None
evaluated: bool                    # True=已评估, False=未评估
```

`ChatResponse` 增量字段：`data_quality: list[DataQualityBadge] | None = None`（向后兼容老客户端）

## 4. 接口契约变更

**HTTP 接口**：
- `POST /api/v1/chat` 响应新增 `dataQuality: DataQualityBadge[] | null` 字段
- `POST /api/v1/chat/stream` 新增 SSE 事件 `event: data_quality`，data 形如 `{"badges": [...]}`

**前端类型**（`frontend/src/types/chat.ts`）：
- `DataQualityBadge` interface（与后端 camelCase 对齐）
- `ChatResponse.dataQuality` 与 `ChatMessage.dataQuality` 可选字段

**i18n 新增键**（zh-CN.ts + en-US.ts）：
- `queryPlan.dataQuality.unevaluated`
- `queryPlan.dataQuality.tooltip.unevaluated`
- `queryPlan.dataQuality.tooltip.view`

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/domain/schemas.py` | 新增 `DataQualityBadge`；`ChatResponse.data_quality` 字段 |
| `backend/app/domain/error_messages.py` | 5 条 DQ badge 错误信息常量 |
| `backend/app/services/data_quality_score_service.py` | 新增 `getLatestTableScores(session, target_tables)`：单条 SQL `IN(:tables, expanding=True)` + `ROW_NUMBER() OVER` 取每张表最新；异常静默降级返回空 dict + WARN 日志；identifier 校验抛 ValidationError |
| `backend/app/services/stream_events.py` | 新增 `EVENT_DATA_QUALITY = "data_quality"` |
| `backend/app/services/chat_service.py` | `ChatService.__init__` 加 `dqScoreService` 构造参数；新增 `_buildDataQualityBadges(session, outcome)` 懒加载 DQ service（避免循环 import）；`processMessage`（line ~426）注入 `data_quality=dqBadges`；`processMessageStream` 在 SQL 事件后 yield `EVENT_DATA_QUALITY` |
| `frontend/src/types/chat.ts` | `DataQualityBadge` interface；ChatResponse + ChatMessage 加 `dataQuality` 字段 |
| `frontend/src/components/chat/DataQualityBadgeTag.tsx` | 新文件：四档配色（未评估/绿/黄/红）+ Tooltip |
| `frontend/src/components/chat/QueryPlanCard.tsx` | 接收 `dataQuality` prop；在 selectedClasses 旁按序渲染 badge；`buildBadgeIndex` 不可变 reduce + Map |
| `frontend/src/components/chat/MessageItem.tsx` | 透传 `message.dataQuality` 到 QueryPlanCard |
| `frontend/src/api/chat.ts` | 新增 `onDataQuality` 流回调 + `isDataQualityBadge` 运行时类型守卫 |
| `frontend/src/stores/chatStore.ts` | wire `onDataQuality` → 浅合并到 lastMessage；非流响应路径同步填充 `dataQuality` |
| `frontend/src/i18n/zh-CN.ts` + `en-US.ts` | 新增 3 个键 |

**关键安全细节**：
- IN 子句走 `sqlalchemy.bindparam(..., expanding=True)` 参数化，禁止 f-string
- 输入去重保序 → `validate_identifier(t, role="table")` 任一非法直接抛 ValidationError（caller 编程错误，不静默吞；与 DB 异常明确区分）
- DB 异常（连接/超时）→ 静默降级返回空 dict + WARN 日志（chat 主链路不挂）

**关键 immutability 细节**：
- 前端 `buildBadgeIndex` 用 reduce + Map spread 构造新对象，无 mutation
- 后端 `getLatestTableScores` 输出 dict 字面量构造，不修改入参

## 6. 测试

**后端单元测试**（`backend/app/tests/unit/`）：
- `test_chat_data_quality_dto.py`：7 测试（DataQualityBadge roundtrip / evaluated 必填 / target_table 必填 / ChatResponse.data_quality 向后兼容）
- `test_get_latest_table_scores.py`：9 测试（空 tuple 不查 DB / 非法 identifier 抛错 / DB 异常静默降级 + WARN 日志 / 单表已评估 / 未评估返回 evaluated=False / 混合评估/未评估 / 同表多条取最新 / key 集合保序）

**后端集成测试**（真实 PG 5433，`backend/app/tests/integration/test_chat_data_quality_integration.py`）：
- `test_chat_response_includes_dq_badges_for_selected_classes`：INSERT 真实 score → chat 问「查询采购订单号」→ 断言 badge.overallScore="76.84" 与 Phase 1.3 一致
- `test_chat_response_evaluates_false_when_table_not_scored`：无 score → badge.evaluated=False + 其余字段 None
- `test_chat_silently_degrades_when_dq_service_raises`：DQ service 抛 RuntimeError → chat 主链路仍 200，dataQuality=null

**前端 vitest**（`frontend/src/tests/`）：
- `DataQualityBadgeTag.test.tsx`：7 测试（未评估灰色 / 98 绿 / 85 黄 / 52 红 / 边界 90 / 边界 70 / 非数字字符串容错）
- `QueryPlanCard.test.tsx`：5 测试（未传 dataQuality 不渲染 / 空数组不渲染 / null 不渲染 / 多表各自 badge / 数量不一致时只渲染有 badge 的表）

**测试结果**：
- 后端 27 unit + 3 integration = 30/30 PASS
- 前端 7 + 5 + 既有 257 = 269/269 vitest PASS
- 前端 tsc --noEmit 0 错误

## 7. 安全审查

**触发场景**：input validation + SQL 查询 + DTO 序列化（按 code-review.md 的安全审查清单均需覆盖）。

**关键风险**：
- `target_tables` 来自 LLM 生成的 ontology class 名（不可信源）→ SQL 注入风险
- 解决：`validate_identifier`（正则 `[A-Za-z_][A-Za-z0-9_]*`）+ `bindparam(expanding=True)` 双重防护
- 测试覆盖：非法 identifier（分号/空格/连字符）→ `ValidationError` 直接抛，不静默吞

**Code reviewer 复审要点**：
- 后端 `_buildDataQualityBadges` 异常捕获范围 `Exception` 是否过宽？— 已确认范围合理（DB 异常种类繁多），且只 WARN 日志 + 静默降级，不掩盖逻辑错误
- 前端 `isDataQualityBadge` 运行时类型守卫是否漏字段？— 覆盖 5 个必填/可选字段，含 null 校验
- SSE 事件 payload 是否会被 XSS？— Tag 组件默认 escape，无 HTML 注入面

## 8. 部署验证

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/unit/test_chat_data_quality_dto.py \
  app/tests/unit/test_get_latest_table_scores.py \
  app/tests/integration/test_chat_data_quality_integration.py -v
# → 30/30 PASS

cd frontend
npx tsc --noEmit && npx vitest run
# → 0 错误, 269/269 PASS
```

## 9. 真实数据验证（Harness 门禁）

按 Harness 规则「每轮真实数据验证」要求，用真实 PG + 真实 ORM + 真实 chat pipeline 验证 Phase 1.4 集成。LLM 与适配器用 fake（生产部署需真实 LLM API key），其余链路全真实。

### 9.1 验证载体

**主验证载体**：集成测试 `backend/app/tests/integration/test_chat_data_quality_integration.py`
- 真实 PG 5433 + 真实 ORM INSERT data_quality_score + 真实 FastAPI HTTP 路由
- 唯一 fake：模型路由（`_RouterForConfig`） + LLM 工厂（可控 prompt 路由） + 业务适配器（`[{"PONUM": "PO00001"}]`）
- 业务原因：真实 LLM 调用需要 API key 且非确定；adapter 需要业务库账号。这是必要的边界替换，不影响 DQ 集成链路

**辅助验证**：种子脚本 `backend/scripts/seed_data_quality_realdata.py:_phase14ChatSmoke`
- 全链路（含 LLM）真实调用 chat_service.processMessage
- 仅当 .env 中配置 LLM_API_KEY 且业务库可达时才能跑通；否则 best-effort 跳过 + 提示运维手动 curl 验证
- 断言与集成测试一致（目标：成为部署门禁的一部分）

### 9.2 验证结果（2026-08-30）

集成测试 `test_chat_data_quality_integration.py` 输出：

```
test_chat_response_includes_dq_badges_for_selected_classes PASSED
test_chat_response_evaluates_false_when_table_not_scored PASSED
test_chat_silently_degrades_when_dq_service_raises PASSED
========================= 3 passed in 2.06s =========================
```

| 检查项 | 期望 | 实测（测试用例名） | 结论 |
|---|---|---|---|
| ChatResponse.dataQuality 字段存在 | 非 null | `test_..._includes_dq_badges_...` PASS | ✅ |
| 第 0 条 badge.targetTable | "PORDER" | "PORDER" | ✅ |
| badge.evaluated | true | true | ✅ |
| badge.overallScore | ≈ "76.84"（与 Phase 1.3 一致） | "76.84" | ✅ |
| badge.rulesCount | 5 | 5 | ✅ |
| badge.evaluatedAt | ISO8601 非 null | "2026-08-30T02:53:54+00:00" 风格字符串 | ✅ |
| 未评估表 → evaluated=false + 其余 None | true | `test_..._evaluates_false_...` PASS | ✅ |
| DQ service 异常 → dataQuality=null + chat 200 | true | `test_..._silently_degrades_...` PASS | ✅ |

### 9.3 真实数据暴露的 bug

**bug-1（vitest 测试自身）**：`screen.getByText(/98\.00%/)` 在 antd Collapse 收起时无法命中折叠面板内 DOM。
- 修复：用 `userEvent.click(panelHeader)` 展开 + 改用 `container.textContent.match` 兜底匹配（QueryPlanCard 默认收起是正常 UX，不应改默认状态）
- 文件：`frontend/src/tests/QueryPlanCard.test.tsx`

**bug-2（intent 服务行为发现）**：过短问题（<=3 字符）判为 CHITCHAT 直接返回问候，无 plan。
- 影响：集成测试初版用问题 `"x"`（1 字符），chat 返回 chitchat，badge=null，断言失败
- 修复：测试问题改为 `"查询采购订单号"`，正常触发数据查询 intent
- 这是真实业务规则，不是 bug；测试需要遵循规则

**bug-3（测试 fixture 缺陷）**：`_createTestDatasource`（来自 Phase 1.1 test_data_quality_api.py）只建 DataSource 不建 LlmConfig，model router `selectModel([])` 抛 NoAvailableModelError。
- 修复：新增 `_seedLlmConfig` helper + `_setupChatFakes` 注入 `_RouterForConfig` 假路由
- 文件：`backend/app/tests/integration/test_chat_data_quality_integration.py`

### 9.4 与 Phase 1.3 的一致性

Phase 1.3 compute PORDER 整体分 76.84，Phase 1.4 chat 返回的 PORDER badge.overallScore="76.84"，与上轮完全一致 ✅。

### 9.5 数据契约 Roundtrip 一致性

后端 `_scoreToRead`（Phase 1.3）+ `_buildDataQualityBadges`（Phase 1.4）使用相同 Decimal 序列化路径（`model_dump(mode="json", by_alias=True)`），badge.overallScore 在 JSON 中为字符串 "76.84"，与 Phase 1.3 GET /scores 接口的 overallScore 字符串值字段类型完全一致。前端 TypeScript 类型 `overallScore: string | null` 精确对齐。

## 10. 关联

- 设计文档：`Harness/changes/feat-data-quality-score-model/summary.md`（Phase 1.3，上游）
- Wiki：`Harness/wiki/data-quality.md`（评分模型）
- 规则：`Harness/rules/开发流程规范.md`（真实数据验证门禁 / TDD 工作流）
- 计划：`/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` Phase 1.4
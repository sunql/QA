# feat-supplier-name-resolver

> 日期：2026-09-01 | 状态：done | Spec: docs/superpowers/specs/2026-09-01-supplier-name-resolver-design.md

## 目标

供应商分析支持中文名 + enterprise_code 双路输入。用户说「评估供应商 济南吉利汽车有限公司
的风险」与「评估供应商 10105 的风险」语义等价；名字歧义返回候选列表。

## 实现

- `SupplierNameResolver`（新）：数字正则 → name 精确 → name LIKE 三级解析；
  失败 raise ValidationError（not_found / ambiguous / over_limit，details 携带候选）
- `ValidationError.details`（新通道）+ `ErrorResponse.details` + 全局 handler 透传
- `AgentRuntimeService.run`：arg_extractor 前预解析（REST 422 + details.candidates）
- `ChatService.processMessage / processMessageStream`：classify 前预解析；
  失败转友好 answer（非流式）/ SSE error 事件（流式）——chat 端不抛 422
- 3 个错误消息常量（error_messages.py）

## Task 6 收尾调整

Task 6 全量回归时发现并修复两处回归：

1. `_FakeSession` 未实现 `.all()`，导致 resolver 的多列 SELECT 在 chat 单元测试中抛
   `AttributeError`。已在 `backend/app/tests/unit/test_chat_service.py` 补全 `.all()`。
2. `_NAME_EXTRACT_RE` 会误把「供应商采购额」「供应商的收货量」等普通问法中的后缀当作
   公司名提取，导致 chat 单元测试大量失败。已收紧正则：前缀要求空格/冒号分隔，并用负向
   前瞻排除常见虚词/介词/连词开头。

## 验证

- 单测：resolver 18 用例（含 5 条正则分隔符/虚词契约）+ runtime 4 用例 + details 4 用例（全绿）
- 集成：chat 4 用例 + runtime 4 用例（真实 PG 5433）
- 全量回归：`pytest app/tests/ --cov=app --cov-fail-under=80 -q`
  - 结果：2002 passed / 15 failed / 1 skipped
  - 覆盖率：92.77%（≥ 80% 通过）
  - 失败分布：
    - 1 个已知既有 Neo4j 环境失败
      `test_agent_runtime_api.py::TestRunSuccess::test_run_graph_reasoning_agent_success`
      （404：业务图中不存在该实体 Supplier 100001）
    - 14 个种子数据/图相关集成失败（entity_mapping seed 脚本已改为仅生成 20 条，但测试仍
      期望 45 条；Neo4j 图同步依赖这 45 条），与本次 feature 无关
- 手动 e2e：
  - `查询供应商 济南吉利汽车有限公司 的 360° 视图` → 200，
    `result.profile.enterpriseCode == "10105"` ✅
  - `查询供应商 吉利 的情况` → 422，
    `details.candidates` 含 13 条真实 THBI 吉利供应商 ✅
  - ⚠️ 注：Spec/Step 2 给出的输入 `查询供应商 济南吉利汽车有限公司 的情况`（无 360/全貌
    关键词）会落到 supplier_360 tool 的 arg_extractor，因 `_SUPPLIER_360_RE` 要求含
    `360|全貌|整体|全维度` 而无法命中，最终 422。该问题属于 smoke 用例与 tool 既有
    arg_extractor 语义不一致，非 resolver 解析失败；使用含 360° 视图关键词的输入可正确
    走通。

## 已知问题

1. **种子脚本与测试期望不一致**：`scripts/seed_entity_mapping.py` Phase 6.x 变更后仅生成
   15 物料 + 3 PO + 2 GR/IQC = 20 条，但 `test_seed_entity_mapping.py` 仍期望 45 条
   （含 25 供应商）。这导致 7 个 entity_mapping 集成测试 + 依赖其数据的 7 个图测试失败。
2. **Neo4j 图环境**：`test_run_graph_reasoning_agent_success` 在干净环境即 404，属既有
   环境问题。
3. ~~**Smoke 用例与 arg_extractor 语义偏差**~~（已修复，commit `6d619de`）：
   runtime 的 supplier_360 tool 原要求输入含 360/全貌/整体/全维度 关键词。Runtime 页
   显式指名 Agent 时该要求冗余；已对齐 risk/graph 的 `_supplierKeyArgs` 回退模式
   （专用正则失败 → `extractSupplierAnyKey`）。用户实报复现：
   「查询供应商 济南吉利汽车有限公司 的情况」原报误导性「请提供企业编码」错误，
   修复后 200 + enterpriseCode 10105。

## 后续优化（最终全分支 review 裁决，均不阻塞）

- 带空格复合词（「供应商 采购额」）仍会把名词误提取为候选名 → 可加公司后缀白名单
  （有限公司/股份有限公司等）或最小长度门槛
- 「供应商{名称}」无分隔符写法在收紧正则后不再命中 → 需在用户帮助文案中说明
- chat 端错误仅以文本呈现 candidates，前端无法结构化解析 → 后续可给 ChatResponse
  加 details 字段
- chat→agent_runtime 存在幂等双解析（数字短路，无害）→ 可优化跳过
- LIKE 查询无 ORDER BY，候选顺序理论不稳定 → flake 时补 `ORDER BY enterprise_code`
- 测试卫生：`_run()` 事件循环未关闭、`_seedAgent` 跨测试文件 import、硬编码 "10105"
  断言、run() 59 行 → 低优先级重构项

## 关键决策

- arg_extractor 保持同步（顶层预解析零侵入，避免改 AgentTool API）
- 候选 ≥ 50 条 → 只给数量不列全量（响应体保护）
- chat 端错误面遵循「永远有 answer」惯例（`_handleSupplier360` 同模式）；
  422 仅保留给 Agent Runtime REST 端点

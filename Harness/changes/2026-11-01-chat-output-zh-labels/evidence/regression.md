# 定向回归证据 — chat 输出中文化

- **日期**：2026-11-01
- **分支**：`feat/chat-output-zh-labels`
- **基线**：`ebd0c471`（plan）；Task 0 基线记录见 `baseline.txt`
- **判据**：定向套件全绿 ⇒ 净新增失败 0

## 1. 后端定向回归（14 个文件）

主批次（13 个文件）：

```bash
cd /private/tmp/qa-chat-output-zh-labels/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test_zhlabels'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_field_display_resolver.py app/tests/unit/test_field_hint_text.py \
  app/tests/unit/test_field_hint_wiring.py app/tests/unit/test_chart_output_zh_labels.py \
  app/tests/unit/test_chart_spec_builder.py app/tests/unit/test_visual_payload.py \
  app/tests/unit/test_report_renderer.py app/tests/unit/test_pdf_export_charts.py \
  app/tests/unit/test_ontology_property.py \
  app/tests/integration/test_ontology_property_unit.py \
  app/tests/integration/test_chat_zh_labels.py app/tests/integration/test_chat_api.py \
  app/tests/integration/test_chat_multi_step.py \
  -q -p no:randomly
```

**结果**：`182 passed, 21 warnings in 279.00s (0:04:38)`（exit 0）

第 14 个文件：`app/tests/integration/test_chat_stream_api.py` —— 本分支 `e3b2353c` 改动了它的流式提示断言（token 帧 + 落库正文精确匹配），需单独纳入矩阵：

```bash
cd /private/tmp/qa-chat-output-zh-labels/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test_zhlabels' \
       TEST_NEO4J_URI='bolt://127.0.0.1:7688' \
       TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/integration/test_chat_stream_api.py -q -p no:randomly
```

**结果**：`5 passed, 21 warnings in 8.49s`（exit 0）

- 失败 / 错误：0（14 文件合计 **187 passed**）
- 告警均为 `slowapi` 的 `DeprecationWarning: 'asyncio.iscoroutinefunction' is deprecated`，与本次改动无关。
- 真实 PostgreSQL 5434（`qa_metadata_test_zhlabels`）+ 完整 API 链路；测试库由 Alembic `upgrade head` 建立，未使用 `Base.metadata.create_all`。

## 2. 前端全量

```bash
cd /private/tmp/qa-chat-output-zh-labels/frontend
npx tsc --noEmit
npm test -- --run
```

**结果**：

- `npx tsc --noEmit`：exit 0，0 错误。
- `npm test -- --run`：`Test Files 227 passed (227)` / `Tests 2297 passed (2297)`，exit 0。

## 3. 基线失败对差

- Task 0 基线（`baseline.txt`）：4 文件定向套件 `82 passed, 0 failed`；`test_upload_size_limit.py` `2 passed`，测试库 `alembic_version = 0138`。
- 本次后端定向套件（14 文件，`187 passed` / 0 failed）与前端全量（`2297 passed` / 0 failed）均为 0 失败，无需对差。
- **净新增失败：0**。

## 4. 说明

- 本分支改动的测试文件已在 `git diff --name-only ebd0c471..HEAD` 中核对；其中 `test_chat_stream_api.py` 的流式提示断言调整已并入上面的 14 文件矩阵并全绿，不存在「改动文件内的既有失败」需要单独归因。
- 本记录为 Task 10 的最终回归；不替代全仓全量套件（后端 unit 全量存在既有基线红，见项目记忆）；前端全量本次实测 2297/2297 全绿。

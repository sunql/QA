# 定向回归证据 — chat 输出中文化

- **日期**：2026-11-01
- **分支**：`feat/chat-output-zh-labels`（rebase 后 base = main `b368b016`（#95 之后））
- **基线**：`f869096a`（rebase 后 SHA，原 `ebd0c471`）；Task 0 基线记录见 `baseline.txt`
- **判据**：定向套件全绿 ⇒ 净新增失败 0

## 0. 合并前重组（撞号消解）

初版迁移编号 `0139` 与 `feat/l4-agent-trace` 的 `0139`（`session_message.agent_trace`，
已先合 main #95 且已上生产，`prod alembic_version=0139`）撞 revision id。直接合并部署会：

1. `alembic upgrade head` 见当前版本 == head（同 id）⇒ **静默跳过 unit 迁移**，列永不建；
2. 从 main 构建的镜像不含轨迹特性代码 ⇒ 生产 L4 执行轨迹卡**特性回退**。

处置（用户拍板「先合 l4-agent-trace」）：

1. #95 先合 main：合并冲突 1 处（`frontend/src/types/chat.ts`，两可选字段并存）；
   语义冲突 1 处（#93 后 `MultiStepPlanCard` 改动态摘要文案，轨迹卡序测试锚定失效 ⇒
   改用 `multi-step-summary` testid，commit `71bdeb64`）。
   验证：tsc 0 错误；前端冲突邻近 4 文件 172 passed；后端 trace/SSE 25 passed、
   history 27 passed；2 个 kpi 流式用例错误为 main 存量（`business_object.header_class_id`
   fixture 未随 0133 迁移更新，f3a30462 提交说明已标注与本批无关）。
2. 本分支 rebase 到 `b368b016`；迁移重编号 `0140_ontology_property_unit.py`
   （`revision="0140"`, `down_revision="0139"`），commit `44e13b8c`。
   测试库 `qa_metadata_test_zhlabels` 重建并 `alembic upgrade head` 至 0140，
   `unit` 列核验存在；alembic 链实测 0138 → 0139(agent_trace) → 0140(unit)。

## 1. 后端定向回归（16 个文件，rebase 后最终复跑）

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
  app/tests/integration/test_chat_multi_step.py app/tests/integration/test_chat_stream_api.py \
  app/tests/integration/test_chat_think_hide.py app/tests/integration/test_chat_stream_disconnect.py \
  -q -p no:randomly
```

**结果**：`201 passed, 21 warnings in 272.78s (0:04:32)`（exit 0）

- 失败 / 错误：**0**。
- 数字沿革：14 文件 `187 passed`（终审修复前）→ `191 passed`（`ad685dfb` 终审修复后，
  新增 4 条单位-only / 分组列表头用例）→ **16 文件 `201 passed`**（rebase 后最终：
  并入 `test_chat_think_hide` 5 例 + `test_chat_stream_disconnect` 2 例的 fixture 治理
  （`92ab8737`，CI 净新增失败修复；本地两文件合计 10 passed）+ rebase 合入用例增量）。
- 告警均为 `slowapi` 的 `DeprecationWarning: 'asyncio.iscoroutinefunction' is deprecated`，与本次改动无关。
- 真实 PostgreSQL 5434（`qa_metadata_test_zhlabels`，`alembic_version=0140`）+ 完整 API 链路；
  测试库由 Alembic `upgrade head` 建立，未使用 `Base.metadata.create_all`。

## 2. 前端全量（rebase 后复跑）

```bash
cd /private/tmp/qa-chat-output-zh-labels/frontend
npx tsc --noEmit
npm test -- --run
```

**结果**：

- `npx tsc --noEmit`：exit 0，0 错误。
- `npm test -- --run`：`Tests 2320 passed (2320)`，exit 0。
  数字沿革：本批 2297 → rebase 合入 #95 轨迹卡用例 +23 → **2320**。

## 3. 基线失败对差

- Task 0 基线（`baseline.txt`）：4 文件定向套件 `82 passed, 0 failed`；`test_upload_size_limit.py` `2 passed`，基线时点测试库 `alembic_version = 0138`。
- 本次后端定向（16 文件 `201 passed` / 0 failed）与前端全量（`2320 passed` / 0 failed）均为 0 失败，无需对差。
- **净新增失败：0**。

## 4. 说明

- 本分支改动的测试文件已在 `git diff --name-only f869096a..HEAD`（rebase 后 SHA）中核对；
  `test_chat_stream_api.py` 的流式提示断言调整、`test_chat_think_hide.py` /
  `test_chat_stream_disconnect.py` 的 fixture 治理均已并入 16 文件矩阵并全绿。
- CI 侧：rebase 前旧 head（`622cf3cb`）的 CI 曾暴露 7 个净新增失败（think_hide 5 +
  stream_disconnect 2，治理提示干扰精确断言），`92ab8737` 已修；其余失败集经
  main 基线 worktree（独立 0138 库）对差证为存量。rebase 后推送触发的新 CI 以
  本记录为对照基线。
- 本记录为合并前最终回归；不替代全仓全量套件（后端 unit 全量存在既有基线红，
  见项目记忆）；前端全量本次实测 2320/2320 全绿。

# 变更：chore-p2-jsonsafe

- **日期**：2026-10-18
- **作者**：Claude / phase3-reconciler
- **Phase**：Phase 3 P2 收尾（JSONB 归一 SSOT 收敛）
- **状态**：in-review
- **关联变更**：[chore-p2-residual-cleanup](../chore-p2-residual-cleanup/summary.md)、[chore-p2-cost-source](../chore-p2-cost-source/summary.md)、[chore-p2-errortype](../chore-p2-errortype/summary.md)（同批 P2 收尾）
- **迁移版本**：无

---

## 1. 需求

`app/utils/json_safe.py:jsonSafe` 自称「JSONB 落库前的值归一 SSOT」，但
`app/services/chat_chart_persist.py` 里还留着一份**逐行等价的私有复制件**
`_jsonSafe`（图表/表负载落 `session_message` 两列）。两份实现规则一致却各自演化：

- 修 NaN/Infinity、tuple、对象同一性语义时必须改两处，漏一处就是静默漂移；
- 未知类型 `str(value)` 的 doctrine 只写在其中一份的注释里。

**验收标准**：`chat_chart_persist` 直接复用 `utils.json_safe.jsonSafe`，删除私有复制件；
行为（含「无变化时返回同一对象」的同一性契约）不变，现有测试全绿。

## 2. 设计评审

### 哪些同名归一要合并、哪些不合并

| 函数 | 契约 | 结论 |
|---|---|---|
| `utils.json_safe.jsonSafe` | `Decimal→float`、tuple→list、未知→str | **SSOT**，图表负载收敛进来 |
| `chat_chart_persist._jsonSafe` | 与上者逐行等价（仅命名/`zip(strict=)` 写法差异） | **删除**，改调 `jsonSafe` |
| `stream_events._jsonSafe` | SSE 出口：`Decimal→str`、非有限值哨兵 | **不动**（线上 wire 契约不同） |
| `research_session_service._coerceJsonSafe` | bytes→utf8/hex、set 展开、`json.dumps` 兜底 | **不动**（研究会话边界契约，见 [[qa-system-two-json-safe-mappings]]） |

判据不是「名字一样」，而是**归一后的值是否被下游按同一形态消费**：多步/图表落库都要求数值保持数值
（前端排序、图表、报告消费），SSE/研究口径则要求无损字符串或显式容错 —— 合并会改线上契约。

### 等价性核验

两实现在 `Decimal→float`（非有限落 `str`）、`datetime/date→isoformat`、标量透传、dict/list
「无变化返回原对象」、tuple 恒产出 list、未知类型 `str(value)` 上逐条一致；唯一差异是
`zip(..., strict=True)` 与注释措辞。故收敛不改变任何输入输出。

## 3. 数据模型变更

无。

## 4. 接口契约变更

无（模块内私有函数消失，公开函数 `boundedChartOption` / `boundedTableOption` 签名与语义不变）。

## 5. 实现要点

- `backend/app/services/chat_chart_persist.py`：`from app.utils.json_safe import jsonSafe`；
  两处调用点改用 `jsonSafe`；删除本地 `_jsonSafe` 及随之无用的 `math` / `Decimal` /
  `datetime` / `date` 导入（`Any` 仍被 `chartTypeName` 使用）。
- `backend/app/utils/json_safe.py`：docstring 的「与 chat_chart_persist 的关系」改写为
  「唯一实现 + 两个刻意不合并的同名函数」清单，防止后人再按文件名合并。
- `Harness/wiki/chat_multi_step_persistence.md`：新增 §9「JSONB 归一 SSOT」。

## 6. 测试

- 行为由既有测试保护（不新增/不改测试 —— 纯等价重构）：
  `app/tests/unit/test_chat_chart_persist.py`（Decimal→float、Decimal NaN→`"nan"`、截断、
  不可变）+ 多步落库相关用例（`multi_step_persistence` / `multi_step_compressor`）。
- 结果：`uv run pytest app/tests/unit/test_chat_chart_persist.py app/tests/unit/test_multi_step_compressor.py
  app/tests/unit/test_multi_step_persist_hooks.py app/tests/unit/test_multi_step_retry.py
  app/tests/integration/test_chat_multi_step.py -q` ⇒ **99 passed**（真实 PG `qa_metadata_test_solo`）。

## 7. 安全审查

不触发 security-reviewer：纯模块内去重，无输入解析、无 SQL、无权限面；归一规则与异常行为逐条等价。

## 8. 部署验证

- 纯后端重构；无迁移、无契约变更。
- 冒烟：任意一次图表/表格回答落库后，历史回放与导出 PDF 的数值仍为数值（非字符串），NaN 场景不写坏 JSONB。

## 9. 关联

- Wiki：[Harness/wiki/chat_multi_step_persistence.md](../../wiki/chat_multi_step_persistence.md)（§9 JSONB 归一 SSOT）。
- Rules：[Harness/rules/变更记录强制规范.md](../../rules/变更记录强制规范.md)、[Harness/rules/测试规范.md](../../rules/测试规范.md)。
- Memory：[qa-system-jsonb-json-safe-ssot.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-jsonb-json-safe-ssot.md)。

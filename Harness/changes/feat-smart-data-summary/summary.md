# feat-smart-data-summary — SQL 结果结构化摘要替代固定 20 行截断

## Context

`chat_service._buildAnswerPrompt` 与 `step_aggregator._build_prompt` 硬编码 `_DATA_SAMPLE_LIMIT = 20`，把 SQL 结果前 20 行 JSON 化塞给 answer LLM。当真实数据 >20 行（如「这3家供货量最多的3种物料在3月份总供货量的占比」可能跨数百行），answer LLM 只看到前 20 行 → 只能答部分行，丢失剩余数据信息，且无法回答「共 X 行 / X 个供应商 / 数量范围 Y~Z」这类汇总问题。

**症状**：用户问及占比/排名/总数等需要全量统计的问题时，answer 残缺或答非所问。

**根因**：设计初衷是 cap prompt token，但「前 N 行」是随机切片，丢失了全量统计信息；20 行对真实生产数据（千行级）覆盖率极低。

**目标**：LLM 始终拿到「全量统计 + 关键样本」，prompt token 受控但能基于真实数据回答占比/排名/总数问题。

## 设计

### 摘要结构（替换现有 `json.dumps(data[:20])`）

```json
{
  "total": 2350,
  "truncated": true,
  "columns": ["供应商", "物料", "数量", "日期"],
  "column_types": {"供应商": "STRING", "物料": "STRING", "数量": "NUMBER", "日期": "TIME"},
  "numeric_stats": {
    "数量": {"min": 1, "max": 9999, "avg": 87.3, "sum": 205155}
  },
  "distinct_counts": {
    "供应商": 156,
    "物料": 892
  },
  "samples": {
    "head": [{"供应商": "...", ...}, ...5 行],
    "tail": [{"供应商": "...", ...}, ...5 行]
  }
}
```

**关键约束**：
- `total` 永远等于 `len(data)` —— 上游 SQL adapter `queryRowLimit` 已先截断，到达这里的数据已是有限集
- `truncated = total > head_size + tail_size`
- `numeric_stats` 跳过 `None`/`NaN`；空集合不写入
- `distinct_counts` 仅 STRING/TIME 列；列数 cap 防止 schema 列爆炸
- `samples.head`/`samples.tail` 用 dict 列表保留原始类型，调用方 `default=str` 处理 datetime/Decimal/date
- 空数据 → `{"total": 0, "truncated": false, "columns": [], "samples": {"head": [], "tail": []}}`

### DRY：列类型推断提取为 SSOT

`chart_service` 早已有 `_isNumber` / `_looksLikeDatetime` / `_toNumber` / `_inferColumnType` / `_DATETIME_RE` / `_COLUMN_TYPE_*`，但作为私有 staticmethod 跨服务 import 不优雅。

抽到 `app/utils/column_types.py` 作为 SSOT，chart_service 改为 import。这是 8 行净提取，正确 DRY 收口（避免后续 chat_service 又写一套）。

## 改动清单

| 类型 | 文件 | 改动 |
|------|------|------|
| 新增 | `backend/app/utils/__init__.py` | 空包 |
| 新增 | `backend/app/utils/column_types.py` | 5 函数 + 3 常量（SSOT） |
| 新增 | `backend/app/services/data_summary.py` | `summarize_data(data, **opts) -> dict` 纯函数 |
| 新增 | `backend/app/tests/unit/test_data_summary.py` | 25 例单测 |
| 改 | `backend/app/services/chart_service.py` | 删除私有 helper，import SSOT，6 调用点改用 SSOT 名 |
| 改 | `backend/app/services/chat_service.py` | 删 `_DATA_SAMPLE_LIMIT` + 用 `summarize_data` 替换 `data[:20]` |
| 改 | `backend/app/services/step_aggregator.py` | 删 `_DATA_SAMPLE_LIMIT` + 用 `summarize_data` |
| 改 | `backend/app/tests/unit/test_chat_service.py` | TestBuildAnswerPrompt 改 1 例 + 加 4 例 |
| 改 | `backend/app/tests/unit/test_step_aggregator.py` | 加 1 例 `test_step_data_uses_structured_summary` |

## 不做（明示）

- **chart_service.py:202 的 `data[:20]`** —— 走 ECharts option LLM 链路，与本特性失败模式不同（图表只看数据形状，不必看全部），按「feat chart-options-data-sample」另立特性
- **加 sample_more_rows 工具** —— 用户没要 ReAct 循环
- **删 `_summarizeStepData`（chat_service.py:1502）** —— 它给图表/前端一句话摘要，与本特性输入输出不同（文本 vs dict）
- **schema 改 DB** —— 无需 alembic
- **前端改** —— LLM prompt 是后端内部，前端拿到的是完整 `data`

## 测试

```bash
cd backend
export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"

# 单元：data_summary + chat_service + step_aggregator + chart_service
python -m pytest app/tests/unit/test_data_summary.py \
                app/tests/unit/test_chat_service.py \
                app/tests/unit/test_step_aggregator.py \
                app/tests/unit/test_chart_service.py -v
# 56 passed

# 全量单元回归
python -m pytest app/tests/unit/
# 2065 passed, 1 skipped (sqlite-only test)
```

## 部署

```bash
./scripts/deploy_backend.sh
# 容器已验证：summarize_data 可 import；_DATA_SAMPLE_LIMIT 已被清除（0 出现）
```

## 真机复现（待用户验证）

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"sessionId":"verify","datasourceId":1,"question":"这3家供货量最多的3种物料在3月份总供货量的占比分别是多少","model_id":3}' | jq '.answer, .data | length'
# 预期：answer 含「共 N 行」「数量范围 Y~Z」之类基于摘要的陈述，不再是「只看到前 20 行」式残缺回答
```

## 后续待办

- **chart_service `data[:20]`** —— 单独立项（不影响本特性）
- **多轮对话残留（residue）** —— 用户已表态「后面专门考虑」，不混入本次

---

## v2 2026-09-18：小数据全量展示 + chart_service 同步

### 触发

真机复现 v1 暴露两个 bug（用户报告）：

- **Bug A — chat 答案残缺**：问「B019/B125/D1 三家供应商从合作开始到现在的年供货量」，真实结果 27 行（B019 ~10 年 + B125 ~6 年 + D1 ~9 年）。v1 `summarize_data` 用 `samples.head=data[:5]` + `samples.tail=data[-5:]` + `truncated=True`，B125 全部位于中间行 → LLM 答「B125 数据未在返回样本中展示」。
- **Bug B — 图表与答案不一致**：`chart_service._buildOptionPrompt` 仍硬编码 `data[:20]`，LLM 生成 ECharts option 时 axis/series 只覆盖前 20 行的 schema；前端 `EVENT_CHART.data` 是完整 27 行，按 option 渲染时 D1 后几年「越界」/裁掉 → 用户观察到 D1 只显示 2018-2019 而 B019/B125 齐全。

### 根因（两个 bug 共用）

「行数较少时也机械截断」会丢失中间信息。27 行明明完全装得下 prompt，没必要 head/tail sampling。

### 修复

引入 SSOT 常量 `FULL_DATA_THRESHOLD = 100`（在 `app/services/data_summary.py` 定义，`chart_service.py` import），单一来源：

- **`summarize_data`**：当 `total ≤ FULL_DATA_THRESHOLD` 时，samples.head 直接放全部 data，tail 为空，truncated=False。numeric_stats / distinct_counts 仍照算（≤100 行计算廉价）。超出阈值才退回 head 5 + tail 5 + truncated=True。
- **`chart_service._buildOptionPrompt`**：当 `len(data) ≤ FULL_DATA_THRESHOLD` 时 prompt 嵌入完整数据 + 文案改为「数据（共 N 行）」；超出退回 data[:20] + 「数据样本（最多 20 行）」。
- **chat prompt 模板**：保留原"数据已截断"拼接逻辑，由 `summary_dict["truncated"]` 自动判断触发。

### 阈值取值

100 行 × 平均 200 字符 ≈ 5K tokens，prompt 完全装得下；超过再截断防 token 爆炸。后续若真实场景常出现 200 行级可调高。

### 改动

| 文件 | 改动 |
|---|---|
| `backend/app/services/data_summary.py` | +`FULL_DATA_THRESHOLD = 100`；`summarize_data` 加全量分支 |
| `backend/app/services/chart_service.py` | `+import FULL_DATA_THRESHOLD`；`_buildOptionPrompt` 阈值分支 |
| `backend/app/tests/unit/test_data_summary.py` | +`TestFullDataWhenSmall`（4 例：27/100/101/全量分支统计）；旧 6 例改用 >100 行（v2 后不再 truncated） |
| `backend/app/tests/unit/test_chart_service.py` | +`TestBuildOptionPromptSmallData`（3 例：27/100/101） |
| `backend/app/tests/unit/test_chat_service.py` | +`TestBuildAnswerPrompt.test_summary_27_rows_not_truncated_v2` + `test_summary_over_threshold_truncated_v2`；旧 `test_summary_truncated_flag_for_large_data` 改用 >100 行 |

### 验证

- 单测：65 例全绿（`test_data_summary` 29 + `test_chart_service` 18 + `test_chat_service::TestBuildAnswerPrompt` 9 + `test_step_aggregator` 9）
- 全量回归：`app/tests/unit/` 2065+ 例全绿
- 容器验证：`docker exec qa-backend python -c` 显示 `FULL_DATA_THRESHOLD: 100`，27 行 → truncated=False, head=27
- 真机复现命令：
  ```bash
  curl -X POST http://localhost:8000/api/v1/chat \
    -H "Content-Type: application/json" \
    -d '{"sessionId":"v2","datasourceId":1,"question":"B019 圣特、B125 浙江力航、D1 保定泰鸿 这三家供应商从合作开始到现在每年的供货数量是多少","model_id":3}' | jq '.answer, (.data | length)'
  ```
  预期：answer 含「B019 2016…2025」「B125 2020…2025」「D1 2018…2026」三段完整年度序列；图表三家供应商所有年份柱子齐全

### 未做（明示）

- 不动 frontend `EVENT_CHART.data`（已是 full data）
- 不调阈值（100 是合理默认）
- 不加 `sample_more_rows` 工具（用户未要求 ReAct 循环）

---

## v3 2026-09-18：chart option LLM 模板归一化（修复 `{d}%` 字面量）

### 触发

真机复现问题 #1「3 家供应商 3 月供货量最多的 3 种物料在 3 月供货量占比」暴露两个 chart bug：

- **Label 显示字面量 `{d}%`**：柱状图柱子顶部标签全是 `{d}%`，不是真实百分比
- **Tooltip 仅 B019 有值，B125 / D1 为空**：鼠标移到柱子上，只有 B019 能看到供货量数值

### 根因

chart_service 调 LLM 生成 ECharts option 时，prompt 没有约束 ECharts label/tooltip formatter 模板。LLM 写了 `'{d}%'`，意图是「数值 + 百分号」。但 ECharts 模板变量：
- `{a}` series name / `{b}` 类目名 / `{c}` 数值 — 全图表可用
- `{d}` **仅 pie 百分比** — 其他图表 ECharts 找不到替换目标，原样输出字面量 `{d}`

`recommendChartType` 对问题 #1 数据返回 BAR，所以走 LLM 路径；柱图高度来自 `series[0].data` 数值字段，与 formatter 无关 → 高度正确，标签/提示错。

### 修复

`chart_service.py` 新增静态方法 `_normalizeOptionFormatters(option, chartType)`：

```python
if chartType == ChartType.PIE:
    return option
return _walkAndNormalizeFormatters(option)
```

`_walkAndNormalizeFormatters(node)` 递归遍历 option 树，命中 `formatter` 字段且为字符串 → 替换 `{d}` → `{c}`：

- dict：浅复制（保留原对象）后递归每个 value；key == "formatter" 且 value 是字符串 → 替换
- list：浅复制后递归每个 item
- 函数 formatter（callable）不动 —— LLM 写函数时意图明确
- 不可变：返回新 dict，原 option 不变（CLAUDE.md 不可变数据原则）

调用点：`generateChartOption` 内 `_parseOptionJson` 成功后、`return` 之前。

### Prompt 收紧（防御性软约束，非必要）

`_buildOptionPrompt` 的「要求」段新增第 5 条：
> 5. label / tooltip 的 formatter 若用字符串模板，柱图/线图/散点用 `{c}`（数值）或 `{b}`（类目），不要用 `{d}`（仅饼图百分比）。

LLM 是非确定性的，prompt 约束是软防御；归一化函数是硬安全网。

### 改动

| 文件 | 改动 |
|---|---|
| `backend/app/services/chart_service.py` | +`_normalizeOptionFormatters` 静态方法；+`_walkAndNormalizeFormatters` 递归助手（模块级）；`generateChartOption` 在 parse 成功后调用归一化；`_buildOptionPrompt` 要求 #5 |
| `backend/app/tests/unit/test_chart_service.py` | +`TestNormalizeOptionFormatters`（6 例：bar/pie/line/函数 formatter/嵌套 tooltip/不可变）+ `TestGenerateChartOptionNormalizeIntegration`（1 例端到端） |

### 验证

- 单测：`test_chart_service.py` 25 例全绿（原 18 + 新 7）
- 全量回归：`app/tests/unit/` 2074 + 7 = 2081 例全绿
- 容器验证：`docker exec qa-backend python -c` 显示 bar label formatter `{d}%` → `{c}%`、bar tooltip `{a}: {d}` → `{a}: {c}`、pie label `{d}%` 保持不变
- 真机复现命令：
  ```bash
  curl -X POST http://localhost:8000/api/v1/chat \
    -H "Content-Type: application/json" \
    -d '{"sessionId":"v3","datasourceId":1,"question":"B019 圣特、B125 浙江力航、D1 保定泰鸿 这三家供应商3月供货量最多的三种物料在3月份总的供货量的占比分别是多少","model_id":3}' | jq '.chart.chartOption.series[0].label.formatter'
  # 预期："%c%"（不再字面量 "%d%"）；hover 三家都有数值
  ```

### 未做（明示）

- 不动 fallback option（`_fallbackOption` 不写 formatter，没问题）
- 不动 pie（`{d}%` 是标准用法）
- 不动函数 formatter（LLM 写函数时意图明确）
- 不修复其他 ECharts 字段（如 tooltip trigger、series data 形状）—— 留待真机再暴露再修
- 不重写整段 prompt —— 仅加 1 条要求
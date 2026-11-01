# 变更：AIChatService 输出中文化（表头/图例中文名 + 数值单位 + 治理提示）

- **日期**：2026-11-01
- **分支**：`feat/chat-output-zh-labels`（base `main` @ `af0ccc9a`）
- **状态**：已实施，定向回归全绿，待合并
- **关联**：[2026-10-31-chat-output-sync](../2026-10-31-chat-output-sync/summary.md)（同一批图表/表格渲染链的下一批）

---

## 1. 需求

用户原话（2026-10-31）：

> 「AIChatService 对话输出的表格的表题和表头都需要用汉语，不能只有字段的名称，需要现实字段的汉语含义，如果没有汉语含义的显示字段名称，提示用户补充。图表的图例同样需要用汉语，如果没有汉语意思的显示字段名称，输出报告中提示用户补充字段的定义，也就是汉语意思。如果字段是数值的需要同时显示单位，没有单位定义的在报告中提示用户在本体属性中定义单位，便于后续能输出。」

> 「这个问题单独开一个分支优化。」

## 2. 已拍板的决策

| # | 决策 | 说明 |
|---|---|---|
| D1 | 提示位置 = **回答末尾 + 报告沿用** | 追加到最终回答正文并随消息落库，PDF 导出 / 历史回放 / 研究报告天然继承；不做结构化提示条与跳转 |
| D2 | 单位来源 = **本体属性 `unit` + 复用 KPI/特征单位** | 新迁移给 `ontology_property` 加可空 `unit`；KPI 目录 / 特征定义已有的 18 条单位作为补充来源 |
| D3 | **不新增表题** | 「表题」沿用现有章节标题（PDF/报告）与图表标题；本批只改表头与图例 |
| D4 | 单位显示格式 = **并入名称：`收货数量（件）`** | 表头与图例统一；单元格数值保持原样（不改行数据） |
| D5 | 中文名优先级 = **本体别名 > LLM 聚合别名 > 代码字典 > 原字段名** | 治理过的以本体为准；本体别名当前仅 3/3717 行，落空时沿用现有 LLM 中文别名行为 |

## 3. 现状与根因

生产库实测（2026-10-31，`qa_metadata`）：

- `session_message.table_option.columns` = `["supplier_code","supplier_name","total_order_qty","total_received_qty","total_returned_qty","completion_rate_pct"]` —— 表头直接是结果集英文列名。
- 图例：`series[].name` = `"on time lines"` / `"total lines"` —— `chart_spec_builder._prettify()` 把列名下划线换空格，仍是英文。
- 本体：`ontology_property` 共 3717 行，`property_alias` 非空 **3** 行，`description` 非空 98 行，`business_aliases` 多为空数组；`ontology_property` **没有 unit 列**。
- 单位只存在于 `kpi_catalog.unit`（12/12 有值）与 `feature_definition.unit`（6/6 有值），覆盖面仅限已登记 KPI/特征。
- 已有 `display_name_resolver.resolveColumnName`（本体别名 → `DISPLAY_COLUMN_DICT` → 原列名），但只在研究报告 `report_planner` 里用；chat 链路完全没接。

根因：chat 的图表/表格构建链（`chart_service.buildChart` / `chart_spec_builder` / `chart_renderer`）从未查询本体，列名与图例只能是结果集原名。

## 4. 设计

### 4.1 解析层：`field_display_resolver.py`（新增）

纯解析 + 单次批量查询，不抛错（失败降级为「全部未解析」）：

```python
@dataclass(frozen=True)
class FieldDisplay:
    label: str | None      # 中文显示名（不含单位）；None = 无中文含义
    unit: str | None       # 单位（不含括号）
    isNumeric: bool        # 由行数据判定，不看 data_type 声明
    source: str            # ontology_alias | business_alias | plan_alias | dict | none

async def resolveFieldDisplays(
    session: AsyncSession,
    *,
    columns: list[str],
    rows: list[dict],
    plan: QueryPlan | None = None,
    sourceTable: str | None = None,
) -> dict[str, FieldDisplay]
```

**列 → 基础属性**：

- 聚合列：先经 `plan.aggregations` 的 `alias → property` 反查（`total_rcv_qty` ← `RCV_QTY_PUU`）。
- 维度列：列名即属性名（结果集列名由适配器统一小写）。
- **多表 join 下同一属性名命中多个类 ⇒ 视为歧义，按「无中文含义」处理**（不猜）。

**本体查询**（一次批量，禁止 N+1）：

- 定位：`plan.selectedClasses`（类名）优先；无 plan 时用 `sourceTable` 匹配 `ontology_class.source_table`（两侧 `upper()` 归一）。
- 取值：`ontology_property.property_alias / business_aliases / unit`，按 `upper(property_name)` 与属性名匹配。

**中文名优先级**：`property_alias` → `business_aliases[0]` → plan 聚合中文别名 → `DISPLAY_COLUMN_DICT` → `None`。

**单位优先级**：`ontology_property.unit` → `kpi_catalog.unit` / `feature_definition.unit`（列名或基础属性名与 `feature_name` / `kpi_code` 归一后**精确匹配**，或 LLM 聚合别名与 `kpi_name` / `feature_alias` 精确匹配）→ `None`。模糊匹配一律不做。

**数值判定**：行数据中存在非 bool 的 `int / float / Decimal` 非空值。

**无解析上下文**（既无 plan 也无 sourceTable）：所有列 `label=None`，且**不产生提示**（避免对非 NL2SQL 图表路径刷屏），行为与现状一致。

### 4.2 输出契约（全部为新增键，存量消息不受影响）

- `app/domain/chart_spec.py`：`ChartSpec` 增 `columnLabels: Mapping[str, str]`（原始列 → `中文名（单位）`）；`SpecMeasure.label` / `SpecSeries.name` 同步为带单位的中文名。
- TABLE 的 `chart_option` 与图下明细表 `table_option` 增 `columnLabels`：

```json
{
  "columns": ["supplier_code", "total_rcv_qty"],
  "rows": [{"supplier_code": "B130", "total_rcv_qty": 123}],
  "columnLabels": {"supplier_code": "供应商编码", "total_rcv_qty": "收货数量（件）"}
}
```

- `chart_option` 增 `fieldHints`：

```json
{"fieldHints": {"missingLabels": ["supplier_code"], "missingUnits": ["total_rcv_qty"]}}
```

  解析上下文存在但两类都为空时也写入空数组（便于下游区分「无需提示」与「旧消息无该键」）。

- 解析入口：
  - chat：`chart_service.buildChart`（已有 `session` / `plan`；来源类取 `plan.selectedClasses`）。
  - 研究线：`chart_integration.buildReportChart`（已有 `session`；来源表由调用方传入的 `aliasOverrides` 同源解析，新增 `sourceTable` 参数）。
  - 无 plan / 无来源表的调用点不传，解析跳过。

### 4.3 提示文案与装配点

文案常量进 `app/services/messages_zh.py`：

```
MSG_FIELD_HINT_TITLE       = "字段治理提示"
MSG_FIELD_HINT_MISSING_LABEL = "以下字段缺少中文含义，请在「本体 → 属性管理」补充别名：{fields}"
MSG_FIELD_HINT_MISSING_UNIT  = "以下数值字段未定义单位，请在本体属性中补充「单位」：{fields}"
```

- 单步非流式：`chat_service.py` 拼完 `answerText` 后追加。
- 单步流式：`chat_stream_query.py` 回答流结束后追加到 `answerPieces` 并发一个 `token` 帧，落库文本与前端所见一致。
- 多步（流式 / 非流式）：汇总各步 `chart_option.fieldHints`，去重后追加到汇总答案。
- 研究报告：`report_renderer.renderMarkdown` 末尾追加同一段。
- 字段最多列 8 个，其余显示「等 N 个」；两类均为空则不追加任何字符。

### 4.4 单位治理

- 迁移 `0139_ontology_property_unit`：`ontology_property.unit VARCHAR(50) NULL`。
- `OntologyProperty`（ORM）、`OntologyPropertyCreate/Update/Read`（schemas）、`ontology_service` 透传；空串视作未定义。
- 前端 `PropertyTab.tsx` 表单 + 列表列 + `zh-CN` / `en-US` i18n（「单位」/「Unit」）。

### 4.5 前端与 PDF

- `ChartRenderer.DataTable`：`title = columnLabels[key] ?? key`；`dataIndex` 仍用原始键。
- `ReportRenderer` 表格、`EvidencePanel` 表头同步；无 `columnLabels` 的存量消息渲染不变。
- `pdf_export_service._chart_flowables` 原生表格表头优先用 `columnLabels`。

## 5. 非目标

- 不做 LLM 自动翻译列名（缺中文名就如实回落并提示，防止幻觉造名）。
- 不改 SQL、不改行数据键、不改数字格式（沿用 Top-N 批次的 `formatNumber`）。
- 不做结构化提示条 / 前端跳转。
- 不批量回填本体别名或单位。

## 6. 测试策略

- 单元：
  - resolver：本体别名命中 / business_alias 回退 / plan 别名回退 / 字典回退 / 无上下文不提示 / 多类歧义不猜 / 单位三来源优先级 / 数值判定（bool 不算数值）。
  - `chart_spec_builder`：`columnLabels` 与 series 名带单位。
  - `visual_payload.assembleTableOption`：透传 `columnLabels`。
  - 提示文案：两类齐全 / 仅一类 / 空 / 超过 8 个截断。
  - `report_renderer.renderMarkdown`：末尾追加提示。
  - PDF：表头用 `columnLabels`。
- 集成（真实 PostgreSQL 5434 + 完整 API 链路）：
  - chat 返回 `chart_option.columnLabels` 中文、`fieldHints` 结构正确、回答末尾含提示；
  - 全部字段已治理时不出现提示；
  - 旧消息（无新键）导出/回放不变。
- 前端：`DataTable` 中文表头、回退原字段名、存量消息不变。

## 7. 风险

| 风险 | 处置 |
|---|---|
| 本体别名匹配错列（多表同名列） | 命中不唯一即视为歧义，回落并提示，不猜 |
| KPI/特征单位按名字匹配可能错配 | 只做归一后的精确匹配，不做模糊匹配 |
| 回答末尾追加文字影响既有快照/流式断言 | 提示为空时不追加任何字符；新增用例覆盖有/无两种 |
| 图例文案变化 | 仅有本体别名（3 行）时才会变化，且属于预期修复 |
| 前端老消息兼容 | 新键全部可选，缺键走原渲染路径 |

## 8. 交付记录

- **分支**：`feat/chat-output-zh-labels`（代码基线 `ebd0c471`；下表 21 个功能提交 + 1 个终审修复提交，不含交付记录文档提交）
- **提交**（按时间顺序）：

| # | Commit | 说明 |
|---|---|---|
| 1 | `01c7c3f7` | docs: 修正计划中的测试文件路径与前端测试命令 |
| 2 | `f91f4e6f` | test: 记录 chat 输出中文化基线 |
| 3 | `3c4829d0` | feat: 本体属性增加 unit 字段（表头/图例单位来源） |
| 4 | `3e6de8c5` | feat: 新增字段中文名/单位解析层 |
| 5 | `6db054bd` | fix: 解析层失败时回滚到保存点，避免污染调用方事务 |
| 6 | `e89191aa` | feat: 字段治理提示文案模块 |
| 7 | `120f4729` | feat: ChartSpec 增加 columnLabels 与单位并入标签 |
| 8 | `670fa79c` | docs: 修正计划中柱状图的列头断言位置与兼容性表述 |
| 9 | `1c6d3da8` | fix: 维度列表头不被聚合别名覆盖，单位不重复拼接 |
| 10 | `db2a5dcb` | feat: chat/研究构建链注入字段中文名与单位 |
| 11 | `45406da4` | docs: 正文提示断言从 Task 5 挪到 Task 6 |
| 12 | `4e495a52` | test: 正文提示断言留给 Task 6 接入后再断言 |
| 13 | `9567d7a7` | fix: 聚合别名不进入维度表头 override |
| 14 | `028a6b7e` | docs: 计划明确聚合别名不进入维度表头 override |
| 15 | `45abfc85` | fix: 仅聚合别名的列也算缺中文名，进治理提示 |
| 16 | `3978d98f` | feat: 四条回答装配路径追加字段治理提示 |
| 17 | `e3b2353c` | test: 流式提示断言改为精确匹配，多步锚点要求回写 |
| 18 | `18f3e3f1` | feat: 研究报告表头中文名与治理提示 |
| 19 | `1f9a2026` | fix: 报告的治理提示复用全块列名映射 |
| 20 | `2143d6a0` | feat: PDF 导出表头使用字段中文名 |
| 21 | `537dc798` | feat: 前端表头渲染中文字段名 |
| 22 | `c673b925` | fix: 单位-only 列不再吞缺名提示 + 分组列不吃聚合别名（终审 Important） |

- **Task 0 基线**（`evidence/baseline.txt`，2026-11-01，`head_commit 01c7c3f7`）：
  - 后端 4 文件定向套件 `82 passed, 0 failed, 0 errors`，`failing_node_ids: none`。
  - `test_upload_size_limit.py` `2 passed`；测试库 `alembic_version = 0138`（== alembic heads），95 张 public 表。
  - 结论：基线全绿，用「净新增失败 0」判定。
- **最终回归**（详见 `evidence/regression.md`）：
  - 后端 14 文件定向套件（真实 PG 5434 + 完整 API 链路）：终审修复后复跑 `191 passed, 0 failed`（`c673b925` 前为 `187 passed`；新增 4 条单位-only / 分组列表头用例）。
  - 前端：`npx tsc --noEmit` 0 错误（exit 0）；`npm test -- --run` `227 files passed / 2297 tests passed`（exit 0）。
  - **净新增失败：0**。
- **已确认偏差（本批不实现）**：
  1. **`EvidencePanel` 的 `TopNTable` 表头保持英文**。证据摘要 `trySummarizeEvidence` 是纯函数、没有 DB 会话，本批无法接入字段显示解析，因此证据面板列头继续显示原始列名；后续可由 `report_planner` 在 evidence payload 里携带 `columnLabels` 再统一。
  2. **聚合列反查基础属性只做「大小写不敏感精确相等」名称匹配**。本仓 `plan.aggregations[].alias` 是中文业务别名（不是 SQL 结果列别名），无法据此可靠反查属性，因此只按列名与属性名归一后精确相等匹配，不做模糊匹配（避免猜错列）。匹配不到时按要求回落原始列名并进入「缺中文含义」治理提示。

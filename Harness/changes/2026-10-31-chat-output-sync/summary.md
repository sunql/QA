# 变更：AIChatService 输出四项同步优化（Top-N / 计划卡 / PDF 分节图 / 研究线）

- **日期**：2026-10-31
- **分支**：`feat/chat-output-sync`（base `main` @ `4e535289`）
- **状态**：T1–T7 已完成（见 §8 交付记录），待合并/部署
- **关联**：[2026-10-31-chat-report-charts](../2026-10-31-chat-report-charts/summary.md)（本批是其 §5「非目标」的下一批）、[feat-pdf-export](../feat-pdf-export/summary.md)

---

## 1. 需求

用户原话（2026-10-31）：

> 「PDF 分节插图、Top-N 截断、计划卡片改版、研究线报告 这几个和 AIChatService 相关的功能需要同步优化」

四项是上一批（`chat-report-charts`，main `e54f9005`）明确列为「非目标」的内容。本批成批处理，**四项共用同一条图表渲染链**，避免各改各的再次分叉。

补充要求（同日）：

> 「pdf 导出的时候使用微软雅黑的字体」

## 2. 已拍板的决策

| # | 决策 | 说明 |
|---|---|---|
| D1 | Top-N = **前 20 + 「其他」聚合**，并提供「显示全部」切换 | 用户选定；只作用于排名类图，折线/时间序列不截断 |
| D2 | 计划卡 = **精简摘要 + 展开详情，卡内不再出图** | 图只出现在最终报告对应分节，消除重复 |
| D3 | 研究线报告 = **与聊天完全一致** | 同一套 Top-N、动态高度、数字格式 |
| D4 | PDF = **按分节插入每步图** | 复用 `buildReportSections`，与回放同一函数 |
| D5 | PDF 正文字体 = **微软雅黑**，字体文件外置 | 商业字体不入仓；缺失时回落 STSong-Light |

## 3. 设计

### 3.1 Top-N 截断（A）

**生效条件（后端判定，前端不自造规则）**

- `kind ∈ {bar, hbar, pie, donut}`
- 唯一类目数 > `TOP_N_LIMIT`（20）
- `signals.isTopN or signals.hasRankingIntent`

折线 / combo / heatmap / scatter / waterfall / table / kpi 一律不截断。

**后端（`chart_renderer.py`）**

```python
renderChartOption(spec, data, *, topN: bool = False) -> dict
```

- 先按现状生成**全量 option**，逐字不变（反向守卫钉住）。
- 满足条件时，用同一 builder 对「前 20 行 + 合成行」重算一份，挂到：

```json
{
  "topNOption": { ...完整的 ECharts option... },
  "topNMeta": { "limit": 20, "hidden": 34, "total": 54 }
}
```

- 合成行：
  - 第一维取值 = `其他（{hidden} 项）`；
  - 其余维度组合各生成一行（分组柱状图正确）；
  - 每个指标列 = 尾部各行该列的数值合计（非数值按 0 计；全空 → null）。
- 全量 option 的 `dataZoom` 保持原样；`topNOption` 因 ≤21 类天然不带 `dataZoom`，`grid`/`legend` 由同一 builder 重算，不需要前端补布局。
- 调用点传 `topN=signals.isTopN or signals.hasRankingIntent`：
  - `chart_service.buildChart`
  - `chart_integration.buildReportChart`（研究线报告自动继承）

**持久化**：`topNOption` 随 `chart_option` JSONB 一起落库，回放/导出/研究线**自动继承**，无需新增列或迁移。

**前端**

- 新增纯函数 `frontend/src/utils/chartTopN.ts`：
  ```ts
  topNOptionOf(option): Record<string, unknown> | null
  topNMetaOf(option): { limit: number; hidden: number; total: number } | null
  defaultChartOption(option): Record<string, unknown>
  ```
- `ChartRenderer` 默认渲染 `topNOption`，图下提供切换按钮：
  - 默认态：「显示全部 {total} 项」
  - 全量态：「只看前 {limit} 项」
- 切换是组件本地 state，不改 store、不回后端。
- 高度、数字格式、PNG 截图一律基于**当前显示的 option**。
- 研究线 `ReportRenderer` 复用同一组函数与切换控件。

**已知限制（写入文档）**：`其他 = 尾部合计` 对 SUM/COUNT 类指标成立；AVG/MIN/MAX 类排名图若未来出现，聚合口径需业务确认。当前只在排名意图下触发。

### 3.2 计划卡片改版（B）

`MultiStepPlanCard.tsx`：

- 折叠头一行摘要：
  - 运行中：`执行中 (i+1/n)`
  - 结束：`{n} 步 · 成功 {x}` +（失败时）`· 失败 {y}`
  - 有耗时：追加 `· {s}秒`（取流式 `done` 帧的 `latency_ms`；回放无耗时则省略）
- 展开为步骤时间线：保留 description/subQuestion/状态徽标/`QueryPlanCard`/SQL/摘要/压缩行数/错误/续跑按钮。
- **移除卡内每步 `ChartRenderer`**。
- `ChatMessage` 增可选 `elapsedMs?: number`，由 `chatStore` 的 `done` 处理器从 `latency_ms` 回填；历史回放不伪造。
- 新增 i18n：`multiStep.summary*`、`multiStep.elapsed`。

### 3.3 PDF 分节插图（C）

**导出契约扩展（无迁移）**

- `ChatExportChartImage`（请求）增可选 `stepIndex: int | None`；键变为 `(messageId, stepIndex)`。
- `ChatExportTurn` 增：
  - `report_sections: list[dict] | None`
  - `step_charts: tuple[ExportStepChart, ...]`（`step_index / chart_type / chart_option / chart_image`）

**后端取数（`session_history_service.py`）**

- 两个 payload 构建器（整会话 / 单条）的 SELECT 增 `SessionMessage.steps_snapshot`。
- 用 `buildReportSections(content, steps_snapshot)` 现算 `report_sections` —— 与历史回放同一纯函数，保证 PDF 与屏幕一致。
- `step_charts` 从 `steps_snapshot` 提取（仅 `sql` 非空且 `chartType/chartOption` 非空的步骤）。

**图片校验（`resolveChartImages`）**

- 解码/字节/像素上限不变。
- 新增校验：`stepIndex` 必须落在该消息 `steps_snapshot` 范围内，且该步确有图；否则 422（口径同现有 messageId 校验，不回显 id）。

**PDF 渲染（`pdf_export_service.py`）**

- 有 `report_sections`：按块顺序输出
  - `text` → 现有 Markdown → flowables
  - `chart` → 按 `stepIndex` 取 `step_charts`，三档降级：
    1. 前端回传 PNG → `Image`
    2. `table` / `kpi` → 原生画
    3. 其余无图 → 占位框
- 无 `report_sections` → 完全维持现状（单图挂轮次末尾）。
- 分节轮次不再整轮 `KeepTogether`：只把「用户问题 + 助手标题」保持在一起，分节内容允许跨页（多图轮次超一页时 `KeepTogether` 会告警并强拆）。

**前端（`collectExportCharts.ts`）**

- 有分节 chart 块的多步消息：按 `(messageId, stepIndex)` 逐块截图，跳过该轮顶层图。
- 无分节 → 现状（顶层图）。
- 图表条数 / 像素预算按**当前默认展示 option** 计算，上限不变。

### 3.4 研究线报告（D）

`frontend/src/components/research/ReportRenderer.tsx` 的 chart 块：

- 默认 option = `defaultChartOption(echartsOption)`（Top-N 优先）。
- 高度 = `chartHeightOf(displayedOption)`（替换 `EChart` 默认 280）。
- 数字格式：渲染前 `applyChartNumberFormat`（与聊天端一致：万/亿、千分位）。
- 切换控件与聊天端同一套。
- 评价报告的 `ReportDimensionBar` / `ReportTrendLine` / `ReportPassFailPie` **不动**。

### 3.5 PDF 微软雅黑字体（D5）

**新增 `backend/app/services/pdf_fonts.py`（纯注册/探测，无 IO 之外的副作用）**

- 环境变量：
  - `PDF_CJK_FONT_PATH`：常规体（`.ttf` / `.ttc`）
  - `PDF_CJK_BOLD_FONT_PATH`：粗体（可选）
- 默认探测顺序：
  1. 环境变量
  2. `/app/fonts/msyh.ttc`、`/app/fonts/msyh.ttf`
  3. `docker/fonts/msyh.ttc`、`backend/fonts/msyh.ttc`（本地开发）
- 加载成功：注册 `MSYaHei`，并在同目录探测 `msyhbd.ttc` 作为粗体；无粗体时 `registerFontFamily` 把 bold 映射回常规体（**不能让 `<b>` 回落到 Helvetica，否则中文变方块**）。
- 加载失败/未提供：注册并返回 `STSong-Light`（现有行为），`logger.warning` 提示放置路径。
- 幂等：模块级缓存 + 锁，重复调用不重复注册。

**容器接入**：`docker-compose.yml` / `docker-compose.centos.yml` 的 backend 增只读挂载 `./fonts:/app/fonts:ro`；`docker/fonts/README.md` 说明字体放置与许可责任；`.gitignore` 忽略字体二进制（不把商业字体提交进仓库）。

**验证**：单测覆盖「有文件 → MSYaHei」「无文件 → STSong-Light」「坏路径 → 回落」「粗体映射」；用 reportlab 自带 Vera.ttf 作为替身文件验证注册链路，真实雅黑需部署机放置后冒烟。

## 4. 非目标

- 不做图表服务端重绘（PDF 仍以浏览器渲染的 PNG 为准）。
- 不动 L4 / 单步问答的渲染链路（仅受 Top-N 影响）。
- 不动评价报告的三张固定图（`ReportDimensionBar` 等）。
- 不为 Top-N 新增数据库列或迁移。

## 5. 任务分解（TDD）

| Task | 内容 | 主要文件 |
|---|---|---|
| T1 | 后端 Top-N：合成行 + `topNOption/topNMeta` + 生效矩阵 | `chart_renderer.py`、`chart_service.py`、`chart_integration.py`、单测 |
| T2 | 前端 Top-N：默认展示 + 切换 + 高度/快照跟随 | `chartTopN.ts`、`ChartRenderer.tsx`、`chartSnapshot.ts`、`collectExportCharts.ts`、单测 |
| T3 | 计划卡改版：摘要行 + 移除卡内图 + `elapsedMs` | `MultiStepPlanCard.tsx`、`chatStore.ts`、`types/chat.ts`、i18n、测试 |
| T4 | 研究线报告同步：高度 + 数字格式 + 切换 | `ReportRenderer.tsx`、测试 |
| T5 | PDF 分节插图：导出契约 + 分节渲染 + 前端按步截图 | `schemas.py`、`session_history_service.py`、`pdf_export_service.py`、`collectExportCharts.ts`、测试 |
| T6 | PDF 微软雅黑：字体探测/注册/回落 + compose 挂载 | `pdf_fonts.py`、`pdf_export_service.py`、compose、README、测试 |
| T7 | 回归 + 文档 + 发布说明 | 本文件、`Harness/wiki/chart-rendering.md`、部署说明 |

依赖：T1 → T2/T4；T5 依赖 T1（分节图默认 Top-N）；T6 独立。其余可顺序推进。

## 6. 测试计划

**后端**

- `unit/test_chart_renderer.py`
  - hbar/bar/pie 且 >20 类且有排名意图 → 有 `topNOption`、`topNMeta.hidden` 正确
  - 折线/时间序列/≤20 类/无排名意图 → 无 `topNOption`（反向守卫）
  - 「其他」= 尾部合计；分组柱状图按组求和；全空尾部 → null
  - 全量 option 与改动前逐字一致（快照式断言）
  - `topNOption` 不含 `dataZoom`；全量 >40 类仍含
- `unit/test_pdf_export_sections.py`（新增）
  - 分节顺序 text→chart→text；无分节回落旧路径
  - step 图缺失 → 占位；table/kpi 原生优先
- `unit/test_pdf_fonts.py`（新增）
  - 环境变量文件存在 → `MicrosoftYaHei`；缺失/坏路径 → `STSong-Light`
  - 无粗体文件时 `<b>` 映射回常规；`.ttc` 走 `subfontIndex=0`
  - 反向守卫：导出服务样式必须用解析出的家族名（不得再硬编码）
- `integration/test_session_export_charts.py`（分节用例）
  - 带 `stepIndex` 的请求通过；越界 / 无图步骤 / 无快照 → 422（不回显 id）
  - 分节消息导出包含分节图（flowable 顺序断言）
- `integration/test_chat_multi_step.py` / `test_chat_history_api.py`
  - 响应/回放中的 `chartOption` 携带 `topNOption`

**前端**

- `tests/chartTopN.test.ts`：默认/全量选择、meta 解析、无 meta 原样返回
- `tests/ChartRenderer.test.tsx`：默认 Top-N、点击切全量、无 Top-N 不出按钮
- `tests/MultiStepPlanCard.test.tsx`：摘要行计数、卡内无图、失败/运行中状态
- `tests/collectExportCharts.test.ts`：分节消息按步截图、跳过顶层、预算含步图
- `tests/ReportRenderer.test.tsx`：动态高度、数字格式、Top-N 切换
- `tests/chartSnapshot.test.ts`：截图使用默认（Top-N）option
- `npx tsc --noEmit` 0 错误

**部署冒烟**

1. 本地放 `docker/fonts/msyh.ttc` 后重启 backend；
2. 导出一份多步会话 PDF：正文为雅黑、图在各步骤标题下；
3. 生产：字体放入 `/opt/qa-system/docker/fonts/msyh.ttc`，`docker compose stop/up backend`，同样验证。

## 7. 风险

| 风险 | 缓解 |
|---|---|
| 「其他」求和语义对 AVG 类指标不成立 | 仅排名意图触发；标签写明「其他（N 项）」；文档记录已知限制 |
| 模型未按分节格式输出 | `buildReportSections` 已有回退；PDF 走旧路径 |
| 老会话无 `steps_snapshot` | 字段缺省，零回归 |
| 多图轮次 PDF 超页 | 分节轮次拆掉整轮 `KeepTogether` |
| 微软雅黑缺失 / 许可 | 字体外置不落仓；缺失自动回落 STSong-Light，导出永不失败 |
| `.ttc` 子字体选择错误 | 固定 `subfontIndex=0`（Microsoft YaHei），单测覆盖注册链路 |

## 8. 交付记录

| Task | 提交 | 验证 |
|---|---|---|
| T1/T2 Top-N | 见分支历史（`chart_renderer` / `chartTopN`） | 后端单测 + 前端套件 |
| T3 计划卡 | `969516cf` | `MultiStepPlanCard.test.tsx` |
| T4 研究线报告 | `1deed721` | `ResearchReportRenderer.test.tsx` |
| T5 PDF 分节插图 | `4592a773` | 后端 46 unit + 19 integration；前端 `collectExportCharts` 20 |
| T6 微软雅黑 | 本提交 | `test_pdf_fonts.py` 7；PDF 相关 53 unit + 25 integration |
| T7 文档 | 本提交 | 全前端 227 文件 / 2285 用例、`tsc --noEmit` 0 错误 |
| 审查修复 | `dffb7f4c` | 导出相关 56 unit + 56 integration；前端 227 文件 / 2287 用例 |

**审查修复（`dffb7f4c`）**：`steps_snapshot` 是 T5 新开的一条「落库 → 导出时读回」
数据通路，审查发现三处缺口：

1. **快照落库口径不一致**：顶层 `chart_option` 过 `boundedChartOption`（rows 截
   200 并置 `truncated`），快照只 `jsonSafe` ⇒ 十万行明细原样进 JSONB。现两条路径
   同一口径，且保持不可变（实时响应仍发全量）。
2. **渲染侧无上界**：存量快照行没截过，而 `build()` 在 async 端点里同步排版 —— 
   `_table_from_chart_option` 再截一道（`_PERSIST_MAX_TABLE_ROWS`）并如实标注。
3. **请求体只有 service 级闸门**：`charts` 与 `image_png` 在 pydantic 解析阶段就
   加上界（200 条 / 单图 2 MiB 解码字符量），上界数字统一在
   `app/domain/export_limits.py`。前端 `collectExportCharts` 同步预检单张大小，
   超限只丢那一张，不让整个导出 422。

**部署注意**

1. 字体文件**不在发布包里**：在部署机 `<部署目录>/docker/fonts/` 放
   `msyh.ttc`（+可选 `msyhbd.ttc`），再 `docker compose stop backend && up -d backend`
   —— 新建容器才会应用新挂载。未放文件时导出仍可用，只是回落到 STSong-Light。
2. 后端与前端**必须同批**：导出契约新增 `stepIndex`、`report_sections` 由后端计算，
   旧前端只发顶层图 ⇒ 分节轮次里图会落到占位框（不会报错）。
3. 冒烟：导出一份多步会话 PDF，确认 ①正文为雅黑字形 ②每张图紧跟其步骤标题
   ③「其他（N 项）」聚合行出现在排名图里。

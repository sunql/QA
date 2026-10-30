# 变更：图表输出可读性优化（图例/折叠/断点/数字/PNG/自适应/流式门控）

- **日期**：2026-10-30
- **作者**：Claude / 启琳
- **Phase**：图表渲染可读性（后端 `chart_renderer.py` / `chart_decision.py`；前端 `theme/chartFormat.ts`（新）/ `theme/tokens.ts` / `theme/chartSnapshot.ts` / `utils/download.ts` / `components/chat/{ChartRenderer,MessageItem,ChartSkeleton}.tsx`）
- **状态**：done（已提交到 `feat/chat-chart-rendering`，基于 origin/main `40c9214f`）
- **关联变更**：[2026-09-30-chart-decision-engine](../2026-09-30-chart-decision-engine/summary.md)（决策引擎前置）、[2026-10-01-visual-output-strategy](../2026-10-01-visual-output-strategy/summary.md)（tableOption/visualRationale 契约）
- **迁移版本**：无（无 schema 变更、无新 API 字段、SSE 契约零改动）
- **SSOT 出处**：`Harness/wiki/chart-rendering.md`（本次增量更新）
- **commit**：`75d93412`（G1 图例+G3 瀑布）→ `344ca877`（G2 双射折叠）→ `be662dda`（G4 数字格式化）→ `a155f166`（G5 PNG+ResizeObserver）→ `7d73f3e6`（G6 门控+骨架+卡片 token）；分支 `feat/chat-chart-rendering`

---

## 1. 需求

用户原话：「单独在主分支上开一个分支，把残留处理掉，新的分支要优化 AIChatService 的功能中**图标输出的效果**」（「图标」经确认为「图表」）+ 截图补充：「**图例说明放到下面，现在图例和标题重合了，导致无法看清楚**」（31 供应商分组柱状图，图例折多行盖住标题）。经 AskUserQuestion 用户四个方向**全选**：视觉缺陷修复 / 数字与标签可读性 / 图表选择与数据质量 / 流式时序与交互。

## 2. 六个缺陷（全部 TDD 闭环）

| # | 缺陷 | 根因 | 修复 | 层 |
|---|---|---|---|---|
| G1 | 图例盖标题 | `_legend` 只发 `{"show": true}`，ECharts 默认顶部放置，31 系列折多行 | `{"show","type":"scroll","bottom":0}`（`_scrollLegend`，轴图与饼类共用）；`grid.bottom` 32/56 联动 | 后端结构 |
| G2 | 31 供应商假分组 | GROUP BY `code,name` 严格双射，被当两维画 31 系列分组柱状 | `buildChartSignals` 前置 `_collapseBijectiveDimensions`：折叠为单维、保留平均标签更长者；R13 原始明细 / 重复行 / None / 单行不折叠 | 后端决策 |
| G3 | 瀑布缺失值伪 0 | 循环把 None 当 0 推进累计 | 无效行输出双 None（base/height），累计不推进（断点） | 后端渲染 |
| G4 | 数字裸奔 | formatter 是 JS 函数，JSON 下不来 | 前端 `theme/chartFormat.ts`：轴万/亿（zh）、tooltip 千分位 ≤2 位小数；只补缺省不覆盖已有；不遍历 series | 前端主题层 |
| G5 | PNG 打不开 / 暗色白字 / 侧栏开合不重绘 / 卡片暗色不可读 | ① `new Blob([dataUrl])` 把 base64 字符串原样写进 .png；② 在线实例 getDataURL 带暗色注入；③ ResizeObserver 缺失；④ 卡片 `#fafafa` 硬编码 | ① `dataUrlToBlob`（atob→Uint8Array）；② 导出改走 `renderChartPng` 离屏亮色渲染（传**原始结构**）；③ 容器 ResizeObserver（width≤0 忽略、disconnect、无 API 静默）；④ 语义 token `colorChatAssistantBg/UserBg`（DARK `#152838`/`#1c3b56`） | 前端视觉 |
| G6 | 图先于答案出现 | `chart` SSE 事件先于答案 token 到达 | `chartReady = Boolean(chartType) && (!isStreaming \|\| content.trim().length > 0)`；未就绪渲染 `ChartSkeleton`（role="status" + `chat.visual.chartLoading`） | 前端时序 |

## 3. 架构边界（不变的铁律）

- **结构归后端，表现归前端**：G1–G3 改的是后端 JSON 结构；G4–G6 全在前端。数字 formatter 只能是前端 JS 函数。
- **SSE / API 契约零改动**：无新字段、无新端点；`chartType`/`chartOption`/`tableOption`/`visualRationale` 语义不变。
- **在线与截图同口径**：组合顺序恒为 `applyChartTheme(applyChartNumberFormat(raw, locale), token)`（离屏为 `"zh-CN"` + LIGHT_TOKEN）；formatter 先于主题（主题只补色不碰 formatter）。
- **既有拍板不变**：多步顶层无图（e2e `charts.length===0` 不动）、聊天侧不注入维度显示名、TABLE/KPI 契约冻结；`spec.sort` 与死配置 `CHART_TOP_N_MAX` 本批**不激活**。

## 4. 关键实现细节

- **双射判定**（`_isBijectivePair`）：行数 = 双侧基数、`forward/backward` 两 dict 双向单值、任一行含 None 即否。折叠保留 `_avgLabelLength` 更长者（tie 留首维）。折叠后 ruleId 由 R10 变 R11/R12 是**预期语义变化**。`heatmapCoverage` 在折叠**后**计算。
- **formatter 纪律**（`applyChartNumberFormat`）：只碰 value/log 轴与 `tooltip.valueFormatter`；已有 formatter（函数/字符串）一律不覆盖；**绝不遍历 series**（饼图 `{b}: {d}%`、瀑布 `{c}` 逐字保留）；不可变；非对象原样返回（后端 JSON 形状不可信）。
- **hooks 纪律**（ChartRenderer）：`useEffect`（ResizeObserver）必须放在 `isKpi && !kpiPayload` 等早退**之前**——组件随消息流重挂，早退后挂 hook 直接炸。
- **门控收敛性**：`onDone`/`onError`/`stopRun` 都把 `isStreaming` 置 false ⇒ 骨架必收敛；模型全程无 token 时骨架保留到 done/error（优于空气泡蹦图）。
- **i18n**：`chat.visual.chartLoading`（zh「图表生成中…」/ en「Rendering chart…」）双字典同步。

## 5. 测试与取证

- 后端（真实 PG 5434）：`test_chart_{renderer,decision,spec_builder,service}.py` **185 passed**；PDF/历史/rationale 定向 **62 passed**。新增：图例 5 测、瀑布断点 2 测、双射折叠 8 测、端到端 31 供应商回归 1 测（HBAR 单系列 + 无 legend + 名称列类目）。
- 前端：全量 vitest **2233 passed / 0 失败**（224 文件）；`tsc -b` 0 错误；覆盖率 functions **82.54%** ≥ 80% 门禁。
- 新增前端测试：`chartFormat.test.ts`（14）、`download.test.ts`（dataUrlToBlob 3）、ChartRenderer（离屏导出 4 + ResizeObserver 4）、MessageItem（门控 7 + 主题 4）。
- **既有测试适配**：`test_sparse_two_dimensions_falls_back_to_bar` 的 20 行对角 fixture 恰是双射（会被折叠改判 R12）——补一行非双射数据保住 R10 语义。

## 6. 风险与已知边界

- **折叠误判面**：只认严格双射；偶发「两列恰好 1:1 但业务上是两维」会少一条系列——可接受（信息无损，两列语义等价）。
- **formatter 不可序列化**：只存在于前端内存 option，禁止 JSON.stringify 后回传/落库（现有落库链路只存原始结构，未触碰）。
- **万/亿边界**：9999.999 刻度可能显示 10,000（刻度通常整数，仅观感）。
- **PNG 导出尺寸**：固定 800×420（2x），换取恒亮色。
- **骨架长留场景**：模型全程无 token（如纯图直答异常）时骨架保留到 done/error——刻意优于空气泡。

## 7. 部署

- 后端：`docker compose build backend` + `up -d`，**无迁移**。
- 前端：`docker compose build frontend` 必须显式 `--build-arg VITE_API_BASE_URL=`（空），否则 localhost 绝对地址烤进 bundle ⇒ 非 localhost 全站 CORS 挂。
- 冒烟清单：出图查询（图例在底部）、31 供应商案例（单维横柱、图例消失）、暗色切换（卡片与轴文字可读）、侧栏开合（图重绘）、PNG 下载（文件可打开）、流式时序（骨架 → 图表）。

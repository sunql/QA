# 变更：图表进「最终报告」（聊天最终回答 + 导出 PDF）

- **日期**：2026-09-30
- **作者**：Claude / 启琳
- **Phase**：图表渲染（Part A：`chat_multistep.py` / `chat_stream.py`；Part B：`session_history_service.py` / `pdf_export_service.py` / `session.py` + 前端 `utils/chartSnapshot.ts` / `utils/collectExportCharts.ts` / `utils/chartContract.ts`）
- **状态**：**代码完成且测试通过，未提交**（工作区改动；本分支 `feat/chart-decision-engine` 上叠着决策引擎与本变更两层，逐文件清单见文末「改动清单」）
- **关联变更**：[2026-09-30-chart-decision-engine](../2026-09-30-chart-decision-engine/summary.md)（决策引擎本身，本变更是它的直接后续）
- **迁移版本**：`0105`（`session_message` +`chart_type` VARCHAR(20) NULL / +`chart_option` JSONB NULL）；**仅在 `qa_metadata_test` 执行，生产 `qa_metadata` 未执行**
- **SSOT 出处**：`Harness/wiki/chart-rendering.md`（本变更扩写「线上契约」与「前端」两节）
- **commit**：无（待提交）

---

## 1. 需求

用户原话：

> 「弄错了一个地方，图表应该是在**最终的报告**里面显示，不是只在分析计划里面显示。
> 最终报告要有图表，分析计划里面可以有，也可以没有」

改造前的问题：决策引擎落地后，**单步**查询的图直接出现在回答里，但**多步**查询的图
只出现在「分析计划」折叠卡的每个步骤内，最后那条汇总回答**是纯文字**；导出 PDF 里图表
更只有一个灰框占位（`pdf_export_service._chart_placeholder_flowable`，写着「图表对象未
持久化，原始 ECharts option 不可在 PDF 重渲染」）。图表被当成**分析过程的附属品**，
而不是**结论的组成部分**。

用户拍板两点：

1. **「最终报告」= 两个都要** —— 聊天里的最终回答要带图，导出的 PDF 也要是真图。
2. **用最后一个数据步骤的数据画图** —— 整条链的终点，也是服务端保存的追问锚点
   （`last_plan`/`last_sql`/`last_data`），决策引擎已经为它画过一张图，提到响应顶层即可。

---

## 2. Part A：多步最终回答带图

**规则**：多步响应的**顶层** `chartType`/`chartOption`/`data` = **最后一个成功数据步骤**
的图。判据复用既有的 `run.result.sql is not None`（与 `last_plan`/`last_sql`/`last_data`
同一处）；全部步骤失败 → 顶层为 `None`（**不是空图**）；超限拒收分支没有数据步骤 → 不动。

### 后端非流式（`chat_multistep.py`）

`_executeMultiStep` 有两个承载数据的 return（聚合成功 / 降级收尾），都补上三个字段。
循环里与 `last_data` 并列记录 `last_chart_type` / `last_chart_option`，
`_finalizeMultiStepDegrade` 新增两个关键字参数并转交落库 —— 避免两个 return 各写一遍判据。

### 后端流式（`chat_stream.py`）

`_streamMultiStep` 原先**完全没有** `EVENT_CHART`。新增 `_reportChartEvent` 静态助手，
在**两条终点的「回答 token 之后、`EVENT_DONE` 之前」**各发一次：

```
聚合成功：token(925) → chart(929-933) → done(939)
降级收尾：token(984) → chart(986-990) → done(996)
```

**为什么走独立 `chart` 事件而不是塞进 `done` 帧**：`done` 虽已带 `steps`，但前端
`StreamSummary` 没有 `steps` 字段、`done` 分支也不读 `d.steps`；塞进去要同时扩前端两处。
而 `chart` 事件前端**已经完整支持**（`onChart` → `patchLastMessage`），走它等于零前端改动。

`_reportChartEvent` **有图才发**（无图返回 `None`，调用方不发）—— 空负载的 `chart` 事件
只会让前端多做一次无谓的消息改写。

### 前端

**零改动**。已核实两条链路都会接住顶层字段：流式 `chatStore` 的 `onChart`；非流式读
`res.chartType`。`MessageItem` 的渲染门 `Boolean(chartType)` 已经把图挂在回答下方。

**计划卡里的每步图保留不动**（用户明确说「可以有，也可以没有」）。代价是最后一步的图
会在「计划卡的该步」与「最终回答」各出现一次 —— 计划卡默认折叠，不构成视觉噪音。

---

## 3. Part B：导出 PDF 里的真图

### 3.1 为什么是「前端渲染 → 回传 PNG」而不是服务端渲染

服务端把 ECharts option 变成图片只有两条路，都比回传 PNG 差：

- **装无头浏览器**（playwright + chromium）：后端镜像陡增，容器内存与启动成本高。
- **用 matplotlib 重画一遍**：等于**第二套渲染器**，与前端的图必然长得不一样，
  违反 SSOT。项目对同类问题的既有判断也是这个方向 —— `evaluation_report_export_service.py`
  明确写着「报告 PDF 内的图表用文字+表格占位，不重画 echarts」。

另有一条被探索证实的技术理由：`builder.build(payload)` 是**同步调用、跑在 async handler
里**（`session.py`），服务端光栅化会**阻塞事件循环**；而图片只是往既有的同步 build 里
多插一个 `Image` flowable。前端已经用 ECharts 渲染过这张图，让它导出同一张 PNG 回传，
是唯一既零新依赖、又像素级一致的路子（`ChartRenderer.tsx` 的「导出 PNG」按钮早已在用
`getDataURL({type:"png", pixelRatio:2, backgroundColor:"#fff"})`，技术已验证）。

### 3.2 持久化（`chat_context.py` + `0105`）

`session_message` 加两列。**为什么必须持久化**：导出会话时**早先轮次的图不在页面上**
（前端注释明确「chart/chartOption/data 未持久化，历史回放仅展示文本与 SQL」）。不落库
就只能导出最近一轮。

写入点是 `_storeSessionMessages` —— **一轮对话唯一的落库点**，增两个默认 `None` 的关键字
参数，`app/services/` 下既有 **28 处**调用点语义不变。顺带修好「历史会话看不到图」这个
既有缺口。

两个必须在落库点（而不是各调用点）做的归一：

- **`chartTypeName`**：`ChatResponse.chartType` 类型是 `ChartType | None`，pydantic 保留
  **枚举成员**；列是 VARCHAR(20)，不归一就会静默存进 `"ChartType.BAR"` 这种脏值。
  让它成为「唯一落库点」的职责，而不是靠 28 个调用点各自记得写 `.value`（忘了不报错）。
- **`boundedChartOption`**：`QUERY_ROW_LIMIT` 默认 0 = **不限行**，而
  `chart_renderer._buildTable` 返回的 `rows` 是**全量** data —— 一条「列出所有…」的明细
  查询能把十万行塞进 `session_message` 的一行（DB 膨胀 + 导出 PDF 时同步构建巨型表格会
  阻塞事件循环）。`_PERSIST_MAX_TABLE_ROWS = 200` 截断并记 `truncated: true`。
  **只截落库份**：实时响应仍发全量（用户当场要的就是全部行），历史回放与导出拿到的是
  截断份，由 `truncated` 标记如实告知，不假装是全部。

按《魔数治理》的**「判别不清」档不治理**（防御性兜底，不是运营想调的边界，是「别把库里
写爆」的闸），代码注释写明理由。

### 3.3 导出端点：GET → POST（**契约变更**）

`GET /api/v1/sessions/{sessionId}/export.pdf` → **`POST`**，body：

```json
{ "messageId": 123, "charts": [{ "messageId": 123, "imagePng": "data:image/png;base64,..." }] }
```

前端是我们自己的 SPA，GET 无法带图片体；保留两个入口会变成两条会漂移的路径。
`charts` 缺省为空列表 ⇒ **等价于旧行为**（PDF 里图表回落占位框）—— 图缺失**不影响导出
本身的成败**：导出是主功能，图是增强。

**输入校验**（边界处显式失败，全部 422）：

| 控制 | 常量 | 值 |
|---|---|---|
| 单图解码后上限 | `_MAX_EXPORT_IMAGE_BYTES` | 2 MB |
| `charts` 条数上限 | `_MAX_EXPORT_CHART_IMAGES` | 200 |
| 全部图片解码后总量 | `_MAX_EXPORT_TOTAL_IMAGE_BYTES` | 20 MB |
| 单图像素上限 | `_MAX_EXPORT_IMAGE_PIXELS` | 8 M px（见 §3.6(a)） |
| 全部图像素上限 | `_MAX_EXPORT_TOTAL_IMAGE_PIXELS` | 40 M px（见 §3.6(a)） |

`charts[].messageId` 必须**属于该 session**（不信任前端传来的 id），否则 422。
总量是**逐张累加即判**的 —— 等全部解完再量体，等于让一个请求先把内存占满。

### 3.4 前端取图的三个设计决策

`collectExportCharts(sessionId, messageId?)` **以服务端的消息流为准，而不是以屏幕上的
列表为准**，三条理由：

1. 位图要按 `SessionMessage` 主键归属，而实时会话的前端消息**根本没有 DB id**
   （`dbMessageId` 从未被回填 —— 单条导出的按钮实际上是死代码）。拿屏幕上的列表去猜 id
   是在猜。
2. 导出必须覆盖**已经滚出屏幕甚至已经不在当前列表里的轮次** —— 这正是要给
   `session_message` 加列的原因。
3. 服务端返回的 `chartOption` 就是**当时落库的那份结构**，与历史回放画出来的图一致；
   用屏幕上的现值反而会让「看到的图」与「导出的图」分叉。

所以：GET 一次消息流 → 挑出需要截图的轮次 → 离屏渲成 PNG → 交给导出端点。

`chartSnapshot.renderChartPng` 的要点：离屏容器 **显式 800×420**（ECharts 在零尺寸容器里
会拿到 0×0 画布）、**固定亮色 token + 白底**（PDF 页面是白的，跟随暗色主题会得到白底上的
浅色图）、关动画、`finally` 里 dispose + 移除容器、任何异常返回 `null`。

`table` / `kpi` **不截图** —— 后端原生排版（见 3.5），比位图清晰。

### 3.5 PDF 渲染三档降级（`pdf_export_service.py`）

```
原生 table / kpi（从 chart_option 画，不吃图）  >  回传的 PNG  >  灰色占位框
```

`table` 档排在最前是刻意的：它是**文字**，比位图清晰，而且**前端截图失败时表格照样出**。
占位框保留给改动前的历史消息（没有 `chart_option`）。表格单元格经 `_escape_cell` 转义
（`html.escape(..., quote=False)`）后再进 reportlab Paragraph；`truncated` 时附
「表格仅显示前 N 行」。

### 3.6 安全审查修掉的两个缺陷（**都在新端点的既有代码里，不是设计缺陷**）

新端点首次接收**用户提交的二进制**，因此走了 `security-reviewer`。审查揪出两处会让
「一张图换一份报告」甚至「一张图拖垮进程」的问题，两处都已修并补测试。

#### (a) 解压炸弹：字节闸管不住解码后的大小（CRITICAL）

一张 12000×12000 的**纯色** PNG 压缩后只有 580 KB —— 字节闸（2 MiB）放行、PNG 魔数也
对，交给 Pillow 却要按 **1.44 亿像素**分配内存（实测单张峰值 RSS +2 GB）。而 PIL 默认的
`MAX_IMAGE_PIXELS = 89_478_485` **只在 2 倍以上（1.79 亿）才抛异常** —— 中间这一大段
完全是不设防的。

修法：在**任何解码之前**从 PNG 的 IHDR 头直接读宽高（`_pngPixelCount`，纯 stdlib：
8 字节签名 + 4 字节块长 + 4 字节块类型 ⇒ 偏移 16 起 8 字节），自己卡像素数：

| 控制 | 常量 | 值 |
|---|---|---|
| 单图像素上限 | `_MAX_EXPORT_IMAGE_PIXELS` | 8 M px（≈4000×2000） |
| 全部图像素上限 | `_MAX_EXPORT_TOTAL_IMAGE_PIXELS` | 40 M px |

上界取 8 M px 的依据：前端真实产物是 800×420 × `pixelRatio 2` = **1.34 M px**，留了近
6 倍余量；总量 40 M px 把一次请求的瞬时解码内存压在 ~160 MB。**为什么必须自己卡**：
`PIL.Image.open` 一旦打开就已经按解出的尺寸分配过内存，让它先解码再回头问「你多大」，
炸弹早响完了。像素与字节**同口径逐张累加即判**（等全部解完再量体 = 让一个请求先把内存
占满）。IHDR 读不出来（截断/畸形）按「不是合法 PNG」拒掉，不交给 Pillow 赌它报不报错。

> 这一档按《魔数治理》的**「判别不清」档不治理**：它是抗滥用上界，不是运营想调的策略；
> 常量 + 注释写明理由。

#### (b) 窄高图让整份导出变 500（HIGH）

`_chart_image_flowable` 原先只夹**宽度**。一张 100×4000 的窄高图宽度完全合规、高度却
溢出页面 —— reportlab 会在 `doc.build()` 里抛 `LayoutError`，而**那已经出了本函数的
`try`**，于是整份导出 500，而不是按设计降级成占位框。

修法：`_CHART_IMAGE_WIDTH_RATIO` 改名 `_CHART_IMAGE_BOX_RATIO`（宽高两轴都用它，所以是
「框」不是「宽」），并取两个方向所需比例的**较小者**：

```python
scale = min(box_width / image.drawWidth, box_height / image.drawHeight, 1.0)
```

`min(…, 1.0)` 保证小图不被放大（放大只会糊）。测试钉了三件事：高图**按高度**缩到
`(PAGE_HEIGHT - 2*MARGIN) * 0.7`、小图不被放大、以及端到端真跑一次 `build()`
**不许抛 `LayoutError`**。

### 3.7 取图窗口对齐：`/messages` 新增 `tail` 模式

**这是个会让整批图静默消失的错配**，审查时才发现：

| 侧 | 窗口 |
|---|---|
| 导出（`_buildFullSessionPayload`） | 最新的 500 轮（`turn_dict[-500:]`） |
| 前端取图（`loadFullMessages`） | `order_by(id.asc()).limit(n)` ⇒ 最**早**的 n 条 |

长会话下两个窗口**完全不相交** ⇒ `attachChartImages` 一张也匹配不上 ⇒ 导出的 PDF
**全是占位框，且不报任何错**。

修法：`loadFullMessages` 增 `tail: bool = False`（先倒序取最新 N 条再翻回正序，响应契约
恒为「按时间正序」）；API 侧 `GET /sessions/{id}/messages` 增 `?tail=true`；前端
`loadSessionMessages` 改传 `{ tail: true }`。反向守卫：`before_id` 与 `tail` 同时给时
**`before_id` 优先**（翻页游标是更具体的意图），并有测试钉住。

---

## 4. 新增的图表字段收窄模块（`frontend/src/utils/chartContract.ts`）

原 `VALID_CHART_TYPES` / `isChartType` 住在 `api/chat.ts`。本次导出功能需要从
`stores/chatStore` 与 `utils/collectExportCharts` 复用同一个白名单，若继续从 `api/chat.ts`
导入，就会出现「store 为了一个谓词反向依赖 HTTP 客户端」的耦合 —— 以及随之而来的一整类
**替身漂移**（本次已实测：`chatStore.test.ts` 6 个用例爆红，根因就是 5 个测试文件
把 `../api/chat` 整体替身化）。

故抽出 `utils/chartContract.ts`（纯函数，无 IO）：`VALID_CHART_TYPES` / `isChartType` /
`normalizeChartType` / `asChartOption`。**收窄规则描述的是线上契约，不是 HTTP 传输**，
三个消费者分属三层（SSE/HTTP 响应、历史回放、导出挑图），放一个谁都能 import 的纯函数
模块里才对。**不是**靠给测试打补丁绕过去的。

---

## 5. 顺带的三处抽取（文件 / 函数尺寸约束）

本变更把三个文件推过了「800 行」红线，顺手做了三处**内聚性**改进。**都不是为了压行数
而拆**：每一处拆出来的东西都有独立的职责与测试。

1. **`services/chat_chart_persist.py`（新，58 行）** —— 从 `chat_context.py` 抽出
   `chartTypeName` / `boundedChartOption` / `_PERSIST_MAX_TABLE_ROWS`。「图表负载怎么
   才能在库里存得住」与「一轮对话怎么落库」是两件事，前者有自己的单测
   （`unit/test_chat_chart_persist.py`，10 例）。`chat_context.py` 由 820 → **773 行**。
2. **`chat_helpers._multiStepResponse`（新，模块级构造器）** —— 多步的两条终点
   （聚合成功 / 降级收尾）原先各写一遍 13 个字段的 `ChatResponse(...)`，补图时要在两处
   各改一遍。收敛成**单一构造点**后，「两处漏一处」这类漂移不再可能。
3. **前端 `collectExportCharts` 的 `RENDER_CONCURRENCY = 4`** —— 见下。

### 5.1 `chat_multistep.py` 仍为 803 行（**顶着上限，如实记下**）

拆完仍在红线之上 3 行，**根因不是行数而是函数长度**：`_executeMultiStep` 实测 **191 行**
（AST 统计），远超「函数 < 50 行」的约束。它是一条 5 阶段流水线（召回 → 计划 → 逐步执行
→ 汇总 → 降级）写在一个函数里，真正的修法是**按阶段拆 mixin**，属于既有结构债，不在本次
「图表进最终报告」的范围内。同批的 `chat_stream.py`（1117 行）**改动前就已超线**。
两条都记入待办，不以「本次没加多少行」为由掩盖。

### 5.2 截图并发必须有上限

`renderChartPng` 在 `await` 之前是**同步**执行的，所以 `Promise.all(全部轮次)` 会在一个
不可中断的主线程突发里建出**全部**离屏容器与 ECharts 实例 —— 每张画布 800×420 CSS ×
`pixelRatio 2` ≈ 5.4 MB 后备存储，180 张接近 1 GB 加 180 个 DOM 节点，中端机器直接卡死
甚至 OOM。改成**分块：块内并发 4、块间串行**，峰值降到 4 张。截断到
`MAX_EXPORT_CHARTS = 180` 时**保留最新的若干张**（丢最早的）—— 与导出窗口「最近 500 轮」
同向。渲染失败与消息流拉取失败都留一条可 grep 的 `console.warn`，不静默。

---

## 6. 测试

### 后端（全部真 PostgreSQL + 完整 API 链路）

| 文件 | 数 | 覆盖 |
|---|---|---|
| `integration/test_chat_multi_step.py` | 37 | 顶层图 = 最后一步图；中间步失败；全失败 → `None`；流式 `chart` 事件恰好一次且在 `done` 前 |
| `integration/test_chat_history_api.py` | 20 | 历史消息带 `chartType`/`chartOption`；`tail` 取最新 N 条、`before_id` 优先于 `tail` |
| `integration/test_chat_stream_api.py` | 4 | 图**真的写进了** `session_message`（见下） |
| `integration/test_session_export_charts.py` | 14 | POST 200 + `application/pdf` + 首字节 `%PDF-`；像素闸（解压炸弹 / 总量 / IHDR 不可读）；跨 session → 422 |
| `integration/test_export_pdf_api.py` | 6 | GET→POST 契约迁移 |
| `integration/test_session_message_columns.py` | 4 | 两列存在 + 可空 + ORM 与库一致 |
| `unit/test_pdf_export_charts.py` | 24 | 三档降级；解码四道闸；宽图/高图缩放与小图不放大；表格单元格内容与转义 |
| `unit/test_chat_chart_persist.py` | 10 | 枚举归一为裸字符串；截行 + 标记；**不改调用方那份**；边界与畸形负载 |
| `unit/test_chart_image_bounds.py` | 12 | IHDR 像素解析（含 0 维/截断）；像素上界**低于** Pillow 自己的阈值 |

**两条结构性断言**，都刻意做成「退化实现过不去」：

- **`/Subtype /Image`**：PDF 里出现该字节串才证明位图**真的落进去了**，而不是只证明
  「请求被接受」。已验证有图 True / 无图 False。
- **写入路径**：`test_chat_stream_api` 在真实 `/chat/stream` 之后直接断言
  `SessionMessage.chart_type == "bar"`（裸字符串）且 `chart_option` 非 null。只测 schema 与
  读路径会漏掉这一层 —— 四个 `_storeSessionMessages` 调用点任何一个漏传，读取侧照样全绿
  （读到 `None` 而已），而**导出 PDF 与历史回放会整批没有图**。

`backend/app/tests/_png_support.py` 用 stdlib（`zlib` + `struct`）造 PNG 夹具 ——
Pillow 只是**传递依赖**（由 `pytesseract` 带入），测试不该依赖它。
`pngDeclaringSize(12000, 12000)` 造的是「IHDR **声明**超大、实际像素数据仍是 8×8」的图：
真要造 1.44 亿像素的扫描线，测试为验证一个内存闸先吃掉 400 MB 是荒唐的。改 IHDR 数据
必须**重算 CRC**，否则连合法 PNG 都不是。

### 前端

`chartSnapshot.test.ts` 12、`collectExportCharts.test.ts` 13、`chatHistoryApi.test.ts` 9、
`ChartRenderer.test.tsx` 19、`ChatPage.test.tsx` 19、`chatStore.test.ts` 39。
`npx tsc --noEmit` 干净；全量 1320 passed / 1 failed（`AgentRegistryPage.test.tsx`）。

> 那 1 个 failed 是**既有**的：它是一条在测试结束后才 reject 的未处理 Promise（表单校验
> 拒绝）。已用 HEAD 基线 worktree（`git worktree add` + 软链 `node_modules`）在同一文件上
> 复现出**完全相同的** `5 passed, 1 error` —— 与本次改动无关，也不影响该文件 5 个用例全过。

### 后端全量单测：49 红，**与 HEAD 逐条一致**

`pytest app/tests/unit` 得 **49 failed / 3292 passed**。**逐条比对过基线**：在 HEAD
worktree（同一份 `TEST_DATABASE_URL`、并把 0105 迁移脚本拷进去以免「基线找不到 head」的
假差异）跑同一套，失败集合与本次树 **diff 为空**（49 = 49，双向零行差异）。集中在
`test_chat_service_stream.py`(10) 及其余 chat/适配器套件 —— 正是既有记录在案的
**陈旧假替身**红，不是本次改动所致。

集成侧本次跑了受影响的 5 个文件：**82 passed**（见上表）。

---

## 7. 顺带修掉的四个既有缺陷

都不是本次引入的，但都挡在本次功能的路上（或会被误认成本次改动弄坏的），一并修并钉住。

### 7.1 `/messages` 的游标参数名从没对上过（`before_id`）

前端发的是 `params: { beforeId }`，后端声明的是 `alias="before_id"` —— FastAPI **静默忽略**
了不认识的查询参数，于是**翻页从来没有生效过**（每次都返回最早那批）。修 `loadSessionMessages`
签名时一并改成 `before_id`，并在测试里把参数形状钉住（注释写明这条曾经静默失效）。

### 7.2 流式测试里 `chartType == "pie"` 是引擎重写前的旧期望

`test_chat_stream_api::test_streams_full_pipeline_events` 断言 `pie`，实际出 `bar`。
**先证伪「是我改坏的」再动手**：建 HEAD 基线 worktree 复跑，基线**失败得更早且不同**
（`_StubEmbeddingService` 缺 `searchSimilarQueries`）；把 0105 迁移脚本拷进基线以越过
alembic 失败后，基线在同一处报**同样的** `bar`/`pie`。根因：该测试文件最后修改于决策引擎
重写**之前**，而假计划
`{"target": ..., "selectedClasses": ["PRECEIPT"]}` 没有 aggregations ⇒ 没有 formula ⇒
R02（占比 → 环形）**不可达**，落 R12 ⇒ 柱状。改期望值并写明「别照着改回去」。

### 7.3 前端历史回放不披露表格截断

后端落库时把表格截到 200 行并打 `truncated: true`，PDF 也如实标注，但前端回放**丢掉这个
信息** —— 刷新后那张表看起来就是完整结果，连「导出 CSV」导出的也是截断份。`ChartRenderer`
在表格下方补一行次要色说明（`chartExport.truncated`，中英各一条），并补正反两个用例：
带 `truncated` 要出提示；**不带 `truncated` 不许出**（反向守卫）。

### 7.4 导出按钮在 real-auth 下一律 403（**部署后才暴露**）

**症状**：容器里登录、`/chat/stream`、`/messages` 全部正常，唯独点「导出 PDF」返回
**403「请先登录」**。

**根因**：`exportSessionPdf` 刻意绕开 `httpClient`（PDF 是二进制，走不了 `ApiResponse`
信封解包 ⇒ 连**请求拦截器的 Bearer 注入也一并绕开**），却只手工塞了两个头：

```ts
"X-Tenant-Id": httpClient.defaults.headers["X-Tenant-Id"],   // 有值，但不是凭据
"X-User-Id":   httpClient.defaults.headers["X-User-Id"],     // 恒 undefined（从没设过）
```

而 `sessions` router 是**router 级**鉴权（`api/v1/session.py:59`
`APIRouter(dependencies=[Depends(getCurrentUser)])`，HEAD 上就有），`docker/nginx.conf:60-62`
又把 `X-User-*` 整族剥掉 ⇒ 这条请求**没有任何凭据**。

**为什么一年都没被测试发现**：`chatHistoryApi.test.ts` 的 `httpClient` mock 里写着
`headers: { "X-User-Id": "user-1" }` —— 真实 client 从来不设这个键（`api/client.ts:44-47`
只有 `Content-Type` + `X-Tenant-Id`）。那份**虚构的 mock** 让断言看着通过。本次先把 mock
改成与真实 defaults 一致，测试随即变红，再修实现。

**修法**：改用既有 SSOT `authHeaders()`（`api/authHeaders.ts`，SSE 那次修的就是同一类问题）。
`exportSessionPdf` 是**唯一**一个手搓头的裸 axios 路径 —— 其余三处（`ontology.ts` /
`wikiImport.ts` / `document.ts`）都已走 SSOT。

**双向钉住**：单测断言请求头含 `Authorization: Bearer` + `X-Tenant-Id`，且**不含**
`X-User-Id`（反向守卫）；e2e 在网络层再断言一次（`exportRequestHeaders.authorization`）。

---

## 8. 已知边界与待办

### 8.1 导出与 `/messages` 没有 channel/user_id 归属校验（**既有缺口，本次未扩大**）

`listChatSessions` 有归属校验，但 `buildExportPayload` 与 `/messages` 都**没有**。
本次**不扩大**它：新 POST 至少校验了 `charts[].messageId` 属于本 session，
`body.messageId` 也走同一道校验。**记入待办**。

### 8.2 超长会话只有最新 1000 条内的轮次能带上图

前端一次导出最多扫 1000 条消息（后端导出本身只覆盖最近 500 轮 ≈ 1000 条），同量级；
更长的会话里更早的轮次带不上图（`tail` 取的正是最新那批）。如实记下，不假装没有。

### 8.3 计划里对测试环境的一处判断是错的（**更正**）

计划写「既有 `test_export_pdf_api.py` 实际跑在内存 SQLite 上」。**traceback 证明是错的**
—— 它走 `integration/conftest.py` 的真 PG `client` fixture。同时本次观察到
`test_session_message_columns.py` 在**未导出 `DATABASE_URL` 时**会落到 SQLite 并报
`NoSuchTableError: session_message`；导出后 4 passed。**环境相关，非本次改动所致**。

### 8.4 `npm run lint` 是**既有损坏**，与本次改动无关

`eslint src --ext ts,tsx` 报 `ESLint couldn't find an eslint.config.* file`。
已核：`git ls-tree -r HEAD --name-only | grep -i eslint` 返回**空** —— HEAD 上根本没有
ESLint 配置文件；且 `eslint` **不在 `devDependencies` 里**（版本 10.11.0 要求 flat config）。
**建议单独立项**：要么补 `eslint.config.js` 并加依赖，要么删掉这条 script。

### 8.5 会话回放仍取**最早** 200 条（**观察，未改**）

`chatStore.loadSessionMessages` 调 `apiLoadSessionMessages(sessionId)` 时既不传 `limit`
也不传 `tail` ⇒ 默认 `limit=200` + `order_by(id.asc())` = **最早** 200 条。超过 200 条的
会话重新打开时，看到的是开头而不是最近的对话（且因为 §7.1 的游标 bug，此前连翻页都没有）。
本次**只**给导出链路接了 `tail`，没动回放 —— 换默认值会改变所有长会话的可见行为，属于
本次需求之外，**留给用户拍板**。建议改 `tail: true`（与导出窗口、与聊天界面的直觉都一致）。

### 8.6 `qa:chat:lastSessionId` **从来没被写过** ⇒「刷新恢复会话」不生效（**观察，未改**）

`persistChatUiState.ts` 的头注释写着这个键「用于刷新恢复」，store 初始化也读它
（`chatStore.ts:165`）。但 `writePersisted({ lastSessionId })` 只有四处调用：
`setChannel` / `resetSession` / `loadSessionMessages` / `deleteSession` —— **发送路径一次都不写**。
实测（playwright 读 localStorage）：进入 `/chat` → 提问 → 刷新，三个时点
`lastSessionId` 恒为 `null`。

两个后果：① 刷新后 store 走 `generateSessionId()` 起一个**全新会话**（历史面板里不会出现
「当前」徽标，正是 e2e 第一次跑 step 7 卡住的原因）；② 即使曾点历史项载入过（那时才写键），
`ChatPage` 挂载时也**不会**调 `loadSessionMessages` —— 刷新后永远是空对话。

**本次不改**：这是既有交互行为，改成自动回放会改变所有用户的开屏行为，属需求之外
（与 §8.5 是同一片区域，建议与它一起拍板）。e2e step 7 因此改为走真实路径
（刷新 → 展开历史面板 → 点最新那条）并把 DOM 上的 canvas 钉到 `/messages` 的
`chartType` 字段上。

### 8.7 待办

- 8.1 的归属校验缺口
- 8.4 的 lint 配置缺失
- 8.5 的回放窗口默认值（需拍板）
- 8.6 的刷新恢复（写 `lastSessionId` + 挂载时回放，需拍板）
- `chat_multistep.py` 的 `_executeMultiStep`（191 行）按阶段拆 mixin；`chat_stream.py`
  （1117 行，改动前即超线）同样待拆。**两条都是本次之前就存在的结构债**（见 §5.1）

---

## 9. 部署与验证（2026-09-30 已在本地容器栈执行完毕）

**前后端必须同批**：导出端点是 GET→POST 契约变更，只发一边会让导出按钮直接坏掉。
前端镜像改动走 `docker compose build --no-cache frontend`（`npm run build` ≠ 容器 bundle）。
后端更新走 `./scripts/deploy_backend.sh`。

**已执行**：

| 步骤 | 结果 |
|---|---|
| 备份生产 `qa_metadata` | `backups/pg/qa_metadata_2026-09-30_1747.dump`（13.2 MB）+ 快照表 `session_message_bak_20260930`（1054 行）。此前 `backups/pg/` **一个 dump 都没有**（与已知的备份 cron 失效一致） |
| `alembic upgrade head` | `0104 → 0105`；`chart_type varchar(20) NULL` / `chart_option jsonb NULL`；1054 行原样 |
| 前后端镜像重建 | 后端 `0105 (head)`；nginx → `/api` 通 |
| 写入路径实证 | 会话 `s-munxqo89-kngom9` 的 assistant 行 id=1093 带 `chart_type='table'` + `chart_option` |

**端到端冒烟** `scripts/e2e_smoke/chart_report_e2e.mjs`（真实浏览器 → nginx → 真 PG + 真 LLM，
8 步，**两条路径各跑一遍全过**）：

| 路径 | 问句 | 结果 |
|---|---|---|
| 多步（Part A） | 「第一步：各采购组织的收货数量；第二步：各供应商的收货数量」 | 2 步；SSE 序列 `…step_plan, token, **chart**, done` —— **chart 排在最后一个 token 之后**（Part A 契约）；DOM 仅 1 个 canvas 且计划卡未展开 ⇒ 图在**最终回答**里 |
| 单步（回归） | 「各采购组织的收货数量」 | `hbar` 12 行；243 个 token 帧；chart 在 done 之前 |

两条路径的导出均：请求头带 `Authorization: Bearer`、请求体 `charts[].imagePng` 前缀合法、
响应 `%PDF-` 且含 `/Subtype /Image`；**打开 PDF 目视确认是真图（横向条形图），不是灰框**。
刷新 → 历史面板点回 → canvas 仍在且 `/messages` 的 assistant 行带 `chartType`。

> **两处 probe 假设写错，是我的错不是应用的**：① 先假定多步与单步的 `chart` 帧位置一致
> （单步本就 `chart` 在前，见 §2）；② 先假定刷新会自动回放会话（见 §8.6）。两处都已按
> 真实契约定正，并把真实序列落盘到 `sse_events.txt` 便于比对。

---

## 10. 改动清单

**新增（11）**

| 路径 | 职责 |
|---|---|
| `backend/alembic/versions/0105_session_message_chart.py` | `session_message` +两列 |
| `backend/app/services/chat_chart_persist.py` | 落库归一：枚举 → 裸字符串、表格截行 |
| `backend/app/tests/unit/test_pdf_export_charts.py` | 三档降级 + 缩放 + 单元格 |
| `backend/app/tests/unit/test_chat_chart_persist.py` | 归一/截断纯函数 |
| `backend/app/tests/unit/test_chart_image_bounds.py` | IHDR 像素解析与上界 |
| `backend/app/tests/integration/test_session_export_charts.py` | 导出端点 HTTP 契约 + 边界 |
| `frontend/src/utils/chartContract.ts` | 图表字段收窄唯一口径 |
| `frontend/src/utils/chartSnapshot.ts` | 离屏渲染 PNG |
| `frontend/src/utils/collectExportCharts.ts` | 挑轮次 + 限并发截图 |
| `frontend/src/tests/chartSnapshot.test.ts` | 同上单测 |
| `frontend/src/tests/collectExportCharts.test.ts` | 同上单测 |
| `scripts/e2e_smoke/chart_report_e2e.mjs` | 部署后端到端冒烟（8 步，真实浏览器；含 Bearer / 截图 / 嵌图三条网络层断言） |

**修改（后端）**：`api/v1/session.py`（GET→POST + `tail`）、`services/session_history_service.py`
（像素闸 / 解码 / `loadFullMessages.tail`）、`services/pdf_export_service.py`（三档降级 + 缩放）、
`services/chat_context.py`（落库传图）、`services/chat_helpers.py`（`_multiStepResponse`）、
`services/chat_multistep.py`（顶层图 + 记录最后一步）、`services/chat_service.py`、
`services/chat_stream.py`（`_reportChartEvent`）、`domain/error_messages.py`、`domain/models.py`、
`domain/schemas.py`、`tests/_png_support.py`（`pngDeclaringSize`）、5 个后端测试文件。

**修改（前端）**：`api/chatHistory.ts`（POST + `before_id` 修正 + `tail` + **改用
`authHeaders()`**）、`pages/ChatPage.tsx`、`stores/chatStore.ts`、
`components/chat/ChartRenderer.tsx`（截断披露）、`i18n/{zh-CN,en-US}.ts`
（`chartExport.truncated`）、`types/chatHistory.ts`、`types/chat.ts`（注释指向新模块）、
6 个前端测试文件（`tests/chatHistoryApi.test.ts` 的 mock defaults 同步改成与真实 client 一致）。

**文档**：`Harness/wiki/chart-rendering.md`、`chat-service-capabilities.md`、`data-model.md`。

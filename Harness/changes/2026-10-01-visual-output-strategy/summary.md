# 变更：可视化输出策略（图 / 表 / 图+表 + 判断依据）

- **日期**：2026-10-01
- **作者**：Claude / 启琳
- **Phase**：可视化输出（后端 `visual_rationale` / `visual_payload` / `chat_*` 契约贯穿 + 前端 `chartContract` / `ChartRenderer` / `MessageItem` / i18n）
- **状态**：done（代码已就绪，**待部署** —— alembic 0107 尚未应用，见 §6 残留）
- **关联变更**：[2026-09-30-chart-decision-engine](../2026-09-30-chart-decision-engine/summary.md)（规则表 + spec 的前置）、[2026-09-30-chart-in-final-report](../2026-09-30-chart-in-final-report/summary.md)（0105 多步顶层继承图，本次**反转**其多步部分）
- **迁移版本**：**0107**（`session_message` 加 `table_option` / `visual_rationale` 两列；**尚未应用，部署时 prod 先备份**）
- **SSOT 出处**：`Harness/wiki/chart-rendering.md`（本变更更新 §线上契约 + §前端）
- **commit**：`4f277d2`（Task 1 rationale）→ `5cda713`+`aedaaf6`（Task 2 阈值治理）→ `5e5beb1`（Task 3 装配）→ `eb01dde`（Task 4 契约贯穿）→ `7bc50c0`（Task 5 多步去图）→ `4345371`（Task 6 落库 0107）→ `39b0d37`（Task 10 Decimal 修复）→ `8683897`（Task 7 前端收窄）→ `0c76156`（Task 8 前端渲染+i18n）；Task 9（e2e 断言 + 文档）随本记录一并提交。

---

## 1. 问题

用户 2026-10-01 需求：**单问 / 多步 / 多轮，每轮回答都要判断输出图、表、还是图+表**；有数据清单默认给表；是否出图由可视化规则引擎判定；**必须输出判断逻辑**（为什么用这个图 / 为什么不出图），最终总结同口径；多步每步可出图+表，最后的汇总通常不出图表。

改造前（0105 状态）有三个缺口：

1. **图+表组合不存在** —— 契约是 `chartType` 单值（11 图型 + TABLE），图 XOR 表。
2. **判断依据不外露** —— `ChartDecision.ruleId` 只进日志，前端看不到「为什么画这张图 / 为什么不画」。
3. **多步汇总步没有「为什么不出图」** —— 且 0105 的「多步顶层 = 最后一个成功数据步骤的图」让汇总重复显示一张继承来的图，冗余且误导。

## 2. 根因

- **图+表/依据缺位**：`chart_service` 出口只产 `{chartType, chartOption, data}`，没装配「图之外的明细表投影」与「ruleId 映射出的判断依据」。
- **多步顶层继承图（0105 引入）**：本意是让图进最终回答（导出 PDF / 历史回放），但每步卡片已各挂各的图（chart-decision 决策 3），顶层那张是**冗余** —— 多步汇总的语义是**文字结论**，继承来的图会与各步骤的图重复，且误导读者以为汇总步骤自身产出了图。
- **依据不落库**：`session_message` 只有 `chart_type`/`chart_option` 两列（0105），刷新/导出后图旁的表格与「为什么这么画」整体消失。

## 3. 设计（用户已拍板的 8 项决策）

| # | 决策 |
|---|---|
| 1 | **多步顶层继承图取消** —— 最终汇总回答纯文字 + rationale（反转 0105 多步部分） |
| 2 | 图+表时表格**默认折叠**（图为主、表备查）；仅表场景自然展开 |
| 3 | rationale = **结构化 `{code, params}` + 前端 i18n 模板**渲染，后端不出文案 |
| 4 | **KPI 不附表** |
| 5 | 落库**加两列**（alembic 0107）：`table_option` + `visual_rationale` |
| 6 | rationale **不进 PDF** |
| 7 | 前后端**同批部署** |
| 8 | 表截断沿用 `FULL_DATA_THRESHOLD=100`，且该阈值**迁入 system_config** |

核心形态矩阵：单步图形类 = 图 + 折叠数据表 + rationale 说明行（三件套）；单步 TABLE（明细/降级）= 仅表 + rationale；单步 KPI = 仅指标卡 + rationale；空数据 = 空表 + rationale；多步每个数据步 = 同上（挂各步卡片）；**多步汇总步 = 仅文字 + `SUMMARY_TEXT_ONLY` rationale（无图无表）**。

## 4. 落地内容

| 任务 | 产出 |
|---|---|
| 1 | `services/visual_rationale.py`：`VisualRationale`（frozen）+ `buildVisualRationale` + `summaryTextOnlyRationale`；**21 个 code**（18 个 ruleId + `R_FORCED_CLIENT` + `DEGRADE_SPEC_INVALID` + `SUMMARY_TEXT_ONLY`），零 LLM 零 IO |
| 2 | `FULL_DATA_THRESHOLD` 迁 `system_config`（`readPositiveIntConfig` 公开 + `loadFullDataThreshold`），`summarize_data` 加可选参数透传，种子里补 `FULL_DATA_THRESHOLD` 并纳入漂移守卫 |
| 3 | `services/visual_payload.py`：`assembleTableOption`（图形类才附表；TABLE/KPI → None）；`ChartBuild` 加 `tableOption` + `rationale` |
| 4 | 契约贯穿：`StepResultRead` / `ChatResponse` / `StepResult` / `_chartStep`(7-tuple) / `_stepResultEvent` / 单步 `chart` 事件加 `tableOption` + `visualRationale`；`params.kind` 出口归一为枚举 `.value`（`"bar"` 而非 `"ChartType.BAR"`） |
| 5 | **多步去顶层继承图**：删 `last_chart_type`/`last_chart_option` 全量传递与 `_reportChartEvent`；汇总步 `chartType=None` + `done` 帧带 `visualRationale=SUMMARY_TEXT_ONLY`（正常/降级两终点；超步数拒绝路径不动） |
| 6 | alembic **0107**：`session_message` 加 `table_option`/`visual_rationale` JSONB；`boundedTableOption`（200 行兜底闸）；回放 `ChatMessageRead` 透传两字段 |
| 10 | **前置修复**：`boundedChartOption` 接 `_jsonSafe`（`Decimal→float` 同渲染器 `toNumber` 口径），修掉 0105 遗留的 TABLE 负载 JSONB 序列化崩溃（改「无变化返回原对象」保同一性契约） |
| 7 | 前端契约收窄：`TablePayload`/`VisualRationale` 类型 + `asTablePayload`/`asVisualRationale`，**9 处接入点同口径**（api 4 处 + store 5 处）；`done` 事件只收窄 `visualRationale`（不带 tableOption） |
| 8 | 前端渲染 + i18n：图形类下图 + antd `Collapse`「数据表」（`defaultActiveKey=[]` 默认折叠）+ rationale 次要色一行（**所有形态都渲染**）；`MessageItem` 消息级 rationale（多步汇总）；i18n `chat.visual.<code>` **21 条**（单花括号插值 `{rows}`/`{kind}`）；`visualRationale.ts` 做 kind 本地化 + 缺 key 回退 |

### 过程中发现并修掉的真缺陷

1. **0105 遗留的 Decimal 崩溃**（Task 10）：TABLE 负载存 `Decimal`，`session_message.chart_option` 无自定义 serializer ⇒ `json.dumps` 抛 `TypeError` ⇒ 该轮**整轮不落库**。此前无测试覆盖（既有用例全用 `int`）。
2. **i18n 双花括号陷阱**（Task 8）：本仓 i18next 被覆写为单花括号插值（`i18n.ts`），照双花括号 `{{rows}}` 会渲染出字面量 `{{rows}}`；改用 `{rows}`。

## 5. 验证

> 判定口径：失败 **`文件::用例`** 集合 diff，不按名字子串统计。以下数字是各提交上**实测**的锚点，不是断言。

| 套件 | 结果（在哪个提交上量的） |
|---|---|
| 后端 unit 层（`pytest app/tests/unit -q`，真实 PG 5434） | **49 failed / 3361 passed**（187 文件）。控制器实测的**既有红**（chat 套件陈旧假替身 + 空计划闸门 `PLAN_EMPTY` 夹死、`test_dependencies` Header.lower、`test_datasource_pool` Oracle 引号、`test_query_plan_generation` pop empty），**与本次无关**（已用 BASE/HEAD 失败 ID 集合 diff 证实 49 条逐字节相同） |
| 后端 chat 集成套件（真实 PG，串行） | **16 红**（`PLAN_EMPTY`，同一批陈旧假替身），已与 `5e5beb1` 基线逐字 diff 为空 —— 既有红，非本次引入 |
| 前端裸跑（`cd frontend && npx vitest run`） | Task 8 GREEN 实测（commit `0c76156`）：**2 failed \| 1401 passed (1403)**／161 文件。2 红为**既有基线红**：① `chatStore > 流式 plan 事件回填 ReAct 查询计划（Phase E）` ② `EntityMappingPage > 点击新建并提交调用 createMapping`。失败集合与 BASE `8683897`（2 failed \| 1381 passed）完全一致 |
| 前端类型/构建（`npm run build` = `tsc -b && vite build`） | Task 8 实测绿（`✓ 4293 modules transformed`） |
| **Task 9 本任务** | `node --check scripts/e2e_smoke/chart_report_e2e.mjs` → **通过**（语法自检，确保不是坏文件）；e2e 断言逐条与 Tasks 1–8 实际实现核对（见 §6 残留） |

## 6. 残留 / 未完成（deferred, awaiting authorization）

1. **部署**（决策 7 同批）：prod 备份 → `alembic upgrade head`（0107）→ 后端镜像重建 → `docker compose build --no-cache frontend` → 起容器 → **e2e 8 步全绿**。**全部 deferred，等用户授权** —— 仓库有生产库被清空的历史，alembic `env.py` 默认连 prod，未授权绝不动手。
2. **e2e 执行**：`scripts/e2e_smoke/chart_report_e2e.mjs` 已更新断言（多步顶层无图 / 每步有图+折叠表 / 汇总依据可见 / 单步三件套），**未执行**（须部署后跑，本任务不跑）。
3. **既有测试债务**（**非本次引入**，勿写成「已修复」）：后端 unit 49 红、chat 集成 16 红、前端 2 红；前端 `coverage.thresholds` 80% 本就失败（其他特性的 0% 覆盖）。
4. **deferred Minor**（Task 7 记录）：`chatStore.ts` 4 处站点未补 `normalizeChartType`/`asChartOption`（既有路径不一致，扩大爆炸半径，刻意不碰）。
5. **0107 尚未应用**：当前状态是「代码已就绪、待部署」，文档不把「已部署/已上线」写成既成事实。

## 7. 风险与后续

1. **`chartType` 枚举扩容是破坏性的**（沿用 chart-decision 的教训）：前端 `VALID_CHART_TYPES` 漏同步 → 新图型静默变 null。本次只加字段不改枚举，但仍按决策 7 同批部署。
2. **rationale code 全集还在长**（当前 21 个）：`asVisualRationale` **不内置白名单**，后端可先发新 code，前端缺 key 时显示 code 原文兜底（代价不对称 —— 图型白名单漏同步是「图静默消失」，rationale 漏一个 code 只是说明行退化）。
3. **多步每步的图不落库**（只汇总消息落一行）：各数据步的图/表/依据走 `step_result` 响应，不进 `session_message` —— 刷新后回放只重建汇总步（纯文字 + SUMMARY_TEXT_ONLY），各步图需重新展开步骤卡查看。

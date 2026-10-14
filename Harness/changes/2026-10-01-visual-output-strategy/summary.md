# 变更：可视化输出策略（图 / 表 / 图+表 + 判断依据）

- **日期**：2026-10-01
- **作者**：Claude / 启琳
- **Phase**：可视化输出（后端 `visual_rationale` / `visual_payload` / `chat_*` 契约贯穿 + 前端 `chartContract` / `ChartRenderer` / `MessageItem` / i18n）
- **状态**：**已部署并验证**（2026-10-01）。生产 `alembic` = **`0107`**、两列已建；前后端同批上线；**e2e 8/8 全过**；`seed_system_config` 已执行（`system_config` 22→27 行）。详见 §6 实测记录。
- **关联变更**：[2026-09-30-chart-decision-engine](../2026-09-30-chart-decision-engine/summary.md)（规则表 + spec 的前置）、[2026-09-30-chart-in-final-report](../2026-09-30-chart-in-final-report/summary.md)（0105 多步顶层继承图，本次**反转**其多步部分）
- **迁移版本**：**0107**（`session_message` 加 `table_option` / `visual_rationale` 两列；**2026-10-01 已应用于生产**，应用前已取 dump 并用 `pg_restore --list` 验证可读）
- **SSOT 出处**：`Harness/wiki/chart-rendering.md`（本变更更新 §线上契约 + §前端）
- **commit**：`4f277d2`（Task 1 rationale）→ `5cda713`+`aedaaf6`（Task 2 阈值治理）→ `5e5beb1`（Task 3 装配）→ `eb01dde`（Task 4 契约贯穿）→ `7bc50c0`（Task 5 多步去图）→ `4345371`（Task 6 落库 0107）→ `39b0d37`（Task 10 Decimal 修复）→ `8683897`（Task 7 前端收窄）→ `0c76156`（Task 8 前端渲染+i18n）→ `e05b085`（Task 9 e2e 断言 + 文档）→ `7e6d510`（最终评审修复批：KPI 依据 + NaN 守卫 + 注释/守卫清理）→ `cd0746d`（修复轮 2：L1 **无值**路径改 `R00_EMPTY_TABLE`，不再谎称「以指标卡呈现」+ 删两条永真断言）→ `1fbe66e`（docstring 收敛：去掉「枚举归一」虚假声明）

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
3. **L1 KPI 直答出图却无任何判断依据**（最终整支评审发现）：`chat_service._wrapChatResponse` → `_buildKpiChart` 只返回 2-tuple，**完全绕开** rationale 机制，且 `_storeSessionMessages` 只落 `chart_type`/`chart_option`。⇒ 直接**违反用户原始需求**「不论是否输出图，必须输出一个判断逻辑」（实时与回放都没有说明行）。修复：在该路径合成 `R01_SINGLE_VALUE_KPI` 依据并**一并落库**。
4. **`_jsonSafe` 的 NaN/Infinity 漏洞**（最终整支评审发现）：`float(Decimal("NaN"))` → `nan` → `json.dumps` 出裸 `NaN` → **PG jsonb 拒绝**（`invalid input syntax for type json`），而 `_storeSessionMessages` 无 try/except ⇒ **整轮不落库**。与第 1 条**同症状、同根因类**（都是「JSONB 裸序列化遇见非常规标量」），触发条件更窄。修复：非有限浮点按本模块既有「不静默丢值」doctrine 落字符串。

## 5. 验证

> 判定口径：失败 **`文件::用例`** 集合 diff，不按名字子串统计。以下数字是各提交上**实测**的锚点，不是断言。

| 套件 | 结果（在哪个提交上量的） |
|---|---|
| 后端 unit 层（`pytest app/tests/unit -q`，真实 PG 5434） | **49 failed / 3361 passed**（187 文件）。控制器实测的**既有红**（chat 套件陈旧假替身 + 空计划闸门 `PLAN_EMPTY` 夹死、`test_dependencies` Header.lower、`test_datasource_pool` Oracle 引号、`test_query_plan_generation` pop empty），**与本次无关**（已用 BASE/HEAD 失败 ID 集合 diff 证实 49 条逐字节相同） |
| 后端 chat 集成套件（真实 PG，串行） | **16 红**（`PLAN_EMPTY`，同一批陈旧假替身），已与 `5e5beb1` 基线逐字 diff 为空 —— 既有红，非本次引入 |
| 前端裸跑（`cd frontend && npx vitest run`） | Task 8 GREEN 实测（commit `0c76156`）：**2 failed \| 1401 passed (1403)**／161 文件。2 红为**既有基线红**：① `chatStore > 流式 plan 事件回填 ReAct 查询计划（Phase E）` ② `EntityMappingPage > 点击新建并提交调用 createMapping`。失败集合与 BASE `8683897`（2 failed \| 1381 passed）完全一致 |
| 前端类型/构建（`npm run build` = `tsc -b && vite build`） | Task 8 实测绿（`✓ 4293 modules transformed`） |
| **Task 9 本任务** | `node --check scripts/e2e_smoke/chart_report_e2e.mjs` → **通过**（语法自检，确保不是坏文件）；e2e 断言逐条与 Tasks 1–8 实际实现核对（见 §6 残留） |
| 最终评审修复批 `7e6d510` | 后端 unit 整层 `pytest app/tests/unit -q` → **49 failed / 3365 passed / 1 skipped**，**既有红集合不变**（`3361→3365` 系本批新增用例，非把红转绿）；F1/F2 均**先红后绿**；本批单测 68 passed + 集成 7 passed。本批**未改前端**，故未跑前端套件 |

## 6. 部署记录与残留（2026-10-01 **已部署并验证**）

1. **部署**（决策 7 同批）：prod 备份 → `./scripts/deploy_backend.sh`（灌代码 + 重启）→ `docker compose build --no-cache frontend` → 起前端 → **e2e 8 步全绿**。
   - **⚠️ 迁移不是手工步骤 —— 容器自己跑**（2026-10-01 实测确认）：`docker/Dockerfile.backend:79` 的 CMD 是
     `sh -c "uv run alembic upgrade head && uv run uvicorn app.main:app ..."`。⇒ `deploy_backend.sh` 的
     `docker restart` 会**在容器内**按 `&&` 顺序先迁移、后起服务（迁移失败则 uvicorn 根本不启动，
     不会出现「新代码 × 旧 schema」）。**不要在宿主手工跑 `alembic upgrade head`** —— `env.py` 只认
     `DATABASE_URL`，宿主默认指向生产库，绕过容器是自找麻烦（见 memory `qa-system-alembic-targets-prod`）。
     `scripts/deploy_backend.sh` 的 `SYNC_DIRS` 含 `alembic:/app/alembic`，故 0107 的版本文件会随代码一起灌进容器。
   - **✅ 已于 2026-10-01 执行完毕**（用户原话授权「代码开发，并测试完毕，可以部署」）。四项独立复核**全过**：
     生产 `alembic_version` = **`0107`**；`session_message` 两列为 `jsonb`；容器内 `grep -c table_option models.py` → **3**（新代码）；
     日志 `Running upgrade 0106 -> 0107`。**前端**：`--no-cache` + `--build-arg NPM_REGISTRY=npmmirror` 构建 exit 0 → 起容器；
     **bundle 内容核对**（不只信「容器重启了」）：`index-5VLnvL_N.js` 里 `R00_EMPTY_TABLE`/`SUMMARY_TEXT_ONLY`/`R_FORCED_CLIENT` 各 2 处（zh+en）。
     **代理链路**：首页 200；经 nginx `/api/v1/health` → 200（**无 upstream IP 陈旧 502**）；`/api/v1/auth/me` → **403**（应用层拒绝 ⇒ 证明请求确实到了后端）。
   - **✅ `seed_system_config` 已执行**（2026-10-01，用户批准）：五个受治理键此前在生产**只是没有行**，而 `PUT` 对不存在的 key 返 **404**、
     admin UI 的 key 字段**只读** ⇒ **不 seed 就等于不可调**（决策 8 只落地了一半）。执行后 `system_config` **22 → 27 行**，
     值均等于代码默认（**行为无变化**），无覆盖告警 ⇒ 纯插入、未冲掉任何调优。
     ⚠️ 该脚本是**覆盖式 upsert**，重跑会把已存在的值重置回默认 —— **不得放进启动路径或定时任务**。
   - **⚠️ 唯一的硬顺序约束**：**迁移必须先于新后端容器启动**。ORM（`models.py`）无条件映射这两列，且 `session_history_service` 显式 SELECT 它们 ⇒ HEAD 代码撞 `0106` schema 会让**每一次** `session_message` 读写都抛 `UndefinedColumn`，**chat 与历史回放都会坏**。上面的顺序已满足，**只是不能调换**。反向（先迁移、后换镜像）无害。
   - **部署前只读预检实测（2026-10-01）**：生产 `qa-postgres` = alembic **`0106`**、`session_message` 两列均不存在；**测试库 `qa_metadata_test` 已是 `0107`** ⇒ 该迁移**已被真实执行过一次**，升级路径是验证过的而非仅写出来的；`qa-backend` 容器**仍跑旧代码**（`grep -c table_option app/domain/models.py` → 0）⇒ 当前不存在「新代码 × 旧 schema」的危险组合。
   - **✅ 备份闸门已满足**（2026-10-01 20:14 实测）：手工跑 `./scripts/backup_pg.sh` 成功产出
     `backups/pg/qa_metadata_2026-10-01_2014.dump`（13.5 MB），并已用 `pg_restore --list` 验证**可读且可用**
     （768 TOC 条目，含 `session_message`）—— 未验证过的备份不算备份。
   - **⚠️ 但「定时备份」仍从未触发**（独立运维问题，非本次引入）：此前最新 dump 停在 `2026-09-30 22:32`（约 21 小时前），**当天没有任何备份**。`crontab` 里 `0 8`/`0 20` 两条 `backup_pg.sh` 条目**都存在**，但 `backup.log` **无任何 08:00/20:00 记录**，且已排除「陈旧锁静默跳过」（锁分支会写 ERROR 行、`.backup.lock` 也不存在）⇒ **调度从未触发**（与 memory `qa-system-cron-silently-broken` 吻合）。
     **本次手工跑成功 ⇒ 脚本本身没问题，坏的是调度**（这条排除了「脚本有 bug」的替代解释）。存储仍须另案处理。
2. **✅ e2e 已执行：8/8 全过（0 失败）**，关键证据（非只报绿勾）：
   Step 4 `chartType=hbar, 数据行=12, tableOption=有, rationale=R12_CATEGORY_BAR`；
   Step 5 canvas=1 + 折叠数据表面板=1（三件套确实渲染）；
   Step 6 导出 PDF 98 KB 且含 `/Subtype /Image`；
   Step 7–8 刷新回放 + `/messages?tail=true` 的 assistant 行均带 `chartType=hbar`（证明 0107 两列**在真实读写**）。
   ⚠️ **本次跑的是脚本默认的「单步」问题**；**多步分支未跑**（需换 `E2E_QUESTION` 成能拆 ≥2 数据步的问法）。
3. **既有测试债务**（**非本次引入**，勿写成「已修复」）：后端 unit 49 红、chat 集成 16 红、前端 2 红；前端 `coverage.thresholds` 80% 本就失败（其他特性的 0% 覆盖）。
4. **deferred Minor**（Task 7 记录）：`chatStore.ts` 4 处站点未补 `normalizeChartType`/`asChartOption`（既有路径不一致，扩大爆炸半径，刻意不碰）。
5. **0107 已应用**（2026-10-01）：生产 `alembic_version` = `0107`，两列已建。此前「代码已就绪、待部署」的状态描述**已过期**。
6. **新增 2 条 LOW（纯文档，范围重审发现，**非**代码缺陷）**：① `chat_chart_persist.py:31-38` 的 `chartTypeName()` docstring 理由**错**
   （「忘了写会静默存进 `"ChartType.BAR"`」—— 实测 asyncpg 走实例字符串内容，落库是 `bar`）；**函数不删**：它仍有非 str 兜底
   （`chartTypeName(42)=="42"`）、单点显式化、以及若 `ChartType` 将来不再是 `(str, Enum)` 则纯 `Enum` 成员**无法编码**的保护价值。
   ② `schemas.py:1774` 注释仍写「params.kind 是枚举真值」。
7. **`R00_EMPTY_TABLE` 在「有行但值非数值」子情形下文案略偏**：`查询无结果` 字面不准确（行是有的；`不生成图表` 那半句对）。
   用户仍能从答案正文看到真实值 ⇒ 属「说明不精确」而非「假话」。若该情形常见，再加专用 code。

## 7. 风险与后续

1. **`chartType` 枚举扩容是破坏性的**（沿用 chart-decision 的教训）：前端 `VALID_CHART_TYPES` 漏同步 → 新图型静默变 null。本次只加字段不改枚举，但仍按决策 7 同批部署。
2. **rationale code 全集还在长**（当前 21 个）：`asVisualRationale` **不内置白名单**，后端可先发新 code，前端缺 key 时显示 code 原文兜底（代价不对称 —— 图型白名单漏同步是「图静默消失」，rationale 漏一个 code 只是说明行退化）。
3. **多步每步的图不落库（已拍板的取舍，非缺陷 —— 不要当 bug 去「修」）**
   - **机制**：各数据步的图/表/依据走 `step_result` 响应，**不进 `session_message`**；而顶层汇总行在决策 1 之后 `chart_type = NULL`。⇒ 多步对话在**服务端消息流里没有任何一张图**。
   - **两个可见后果**：
     - **PDF 导出**：`utils/collectExportCharts.ts:48-49` 是按**服务端消息流**的 `message.chartType` 挑图截图，故多步对话导出的 PDF **一张图都没有**（0105 时还有「最后一个数据步」那张）。
     - **刷新回放**：`chatStore.toChatMessage` **不重建 `steps`**（导入表里没有 steps 路径，`ChatMessageRead` 也不带）⇒ 多步对话回放为**纯文字 + `SUMMARY_TEXT_ONLY` 依据**，**哪里都没有图**。「重新展开步骤卡看各步图」只对**未刷新**的实时会话成立。
   - **实时（未刷新）视图不受影响**：各数据步的图/表/依据仍正常显示在**步骤卡**里。
   - **💡 用户 2026-10-01 明确选择「接受」（方案 1）**。**被主动放弃的东西**：0105「图表进最终报告」为多步对话导出最后一步那张图的能力。
   - **被否决的两个替代方案**（记录在案，避免反复重提）：
     - *方案 2*：在汇总行持久化「最后一个成功数据步」的图（即恢复 0105 行为）⇒ **会重新引入「实时 vs 回放」不对称** —— 刷新后汇总行有图、实时却不显示，等于把决策 1 想要消除的「继承图误导」换个地方复现。
     - *方案 3*：持久化**每一步**的图 ⇒ 需要新的存储形态决策（新表或 JSONB 数组），成本远超收益。
   - Task 9 的 e2e 已按本决定断言多步 `charts.length === 0`（**与决定一致，不要改**）。

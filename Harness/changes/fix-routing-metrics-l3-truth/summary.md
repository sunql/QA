# 变更：fix-routing-metrics-l3-truth

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：「L3 监控桶前端真话化」（chat-service-assessment §2.5 `routing_layer` 从不写 L3 行 + §15 残差 #2）
- **状态**：done
- **关联变更**：`chore-l3-deadcode-and-prior-cte-contract`（M5 删 L3 引擎）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.5「`routing_layer` 从不写 `L3`」+ §15 残差 #2

---

## 1. 需求

L3 CTE 引擎已于 2026-09-27 删除（M5 批），但**前端展示**仍把 L3 当作 4 层路由之一渲染：

- `routingMetrics.ts` 联合类型 `"L1" | "L2" | "L3" | "L4"`、注释 `ChainedStep CTE multi-step chain`
- `LAYER_COLORS` 含 L3 (`#fa8c16`)、`buildLineOption` 含 L3 mock 数据 series
- `zh-CN.ts` 「L3 多步链式推理」标签 + 副标题 `L1/L2/L3/L4`

生产数据 `session_message.routing_layer` 只可能为 `L1` / `L2` / `L4`（实测：`:1005` 单级 + `:808` `:1557` 一路写 L2 与 `SELECT`，`L3` 写入点零存在）⇒
**前端 L3 桶恒为 0** ⇒ 监控页 L3 卡片无意义、折线图 L3 线无意义，属**用户可见假话**。

用户口径（binding）：
- L3 联合类型与 `LAYER_COLORS` 一并删除；
- 多步语义（实际由 `_executeMultiStep` 承担，路由层记 `L2`）**并入 L2 文案**，不另设「多步子层」标签；
- 监控页 UI 适配：卡片动态渲染、饼图动态渲染（保留），折线图 mock 删 L3 series；
- 前端镜像必须重建（i18n 编译期注入，docker cp 改不了）。

## 2. 设计评审

### 候选方案

| # | 方案 | 否决/选定理由 |
|---|---|---|
| A | 仅删类型联合 | 否决：`LAYER_COLORS` / `buildLineOption` 引用 L3 后类型会从「字符串字面量联合」漂成「any/never」错误，不彻底清会让 TS 编译挂掉 |
| B | **类型联合 + `LAYER_COLORS` + 折线图 mock + i18n 四处一并删除**（选定） | 联动改才能保持 TS 严格联合；一处遗漏即类型错 |
| C | 把 L3 留作 `LayerMetric` 的可选值（"预留位"） | 否决：L3 层已被 M5 批结构性删除，"预留位"无意义只会复活展示漂移 |

### 用户可见契约变化（i18n diff）

| 文案 | 旧 | 新 |
|---|---|---|
| `routingMetrics.subtitle` | "L1/L2/L3/L4 各层命中率、延迟与 Token 成本" | "L1/L2/L4 各层命中率、延迟与 Token 成本（L3 已废弃，多步记 L2）" |
| `routingMetrics.layer.L1` | "L1 语义匹配" | 同 |
| `routingMetrics.layer.L2` | "L2 LLM 意图分类" | "L2 LLM NL2SQL（含多步拆解）" |
| `routingMetrics.layer.L3` | "L3 多步链式推理" | （删） |
| `routingMetrics.layer.L4` | "L4 LangGraph Agent" | "L4 Agent Loop"（LangGraph 是虚构，同批与 agent-loop.md 一致化） |

### 不做

- **不另设 `L2-multi-step` 标签**：`_executeMultiStep` 路径路由层仍写 `"L2"`，细分会让监控面板再次过度承诺（多步/单步在 plan prompt 形式可观察，无需在前端多一组桶）；
- **不接入真实 backend endpoint**：`RoutingMetricsPage` 后端 `/api/v1/routing-metrics/snapshot` 未实现（前端 TODO 仍在），不属于本批范围；
- **不接入 `App.tsx` 路由**：`RoutingMetricsPage` 是孤立页面（探索阶段发现未注册），接入属独立功能增强。

## 3. 数据模型变更

无。后端 `session_message.routing_layer` 列已存在，校验约束早就是「枚举在代码侧」而非 DB CHECK ⇒ 前端类型收紧不触发任何迁移。

## 4. 接口契约变更

| 面 | 变更 |
|---|---|
| `frontend/src/types/routingMetrics.ts` | `LayerMetric.layer` 联合 `"L1" \| "L2" \| "L4"`（删 L3）；顶层 docstring 写明 3 层路由 + L3 已废弃注释 |
| `frontend/src/i18n/zh-CN.ts` | `routingMetrics.subtitle`、`routingMetrics.layer.L2/L3/L4` 改文案；删 `L3` 键 |
| `frontend/src/pages/RoutingMetricsPage.tsx` | `LAYER_COLORS` 删 L3；`buildLineOption` 删 L3 mock 数组 + series；顶部 docstring「4 个统计卡片」→「按后端实际返回动态渲染」 |
| `frontend/src/pages/__tests__/RoutingMetricsPage.test.tsx` | mock `layerDistribution` 删 L3；4 → 3 桶断言；新增 `queryByText("L3 多步链式推理").not.toBeInTheDocument()` 钉死回归 |
| 后端 Python 代码 | **不动** |

## 5. 实现要点

- **TDD**：测试 mock `layerDistribution` 已先按目标形态写好（删 L3 行），断言精确列举「L3 多步链式推理」**不应出现**（防回归）；
- **TS 联合类型严格性**：`Record<"L1" | "L2" | "L4", string>` 不留 any；折线图 `series` 同步删 L3 项，避免 `LAYER_COLORS.L3` 不可访问；
- **i18n 编译期注入**：前端镜像必须 `docker compose build --no-cache frontend`（memory 永久条目），docker cp 改不了；新 bundle hash 命中、旧 L3 字符串残留 = 0；
- **后端无代码改动**：本批纯前端，backend 容器无需重启。

## 6. 测试

| 层 | 范围 | 结果 |
|---|---|---|
| 前端 vitest | `RoutingMetricsPage.test.tsx` | **5 passed**（含负向断言 "L3 多步链式推理" 不再出现） |
| 前端 tsc | `tsc --noEmit` | **0 errors** |
| 后端 unit + services | 不变（本批无后端改动） | n/a |

## 7. 安全审查

无新攻击面，纯前端文案/类型/数据结构调整。

## 8. 部署验证（2026-09-27）

- **前端镜像重建**（非 docker cp）：
  ```
  docker compose build --no-cache frontend && docker compose up -d frontend
  ```
- **新 bundle 验证**：grep bundle 找 `"L3 多步链式推理"` = **0 命中**；`L2 LLM NL2SQL（含多步拆解）` = 1 命中；
- **后端无需 rebuild**：本批无 Python 改动；
- **网关**：`/api/v1/health` 直连 8000 与 nginx 5173 均 200（前端改动不影响后端）。

## 9. 关联

- commit：
  - `fix: 路由监控页 L3 真话化（routingMetrics 类型 + LAYER_COLORS + 折线图 mock + i18n + 测试 mock）`
- 评估文档：`Harness/wiki/chat-service-assessment.md` §2.5 L3 行 ✅关闭；§0 追加 `fix-routing-metrics-l3-truth` 批次段
- memory：登记 `qa-system-routing-metrics-l3-truth` 条目，包含「前端类型联合严格性 + L3 类型删除的不变量」「RoutingMetricsPage 是孤立页面（未注册 App.tsx）」
- 关联条目：
  - `qa-system-agent-vocabulary` / `qa-system-chat-service-assessment` — L3 层在 §15 残差 #2 已挂账，本批关闭
  - `qa-system-frontend-deploy-build-required` / `qa-system-docker-compose-tag` — i18n 编译期注入，必须 rebuild

## SSOT 校验清单

- [x] `routingMetrics.ts` 联合类型 `"L1" | "L2" | "L4"`（无 L3）
- [x] `LAYER_COLORS` 删 L3，键数 3
- [x] `buildLineOption` 删 L3 mock 数组 + series
- [x] `zh-CN.ts` 副标题 + L1/L2/L4 文案 + 删 L3 键
- [x] 顶部 docstring「按后端实际返回动态渲染」
- [x] 测试 mock `layerDistribution` 3 桶（无 L3）
- [x] 负向断言 `"L3 多步链式推理"` 不在 DOM
- [x] `npx tsc --noEmit` 0 errors
- [x] `npx vitest run` 5/5 passed
- [x] 后端代码零改动（grep `git diff --stat HEAD~1 backend/` 仅 docs 在 `Harness/wiki/`）
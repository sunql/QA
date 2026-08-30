# 变更：lineage 可视化（ECharts graph）— Phase 2.3

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 2（L3 数据治理 — 血缘）
- **状态**：done

## 1. 需求

前端血缘图可视化：基于已有 62 条 data_lineage 边，使用 ECharts graph 渲染层间拓扑结构，支持 7 层筛选 + 字段级/表级区分 + 边的 transformation 标注，使 AI 应用用户能直观查看「KPI → 表 → 字段」链路。

**验收标准**：
- 新页面 `/lineage` 列出全部活跃血缘边
- 7 层（SOURCE_SYSTEM → AI）默认全选，可独立勾选
- 节点按层着色（蓝/青/绿/紫/橙/红/品红）
- 边支持 active（实线）/ inactive（虚线）区分
- 字段级血缘（sourceField 非 NULL）渲染为 `object.field` 节点
- 边携带 transformationRule 作为 label
- **对象级筛选（Step 5）**：层过滤后按具体对象多选聚焦；候选带相连边数 `(count)`；同名对象跨层以复合键 `layer/object` 消歧（如 `SOURCE_SYSTEM/PORDER` vs `ODS/ODS_PORDER`）
- 真实数据：93 条边 → 37 个对象候选可视（21 SOURCE_SYSTEM + 10 ODS + 6 KPI）

## 2. 设计评审

**已与用户确认的关键决策**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 图渲染库 | ECharts（已有依赖 + ChatPage/UsagePage 复用经验） | 与 ChatPage ChartRenderer 同源；echarts-for-react 已封装；零新增依赖 |
| 节点 ID 策略 | 字段级：`object.field`；表级：`object` | 同对象不同字段各算独立节点（语义清晰：1 个 BPARTNER.FCY_0 节点 ≠ BPARTNER.BPRNUM 节点） |
| 层颜色 | SOURCE_SYSTEM=蓝 / ODS=青 / DWD=绿 / DWS=紫 / ADS=橙 / KPI=红 / AI=品红 | 与 7 层标准颜色对应；色盲友好（亮度递增） |
| 布局算法 | force（默认 ECharts） | 无需指定坐标；节点自动避让边；可拖拽 |
| 过滤粒度 | 层过滤 + 对象级多选（Step 5 补充） | 层过滤缩小到层后，再按具体对象（复合键 `layer/object`）聚焦；候选派生自层过滤后的 edges，随层筛选联动 |
| 空数据处理 | LineageGraph 返 null；LineagePage 显式 Empty 占位 | 与 Ant Design Empty 组件对齐 |
| 级联裁剪（Step 5） | `effectiveSelected` 纯派生，不做写回 | 取消某层 → 该层对象选择自动失效；重新勾选层 → 恢复该层先前选中的对象（粘性选择，纯派生实现，无 useEffect） |
| 节点点击 | 不做 Drawer（留 Phase 5） | 字段级边可显示 tooltip；本 Phase 暂不做节点详情 |

**多视角审视**：
- **后端视角**：复用 Phase 2.1 `listEdges(activeOnly=true)`；不新增后端接口
- **前端视角**：LineageGraph 内部 `edgesToGraphOption` 纯函数，单测友好；buildEChartsOption 封装 ECharts 细节
- **可测试视角**：vitest mock `echarts-for-react` 暴露 `data-testid` + 透传 option，便于断言节点/边数量
- **可访问视角**：每个 Layer Checkbox 带 `aria-label`（layer 名）；可用键盘 Tab 切换
- **不可变性视角**：`edgesToGraphOption` 输入不变，返回新 `{ nodes, links }`；不修改 LineageEdgeRead

## 3. 数据模型变更

**无新表 / 无新字段**：纯前端消费既有 `/api/v1/lineage/edges`。

## 4. 接口契约变更

**无 HTTP 接口变更**：复用 Phase 2.1 的 `GET /api/v1/lineage/edges?activeOnly=true`。

**前端类型**：复用 `LineageEdgeRead`（Phase 2.1 已建）。

**前端 API**：复用 `listEdges(filter)`（Phase 2.1 已建）。

**新增路由**：`/lineage` → `LineagePage`（App.tsx + AppLayout nav）。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `frontend/src/components/lineage/LineageGraph.tsx` | 新文件：`edgesToGraphOption` 纯函数 + `LineageGraph` 组件 + `LAYER_COLORS` 7 层颜色常量 + `GraphNode/GraphLink/GraphOption` 类型 |
| `frontend/src/components/lineage/LayerFilter.tsx` | 新文件：受控多选 Checkbox.Group + Tag 颜色预览 + `ALL_LAYERS` 7 层常量 + `layerColor` 辅助 |
| `frontend/src/pages/LineagePage.tsx` | 新文件：useEffect 拉 edges + LayerFilter 受控 + useMemo 过滤 + LineageGraph 渲染 + Empty/Spin/Alert 状态；Step 5 加 `selectedObjects` 状态 + 对象候选/级联裁剪派生 + ObjectFilter 渲染 |
| `frontend/src/components/lineage/lineageFilter.ts` | Step 5 新文件：纯函数 `ObjectCandidate` / `objectKey` / `collectObjectCandidates`（复合键去重 + 边数统计 + 层名排序 + 自环只计一次）/ `filterEdgesByObjects` |
| `frontend/src/components/lineage/ObjectFilter.tsx` | Step 5 新文件：antd Select 多选 + 按层 OptGroup 分组 + 搜索 + 可清空 + 无候选禁用 + `aria-label` |
| `frontend/src/App.tsx` | 注册 `/lineage` 路由 |
| `frontend/src/components/common/AppLayout.tsx` | 左侧导航加 `{ key: "/lineage", labelKey: "appLayout.menu.lineage" }` |
| `frontend/src/i18n/zh-CN.ts` | 加 `appLayout.menu.lineage = "数据血缘"` + `lineage.page.title / lineage.filter / lineage.empty` 命名空间 |
| `frontend/src/i18n/en-US.ts` | 同步英文文案 |
| `frontend/e2e/support/mockApi.ts` | 加 `MockLineageEdge` 接口 + 2 条种子边 + `handleLineageRoutes` 处理器 + 分发 |
| `frontend/e2e/lineage.spec.ts` | 新文件：2 个 Playwright 测试（页面渲染 + LayerFilter 过滤） |
| `frontend/src/tests/LineageGraph.test.tsx` | 11 vitest 测试（纯函数 + 组件） |
| `frontend/src/tests/LayerFilter.test.tsx` | 6 vitest 测试 |
| `frontend/src/tests/LineagePage.test.tsx` | 4 vitest 测试 |

**关键算法**（edgesToGraphOption）：
```ts
function nodeId(object, field) {
  return field ? `${object}.${field}` : object;
}
// 每条边生成 2 个节点 + 1 个 link；节点去重靠 Map
// 边：active=true 实线 / active=false 虚线
// 边 label：transformationRule 截断到 30 字
```

**关键不可变模式**：
- `edgesToGraphOption` 输入 edges 不变，返回新 `{ nodes, links }`
- LayerFilter 用 `new Set(value)` + `add/delete`，不修改原 Set
- LineagePage 用 `useMemo` 派生 `filteredEdges`，不修改 `edges` 数组

## 6. 测试

**vitest 单元 + 集成测试**（37 测试 PASS，含 Step 5 新增 16）：
- `LineageGraph.test.tsx`：11 测试
  - `edgesToGraphOption` 纯函数 8 例：空 / 单边 / 多字段 dedup / 表级 dedup / 层颜色 / active 虚实线 / transformationRule label
  - `LineageGraph` 组件 3 例：渲染 option / 空数据返 null / height 自定义
- `LayerFilter.test.tsx`：6 测试
  - 渲染 7 层 / 取消勾选 / 勾选 / ALL_LAYERS 常量顺序 / layerColor 不同色 / hex 格式
- `LineagePage.test.tsx`：7 测试（Step 5 新增 3）
  - mount 拉 edges + 渲染 graph / 失败 toast / 空数据 Empty / LayerFilter 过滤生效
  - Step 5：对象候选来自层过滤后的 edges（按层名排序）/ 选中对象收窄到触及边 / 取消层 → 该层对象选择被裁剪
- `ObjectFilter.test.tsx`（Step 5 新文件）：13 测试
  - 纯函数 10 例：objectKey 复合键格式 / 候选去重 + 计数 / 确定性排序 / 空输入 / 空选全保留 / source+target 匹配 / 复合键匹配 / 过期键丢弃 / 自环只计一次
  - 组件 3 例：渲染多选 / 无候选禁用 / 选中回调复合键 / 清空回调空集

**Playwright E2E**（2 测试 PASS）：
- `lineage.spec.ts`：
  - `打开 /lineage 页面渲染 graph 节点` — 验证 `<canvas>` 可见
  - `LayerFilter 取消勾选 SOURCE_SYSTEM 后过滤为 0 条边` — 验证 Empty 占位（filteredOut 文案已含「对象筛选」，正则前缀兼容）

**全量验证**：
- `npx vitest run`：307/307 PASS（含 Step 5 新增 16）
- `npx tsc --noEmit`：clean
- `npx playwright test e2e/lineage.spec.ts`：2/2 PASS
- `npx playwright test`（全量）：11 PASS / 3 pre-existing failure（chat/local-import/ontology strict-mode placeholder，与本 Phase 无关）

## 7. 安全审查

**触发场景**：读取 `/api/v1/lineage/edges` 数据 + ECharts graph 渲染。

**关键风险**：
- edges 数据含 `transformationRule` 等用户生成内容 → XSS 风险
- 解决：ECharts `label.formatter` 走 ECharts text rendering（非 innerHTML）；非字符串拼接，不引入 XSS 面

**Code reviewer 复审要点**：
- `LayerFilter.handleToggle` 是否正确 clone Set？— 是；`new Set(value)` + add/delete，不修改原 value
- `LineagePage` 拉取失败是否静默？— 否；用 `message.error` + `Alert` 双重提示
- `LineageGraph` 空数据返 null 是否会引起 React key warning？— 否；返回 null 不渲染任何 DOM

## 8. 部署验证

```bash
cd frontend

# vitest
npx vitest run src/tests/LineageGraph.test.tsx src/tests/LayerFilter.test.tsx src/tests/LineagePage.test.tsx
# → 21/21 PASS

# tsc
npx tsc --noEmit
# → clean

# E2E
npx playwright test e2e/lineage.spec.ts
# → 2/2 PASS
```

## 9. 真实数据验证（Harness 门禁）

按 Harness 规则「每轮真实数据验证」要求，端到端走真实 PG 5433 + 真实 FastAPI HTTP 链路 + 真实 ORM。

### 9.1 验证载体

**真实 API**：`GET /api/v1/lineage/edges?activeOnly=true`
**种子数据**：`seed_ontology.py` 跑完后 19 classes / 39 joins；`lineage_auto_extract.py` 抽取 62 条边。

### 9.2 验证结果（2026-08-30）

**HTTP API**：
```
GET /api/v1/lineage/edges?activeOnly=true → 200 + 62 条 LineageEdgeRead
Layer transitions:
  SOURCE_SYSTEM -> SOURCE_SYSTEM: 62
```

**可视化结构模拟**（用 edgesToGraphOption 算法离线计算）：
```
Total edges: 62
Distinct nodes after dedup: 72
Field-level edges: 62
Table-level edges: 0
Top target objects:
  FACILITY: 16 edges
  BPARTNER: 13 edges
  ITMMASTER: 12 edges
  PORDERQ: 6 edges
  PPRICFICH: 3 edges
```

**ECharts 渲染**：72 nodes + 62 links，force layout；7 层全选时单色（因为当前全部 SOURCE_SYSTEM）。

### 9.3 数据契约 Roundtrip 一致性

后端 `LineageEdgeRead` JSON → 前端 `LineageEdgeRead` interface → `edgesToGraphOption` → ECharts `series[0].nodes/links`，全程字段 1:1 对齐。`sourceField: null` 自动渲染为表级节点（symbolSize=40），非 NULL 渲染为字段级（symbolSize=28）。

### 9.4 Step 5 对象级筛选真实数据验证（2026-08-30）

**验证载体**：真实 `qa_metadata` 库（localhost:5433）93 条活跃 `data_lineage` 边 → 导出 TSV → 用 `vite-node` 执行真实 `frontend/src/components/lineage/lineageFilter.ts` 纯函数。

**结果**：
```
edges loaded: 93
total candidates: 37
composite keys unique: true                      # 37 个复合键无碰撞
SOURCE_SYSTEM/PORDER count: 14                   # 同名对象跨层消歧
ODS/ODS_PORDER count: 1
filtered by KPI/KPI_KPI_TOTAL_QTY: 1 edges       # 跨层收窄有效
edges touching any ODS object: 10                # 与 Step 4 的 10 条 SOURCE→ODS 边一致
cross-layer edges: 18                            # 10 SOURCE→ODS + 8 SOURCE→KPI
candidates per layer: KPI=6, ODS=10, SOURCE_SYSTEM=21
```

**验证结论**：对象候选覆盖 3 层 37 个对象，复合键消歧正确（`SOURCE_SYSTEM/PORDER` 与 `ODS/ODS_PORDER` 独立候选）；按真实对象过滤能精确收窄到触及边；层间边（SOURCE→ODS/KPI）在对象过滤下完整保留。

## 10. 关联

- 前置：
  - `feat-data-lineage-model`（Phase 2.1）— 提供了表 + service + HTTP API
  - `feat-lineage-auto-extract`（Phase 2.2）— 提供了 62 条真实边
- 计划：`/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` Phase 2.3
- 下一阶段：Phase 3 — `feat-entity-mapping-model`（跨系统编码映射）+ `feat-missing-business-objects`（8 个缺失业务对象建模）

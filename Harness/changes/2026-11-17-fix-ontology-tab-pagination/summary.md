# 变更：fix-ontology-tab-pagination

- **日期**：2026-11-17
- **作者**：Claude / 启琳
- **Phase**：本体管理（1109 契约）前端优化
- **状态**：done（前端纯改动，零迁移、零后端、零生产写）
- **迁移版本**：无
- **分支**：`feat/ontology-tab-pagination`（base `origin/main` = `a217fef0`）

---

## 1. 需求

本体管理页的**指标**与**语义关系**两个 Tab 没有分页，要求优化到与**类 / 属性 / 关联**一致。

## 2. 根因（实测，非推测）

五个 Tab 全部依赖 antd Table 的**默认内建分页**，不是仓库标准的 `useTablePagination`：

- antd 5 内建默认：`pageSize = 10`、**不显示总条数**、`pageSizeOptions` 只在 total > 50 时出现、**无快速跳页**。
- 实测本地 `qa_metadata`（5433）各 Tab 行数：类 77 / 属性 4182 / 指标 12 / 关联 86 / 语义关系 10。
  ⇒ 指标（12 条）与语义关系（10 条）在默认分页下**各自只有 1 页**，分页器退化 ⇒ 用户看到「没有分页」；
  而类 / 属性 / 关联 行数多，分页器是真实多页 ⇒ 看起来「这三个有分页」。

所以问题不是「指标/语义关系少配了分页」，而是**五个 Tab 用的分页规格本身就不一致**：
类/属性/关联在数据量大时碰巧显出多页，指标/语义关系因数据量小而显不出来。

## 3. 修法

统一到仓库标准 hook `frontend/src/utils/useTablePagination.ts`（与本体属性管理 / KPI 目录 / 数据域一致）：

| 能力 | 修前（antd 默认） | 修后（五个 Tab 统一） |
|---|---|---|
| 每页条数 | 10 | 20 |
| 总条数 | 不显示 | 显示「共 N 条」 |
| 每页条数选择 | 仅 total > 50 才出现 | 常显，10/20/50/100 |
| 快速跳页 | 无 | 有 |
| 过滤后回到第 1 页 | 无 | 有（保留用户选的每页条数） |

- 五个 Tab 组件全部改用 `useTablePagination()` 并传 `pagination={pagination}`。
- 过滤条件变化时调 `setPage(1)`（**不是** `reset()`：后者会把用户已选的每页条数一并打回 20）。
- `useTablePagination` 的 `reset` / `setPage` 改用 `useCallback` 固定身份 —— 否则消费方放进
  `useCallback` 依赖数组时每次渲染都是新函数，依赖永远变。

## 4. 测试（TDD）

| 文件 | 内容 |
|---|---|
| `src/tests/MetricTab.test.tsx` | 追加 3 例：25 条 ⇒ 显示「共 25 条」、第 1 页含第 11 条不含第 21 条（证明 pageSize=20 而非 antd 默认 10）、翻页、过滤后回到第 1 页 |
| `src/tests/SemanticRelationTab.test.tsx` | 追加 2 例：同上的分页与翻页断言 |
| `src/tests/ontologyTabPagination.test.ts` | 新建源码级守卫（6 例）：五个 Tab 组件都含 `useTablePagination` 与 `pagination={pagination}`；含「五组件都在」防空扫描假绿的反向守卫 |

**取证（防空绿）**：
- RED 已实测：10 failed / 28 passed，失败原因均为「找不到 共 25 条 / 指标21 / 关系21」，非笔误。
- 「过滤后回到第 1 页」用例单独反证：临时删掉 `setPage(1)` ⇒ 该用例失败（`Unable to find … 指标01`）⇒ 非空断言。
- 断言「共 25 条」+「第 11 行可见」是刻意选的：**只断言「第 21 行不可见」在 antd 默认 pageSize=10 下也会绿**，
  属假绿。

## 5. 验收证据

```
vitest run --coverage  →  Test Files 236 passed (236) / Tests 2417 passed (2417)
                          All files 84.14% | branches 83.08%（阈值 80%，CI 闸门通过）
tsc --noEmit            →  exit 0
```

（`eslint` 未在本仓安装，`npm run lint` 无法执行；CI 前端 job 只跑 typecheck + coverage。）

## 6. 明确未做的事

- 不改任何后端 / 接口契约 / 数据模型（纯前端）。
- 不改 `OntologyPage` 里搜索弹窗的 `pagination={{ pageSize: 10 }}`（语义不同：它是弹窗内的结果表，非 Tab 主表）。
- 不做服务端分页：五个 Tab 都是**一次性拉全量 + 前端过滤**（`ontologyFilter.ts`），客户端分页与现状一致。

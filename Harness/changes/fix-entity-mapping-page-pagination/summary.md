# 变更：fix-entity-mapping-page-pagination

- **日期**：2026-10-24
- **Phase**：done（本地全绿；生产待部署）
- **触发**：生产 entity_mapping 同步 354,422 行后，编码映射页面只显示 200 条

## 1. 现象与根因

生产跑 `sync_entity_mapping_from_thbi.py` 成功写入 354,422 行
（`affected=354422`），但编码映射页只有 200 条。

根因**不是数据丢失**：`GET /entity-mappings` 的 `limit` 默认 200
（`entity_mapping.py:51`），前端 `EntityMappingPage.refresh()` 只传
entityType 过滤、不传 limit，也没有分页控件 —— 页面永远显示按 id 排序的
前 200 行。dev 同样如此（35 万行同样只显示 200），只是没人数过。

## 2. 设计

| 方案 | 结论 |
|---|---|
| A. 页面加「显示前 200 条」提示 | 治标，35 万行下搜索优先体验差 |
| B. 服务端分页（limit/offset + total 信封） | **采纳**，范式与 AuditLogPage 一致（feat-audit-history-api Task 1） |

- 后端：响应从裸 `list[EntityMappingRead]` 改为 `EntityMappingPage {rows, total}`；
  service 新增 `countMappings`（与 listMappings 同一套过滤语义）
- 前端：`useTablePagination` 扩展可选 `serverTotal`（不传 = 原客户端分页，
  既有页面不受影响）；页面 effect 随 current/pageSize/filterType 重拉；
  切筛选回第 1 页；删除后落空页弹回第 1 页

## 3. 注意

- **信封是破坏性契约变更**：backend 与 frontend 必须同批部署
- 分页条（含每页条数下拉）在显式 total 后常驻渲染 → 依赖全局 combobox
  下标的测试要改按弹窗作用域取（EntityMappingPage.test 已改）
- AutoComplete / searchMappings（上限 100）不受影响

## 4. 验收

- 后端：test_entity_mapping_api.py **27/27 绿**（含新增
  test_list_pagination_rows_and_total：total 独立于窗口 + 跟随过滤）
- 前端：tsc 0 错；EntityMappingPage 6/6 + entityMappingApi 7/7 + pages 5/5 绿

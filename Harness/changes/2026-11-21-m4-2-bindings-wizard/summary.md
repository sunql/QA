# 变更：M4-2 绑定向导（admin 管理台，消费 M1-1.1 的 10 条路由）

- 日期：2026-11-21　分支：`feat/m5-1-rfq-draft`（随 #170 累积；前端与采购线后端合并无耦合）

## 内容（frontend/src）
- `api/procurementBindings.ts`：绑定 API 客户端（list/detail/create source/property/
  confirm source/property/expire——**confirm = 版本冻结点**，前端入口 Popconfirm 警示）；
- `pages/ProcurementBindingsPage.tsx`：
  - 列表三态（候选/已确认/已失效）+ 属性绑定数；
  - **向导 Steps 3 步**：类与源（ontology classes / datasources picker）→ 属性映射
    （property → source_column 逐条，留空 = 已知限制）→ 创建（候选态）；
  - 详情抽屉：属性绑定确认（逐条）+ **源绑定确认**（候选属性未全确认 ⇒ Alert
    警示 fail-closed 提示；Popconfirm 展示冻结点文案）+ 失效；
  - 管理台 = 后端 requireAdmin（页面不判角色）。
- 路由 `procurement/bindings` + i18n `procurement.bindings` 段（zh/en）。

## 测试
3 用例（三态渲染 / D-3 语义：候选警示 + 冻结点 Popconfirm / 终态无确认按钮）
+ RFQ/i18n 回归 **31 passed**；tsc 全清。

## 坑入档（i18n 嵌套）
新 section 插入 zh/en 时落在**顶层**（`bindings:` ≠ `procurement.bindings`）⇒
页面全裸键。用 node+ts.transpile 直接验证解析后的嵌套结构（procurement keys），
别只看语法 parse。

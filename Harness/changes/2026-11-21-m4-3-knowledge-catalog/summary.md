# 变更：M4-3 知识检索页 + 场景目录页（M4 收尾批）

- 日期：2026-11-21　分支：`feat/m5-1-rfq-draft`（随 #170 累积）

## 内容（frontend/src）
- `pages/ProcurementKnowledgePage.tsx`：四通道 Tab（统一查询/实体解析/受控邻接/
  文档检索）——证据卡片渲染 **M2-1 契约全字段**（channel/confidence/security/
  sourceRef/validAt/releaseVersion），通道降级 Alert 自证；
- `pages/ProcurementAgentCatalogPage.tsx`：场景目录展示（runnable Tag + 工具能力 +
  **不暴露 handlerRef**，DOM 断言钉死）；
- `api/procurementKnowledge.ts`：四端点 + catalog 客户端；
- 路由 `procurement/knowledge` + `procurement/agents`。

## i18n 重建
M4-2/M4-3 的 knowledge/catalog 段曾误落 bindings 内/顶层（同款嵌套陷阱）⇒
**zh-CN.ts / en-US.ts 从干净 JSON 整体重生成**（node+ts.transpile 导出→JSON 操作
→重新写入），根除手工插入的嵌套错误。

## 测试
4 新用例 + 全部前端回归 **28 passed**；tsc 全清；ruff 净新增 0。

## M4 里程碑完成（M4-1/2/3 全落地）
- M4-1 询价草稿页（消费 M5 API 全套）
- M4-2 绑定向导（消费 M1-1.1 路由，admin 面）
- M4-3 知识检索页 + 场景目录页（消费 M2 四端点 + M3-3 catalog）
- 菜单行（DB）= 运营步骤；DRAFT_WRITE 角色矩阵前端呈现 = 后续批

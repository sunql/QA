# 变更：M4-1 询价草稿页（前端，消费 M5 API 全套）

- 日期：2026-11-21　分支：`feat/m5-1-rfq-draft`（随 #170 累积）

## 内容（frontend/src）
- `api/procurementRfq.ts`：RFQ API 客户端——create 携 `Idempotency-Key`
  （crypto.randomUUID），201 新建 / 200 幂等重放契约；
- `pages/ProcurementRfqPage.tsx`：列表（状态色）+ 创建 Modal + 详情抽屉
  （**决策链表 = DoD 4 可视化**）+ **按 status 裁剪动作入口**
  （DRAFT：validate/cancel；VALIDATING：submit；PENDING_APPROVAL：approve/reject；
  **D-4：无发送入口**，测试钉死）；自批禁止由服务端裁决（前端不二次过滤）；
- 路由 `procurement/rfq` + i18n `procurement.rfq` 段（zh/en 同键，types 自动推导）。

## 测试
页面测试 3 用例（列表渲染 / D-4 负向 / PENDING_APPROVAL 审批动作）+ i18n 回归
⇒ 28 passed；**tsc 全清（0 错误）**。

## 坑入档（前端三件）
1. **antd 两字按钮自动插空格**（「详 情」「通 过」）⇒ 测试断言用 `/详\s*情/` regex；
2. **ConfigProvider locale = antd/locale/zh_CN**（i18n 字典不是 antd locale——
   传错 ⇒ TS2741 locale 缺失）；
3. 页面测试 i18n 实例勿重复 init（重置 resources ⇒ 键全部解析失败）。

## M4 剩余（后续批）
绑定向导（M1-1.1 的 admin 面）、场景目录消费页（M3-3 catalog）、知识检索消费页
（M2 四端点）、DRAFT_WRITE 角色矩阵前端呈现。菜单行（DB）= 运营步骤。

# M6-1 设计：ERP/SRM 命令端口 + 模拟器（真实对接留外部窗口）

- 日期：2026-11-21　分支（拟）：`feat/m6-1-erp-srm-port`（off main @ `b53ba5da`）

## 范围（计划 5.4 的 M6 半边 + 外部对接的前置）

M5 已交付 DRAFT-only（D-4）：草稿可建可审，但**不发送**。M6-1 补上「发送」这条
**外部副作用**路径的**端口与模拟器**——真实 ERP/SRM 账号对接是外部阻塞（需 C 问卷
账号 + 网络），本批只交付可替换的端口边界。

### 1. 端口协议 `ProcurementCommandPort`（Protocol，纯接口）
- `sendRfq(draft, idempotencyKey)` → `RfqSendResult`（外部引用 + 状态）
- `querySendStatus(externalRef)` → 状态回写用
- **PROHIBITED_AUTO 动作在此不可达**：`autoAward` / `signContract` / `payInvoice`
  **不提供方法** ⇒ 代码层不可达（设计稿 §6.6「首期禁止 Agent 自动执行」）。

### 2. 模拟器实现 `SimulatedErpSrmAdapter`（默认注入）
- 幂等：同 idempotencyKey ⇒ 返回**同一** externalRef（不重复发）；
- 不产生真实副作用；返回可辨识的 `SIM-<hash>` 外部引用；
- `querySendStatus` 恒返回 SENT（模拟即时成功）。

### 3. 状态机扩展 `READY_TO_SEND → SENDING → SENT`
- M5 的 DRAFT-only 矩阵里 `READY_TO_SEND` 无出口 ⇒ 本批**加 SENDING 出口**，
  但**只在端口注入且 M6 显式启用时**可达（默认端口 = 模拟器 + 默认关闭）。
- **不改变 M5 的「未审批不能发送」**：发送前置 = 已审批（READY_TO_SEND 态）
  + 幂等键 + 二次确认（API 层 `confirm=true`）。

### 4. 编排：`sendRfq` 落 business_command（复用 P5 表）+ outbox 事件
- 幂等语义复用 `business_command.payload_hash`（M5-1 已加列）。
- **不新建表**（准则 H）。

## 明确不做
- 真实 ERP/SRM 适配器（外部账号未到位）；
- 自动定标/签署/付款（PROHIBITED_AUTO，代码层不可达）；
- 询价草稿的 ERP 主数据回写（M6-2 之后按实际接口定）。

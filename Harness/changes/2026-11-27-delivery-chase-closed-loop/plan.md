---
created: 2026-11-27
updated: 2026-11-27
---

# 方案：到货跟踪「催货单」闭环（从 Copilot 选行到按供应商催货）

- **分支**：`feat/delivery-chase-closed-loop`（待建）
- **需求**（用户 2026-11-27）：采购 Copilot · 到货跟踪，对**未到货**的订单行，
  选择需要催的行，**按供应商**创建催货单，形成业务闭环。

## 1. 现状盘点（可复用的资产）

| 资产 | 位置 | 复用方式 |
|---|---|---|
| 未到货明细行 | `pending_delivery_rows.camelPendingRow`（`poNo/poLine/materialCode/supplierCode/orderedQty/receivedQty/openQty/expectedDate/overdueDays`） | 催货行的**数据源** |
| 询价草稿全链路 | `procurement_rfq_draft`（10 态状态机）+ `procurement_rfq_decision`（决策留痕）+ outbox（`rfq.%` 事件，消费者已跑） | **闭环骨架直接复用** —— 催货单 = 一种「向供应商发出的单据」，与询价单同构 |
| 外发端口 | `ProcurementCommandPort.sendRfq` + `SimulatedErpSrmAdapter`（幂等、确定性外部引用、零真实副作用） | 催货发送走**同一端口模式** |
| 供应商存在性校验 | `entity_mapping(SUPPLIER)`：`enterprise_code`（`Q1630`）↔ `enterprise_key`（81324035971） | 待到货行的 `supplierCode` = ERP 供应商码（如 `Q1630`）⇒ 经 `entity_mapping` 解析成 `enterprise_key` |
| 权限/限流/审计 | `requireAdmin` / `rateLimitDep` / `audit_service` | 照抄 |
| 前端 | `ProcurementRfqPage`（列表/详情/决策时间线）+ Copilot `PendingDeliveryResultTabs` | 催货单页照抄询价页形态 |

**结论：不用发明新机制。催货单 = 询价链路的「同构兄弟」，只换业务语义。**

## 2. 设计

### 2.1 数据模型（新表，迁移 0172）

```
procurement_chase_draft（催货单草稿，10 态状态机与 RFQ 相同）
  id, chase_draft_id(uuid), version, status, request_id,
  supplier_enterprise_key(bigint, NOT NULL),     -- 一个催货单 = 一个供应商
  supplier_code(varchar100),                      -- ERP 码（Q1630，冗余可读）
  expected_reply_deadline(timestamptz, NOT NULL), -- 期望供应商答复期限
  notes(text), payload_json(jsonb, NOT NULL), payload_hash, idempotency_key,
  created_by, created_time, updated_time

procurement_chase_line（催货行：来自待到货明细的选定行）
  id, chase_draft_id(uuid FK CASCADE), po_no, po_line_no,
  material_code, material_desc, ordered_qty, received_qty, open_qty,
  expected_date(date), overdue_days(int), created_time

procurement_chase_decision（决策留痕，与 RFQ decision 同构）
  id, chase_draft_id(uuid FK CASCADE), decision(SUBMIT/APPROVE/REJECT/CANCEL/SEND),
  actor, note, decided_time, created_time
```

**为什么不用 RFQ 表加个 kind 列**：RFQ 的 payload 契约（`materialKey+quantity+unit+deliveryDate`）
与催货行（`poNo+poLine+openQty+overdueDays`）**语义不同**；混表会让 `payload_json`
的双契约校验变成 if/else 分叉（违反 KISS），且状态机语义（询价的「发送后收报价」vs
催货的「发送后等答复」）后续会分叉。分表 = 各自简单。

### 2.2 状态机（复用 RFQ 的 10 态）

```
DRAFT → VALIDATING → PENDING_APPROVAL → READY_TO_SEND → SENDING → SENT
              ↘ NEEDS_CORRECTION / REJECTED / CANCELLED        ↘ FAILED
```

四眼审核（submit→approve→send）保留：**催货是对外动作**（把欠交事实告知供应商），
与询价同级别，不该一键直发。

### 2.3 后端 API（`/api/v1/procurement/chase`，admin）

```
POST   /chase/drafts                      创建（幂等；body: {supplierCode, lines[], deadline, notes}）
GET    /chase/drafts                      列表
GET    /chase/drafts/{id}                 详情（含 lines + decisions）
POST   /chase/drafts/{id}/validate        → NEEDS_CORRECTION | READY_TO_SEND
POST   /chase/drafts/{id}/submit-approval → PENDING_APPROVAL
POST   /chase/drafts/{id}/approve|reject|cancel
POST   /chase/drafts/{id}/send            → SENDING → SENT（经 command port，outbox 留痕）
```

**`POST /chase/drafts/from-pending`**（核心入口）：body = Copilot 选中的待到货行
`[{poNo, poLine}]` + deadline。服务端**不信前端带来的数量** —— 按 `poNo+poLine`
回查最新 open_qty/expectedDate（防篡改、防陈旧），**按 supplierCode 分组**生成
**多张催货单**（一个供应商一张，同 batch id 关联），返回 `[draft...]`。

### 2.4 外发端口

`ProcurementCommandPort` 加 `sendChase(draft, *, idempotencyKey)`；
`SimulatedErpSrmAdapter.sendChase` 同模式（确定性外部引用 `CHASE-<hash>`，零副作用）。
将来接真实 SRM 只换 adapter。

### 2.5 前端（两处）

1. **Copilot 到货跟踪卡片**（`DeliveryTrackingResultCard`）：未到货明细行加
   **行选择 checkbox** + 底部「**按供应商创建催货单**」按钮 →
   `POST /chase/drafts/from-pending` → 提示「已创建 N 张催货单（M 个供应商）」
   并给出跳转链接。
2. **催货单管理页**（`ProcurementChasePage`，照抄 `ProcurementRfqPage`）：
   列表/详情/决策时间线/validate→submit→approve→send 操作链。

## 3. 边界与硬约束（照抄既有规则）

- **只读红线不破**：`from-pending` 的回查走 SQL Guard 只读管线；催货单本身写
  元数据库（与 RFQ 同库），**不写业务库**。
- **THBI 硬规则**：回查走 `procurement_source_binding` 解析（`DWD_PURCHASE_ORDER_DTL`），
  不接受调用方指定数据源。
- **幂等**：`idempotency_key = sha256(supplier + sorted(lines) + deadline + batchKey)`；
  同 key 同 payload 返回既有（Copilot 重试安全），异 payload 409。
- **供应商必须在 entity_mapping**（复用 RFQ 的校验口径）；解析不到 ⇒ 该行**剔除**
  并在响应里点名（不炸整批）。

## 4. 排期（半天）

| 步骤 | 内容 |
|---|---|
| 1 | 迁移 0172（3 张表）+ ORM + F1 守卫登记 |
| 2 | `chase_service`（create/from-pending/validate/decide/send，复用状态机模块） |
| 3 | API 路由 + auth 守卫登记（route-auth-guard）+ audit |
| 4 | command port 扩展 + outbox 事件 `chase.sent` |
| 5 | 前端：卡片选行 + 催货单管理页 + i18n |
| 6 | 单测/集成测试 + e2e |

## 4.0 口径修正（用户 2026-11-27 实测后）

**所有订单行都可以下催单 —— 不管是不是脏数据、不管是不是部分到货。**

初版有三处限制与之冲突，全部拆掉：

| 位置 | 初版限制 | 修正 |
|---|---|---|
| 前端 `DeliveryTrackingResultCard` `getCheckboxProps` | 非 `notArrived` 或脏行 ⇒ checkbox 禁用 | **不禁用**（任何行都可勾） |
| 前端 `createChases` | 只提交 `notArrived` 且非脏行的行 | **提交全部勾选的行** |
| 后端 `procurement_chase_lookup` 回查 WHERE | `_openQtyCond`（`openQty > 0 AND openQty < 脏阈值`）⇒ 脏行与已到货行被**静默滤掉** | **只按身份对过滤**；`openQty` 仍算出来写进快照（供人核对），但不当筛选条件 |

> 后端那处最隐蔽：前端把行勾上、提交了，服务端回查却查不到 ⇒ 用户看到
> 「跳过了 N 行」而不知为何。口径统一后前后端一致。

## 4.0.1 欠交量异常 ⇒ 红色标识（用户 2026-11-27 要求）

「所有行都能催」不等于「所有行都正常」。欠交量 **≥ `OPEN_QTY_SANITY_MAX`（1,000,000）**
的行（多半是源侧录入错误，如 150 万）**必须带红色标识、要求重点核对** —— 催出去会把
错值带给供应商。

| 层 | 改动 |
|---|---|
| 迁移 0173 | `procurement_chase_line` 加 `is_dirty`（bool, not null, default false） |
| 服务层 | `_isDirtyQty()` 按**共用常量** `procurement_query_utils.OPEN_QTY_SANITY_MAX` 判定；开单时**冻结**到行表 + 写进外发快照（`lines[].isDirty`） |
| 前端 | 催货单详情：异常行红色 Tag「数量异常·请重点核对」；正常行绿色 Tag「正常」；表头显示「N 行数量异常，请重点核对」 |

⚠️ 阈值**必须复用** `OPEN_QTY_SANITY_MAX`（待到货/到货跟踪/催货三处共用同一常量）——
各写一份会让「同口径」慢慢漂移。

## 4.1 落地状态（2026-11-27）

- ✅ 迁移 0172（三张表）+ ORM + models.py 登记
- ✅ 状态机（复制矩阵，刻意解耦）+ 命令端口 sendChase + 模拟器
- ✅ 服务层（from-pending 回查分组 / 幂等 / validate / approve / reject / cancel / send）
- ✅ API 路由（requireAdmin + rateLimit；非法迁移 ⇒ 409 ConflictError）
- ✅ 治理守卫登记（6 写 + 2 admin 读）
- ✅ 前端：Copilot 到货跟踪卡选行（行级 checkbox，只允许 notArrived 非脏行）+
  「按供应商创建催货单」+ 催货单管理页（procurement/chase 路由）+ i18n
- ✅ 测试：后端 unit 10 + integration 8 + 治理守卫 51；前端 api 4 例
- **业务拍板**：① 审批链路保留不做开关；② SENT 即终态（答复回流二期）；
  ③ 自动升级不做（二期）

## 5. 待确认（业务输入）

1. 催货单**要不要审批**（四眼），还是采购员可直接发？—— 本方案默认保留四眼
   （与询价一致）；要直发就把 approve 步骤做成可配置开关。
2. 供应商**答复渠道**：现阶段只落「已发送」状态（模拟器）；真实答复回流是二期。
3. 超期未答复要不要**自动升级**（再次催告/抄送上级）—— 二期。

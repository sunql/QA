# M5-1 设计：询价草稿数据面（DRAFT-only）

- 日期：2026-11-21　分支（拟）：`feat/m5-1-rfq-draft`（off main @ `a579d09a`）

## 范围（对应计划 5.1 + 5.2 的草稿半边；submit-approval/审批闭环 = M5-2）

1. **状态机矩阵扩展**（复用 `workflow_state_machine.py`，不新建引擎——计划 5.1 原文）：
   新增 `rfq` kind：DRAFT→VALIDATING→{PENDING_APPROVAL | NEEDS_CORRECTION}；
   NEEDS_CORRECTION→{VALIDATING, CANCELLED}；PENDING_APPROVAL→{READY_TO_SEND, REJECTED}；
   READY_TO_SEND→{SENDING, CANCELLED}；SENDING→SENT→CLOSED。
   **D-4（DRAFT-only）**：SENDING/SENT/CLOSED 在 M5 **不可达**（矩阵形态保留，
   M6 接外部系统时启用）——负向用例钉死。
2. **迁移 0160**（main 线 head=0152；**0153–0159 归采购线 b1–b4 + A4-0 预留**，
   本迁移显式让位 ⇒ 合并顺序 = b4 之后）：建 `procurement_rfq_draft` +
   `business_command` 补 `payload_hash` 列（幂等语义需要：同 key 不同 payload ⇒ 409）。
3. **表 `procurement_rfq_draft`**：rfq_draft_id UUID 唯一 / version（乐观锁）/
   status / request_id（业务请购单号）/ deadline / notes / payload_json
   （supplier_keys + line_items）/ payload_hash / idempotency_key 唯一 / 治理列。
4. **幂等语义**（DoD 3）：`Idempotency-Key` 头 + payload SHA-256；
   同 key 同 hash ⇒ 返回既有草稿；同 key 异 hash ⇒ 409。
5. **validate**（草稿态校验）：必填字段 + 供应商存在性（entity_mapping）+
   数量/日期合法性 ⇒ 不通过转 NEEDS_CORRECTION（状态机），通过留 DRAFT
   （提交审批在 M5-2）。
6. **API**（`/api/v1/procurement/rfqs/*`，WORKFLOW 开关组内）：POST drafts /
   GET drafts / GET drafts/{id} / POST drafts/{id}/validate / POST drafts/{id}/cancel。
   submit-approval M5-2。
7. **权限**：登录（getCurrentUser）+ created_by 落库；DRAFT_WRITE 角色闭环
   （计划 5.3，P5 遗留项）= M5-2。

## 明确不做（本批边界）
- 审批闭环/业务命令创建（M5-2）；ERP/SRM 端口与模拟器（计划 5.4，M5-3）；
- outbox 消费者（计划 5.4）；比价规则 version（计划 5.5）。

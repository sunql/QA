/**
 * M4 权限矩阵（设计稿 §6.6 四级）单元守卫。
 *
 * 判据要点：
 * 1. **审批动作需审批角色**（admin / procurement-approver）；无角色 ⇒ 不出按钮
 *    （前端入口裁剪；服务端仍是最终裁决方——后端角色闸在 decideApproval）；
 * 2. **草稿类动作不拦**（当前后端 createDraft/validate/submit/cancel 未设角色闸
 *    ⇒ 前端同口径，避免「前端不让点、后端其实允许」的错位）；
 * 3. **PROHIBITED_AUTO 首期恒不可达**（代码层无入口）；
 * 4. 状态 → 动作映射与后端状态机一致（DRAFT→validate/cancel；
 *    VALIDATING→submit；PENDING_APPROVAL→approve/reject）。
 */
import { describe, expect, it } from "vitest";
import {
  availableRfqActions,
  resolveProcurementPermissions,
  selfApprovalHintKey,
} from "../utils/procurementPermissions";

describe("采购权限矩阵（设计稿 §6.6）", () => {
  it("无角色 ⇒ 可读可草稿、不可审批", () => {
    const p = resolveProcurementPermissions([]);
    expect(p.canRead).toBe(true);
    expect(p.canDraft).toBe(true);
    expect(p.canCancel).toBe(true);
    expect(p.canApprove).toBe(false);
  });

  it("procurement-approver ⇒ 可审批", () => {
    expect(resolveProcurementPermissions(["procurement-approver"]).canApprove).toBe(
      true,
    );
  });

  it("admin ⇒ 可审批（超管旁路，与后端白名单同源）", () => {
    expect(resolveProcurementPermissions(["admin"]).canApprove).toBe(true);
  });

  it("undefined 角色（未登录/加载中）⇒ 不可审批、不崩", () => {
    const p = resolveProcurementPermissions(undefined);
    expect(p.canApprove).toBe(false);
    expect(p.canRead).toBe(true);
  });

  it("PROHIBITED_AUTO 首期恒不可达", () => {
    const p = resolveProcurementPermissions(["admin"]);
    expect(p.canProhibitedAuto).toBe(false);
  });

  it("状态 → 动作映射", () => {
    const approver = resolveProcurementPermissions(["procurement-approver"]);
    const plain = resolveProcurementPermissions(["procurement"]);
    expect(availableRfqActions("DRAFT", plain, false)).toEqual([
      "validate",
      "cancel",
    ]);
    expect(availableRfqActions("VALIDATING", plain, false)).toEqual(["submit"]);
    expect(availableRfqActions("PENDING_APPROVAL", approver, false)).toEqual([
      "approve",
      "reject",
    ]);
    // 无审批角色 ⇒ PENDING_APPROVAL 不出审批按钮
    expect(availableRfqActions("PENDING_APPROVAL", plain, false)).toEqual([]);
    // 终态 ⇒ 无动作
    expect(availableRfqActions("REJECTED", approver, false)).toEqual([]);
    expect(availableRfqActions("CLOSED", approver, false)).toEqual([]);
  });

  it("自批提示：仅创建人自己是创建人时给提示键", () => {
    expect(selfApprovalHintKey(true)).toBe("procurement.rfq.selfApprovalHint");
    expect(selfApprovalHintKey(false)).toBeNull();
  });
});

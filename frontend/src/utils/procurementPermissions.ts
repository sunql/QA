/**
 * M4 权限矩阵（设计稿 §6.6 采购写操作权限分级）—— 前端呈现层。
 *
 * 四级（后端为准，前端只做**入口裁剪**，不替代服务端裁决）：
 * - READ          登录 + 数据 ACL + 审计；
 * - DRAFT_WRITE   业务角色授权 + 参数校验 + **不产生外部副作用**；
 * - APPROVED_WRITE 审批完成 + 当前用户二次确认 + 幂等键 + 外部接口重校验；
 * - PROHIBITED_AUTO 自动定标/合同签署/付款/绕过 ERP 审批 —— **首期禁止**，
 *   本模块**不提供**任何入口（代码层不可达）。
 *
 * 角色码与后端白名单同源（`procurement_rfq_service._APPROVER_ROLES`）：
 * `admin`（超管）/ `procurement-approver`（采购审批）/ `procurement-drafter`（草稿）。
 * ⚠️ 后端当前只对**审批决策**设了角色闸（admin / procurement-approver）；
 * 草稿创建（DRAFT_WRITE）后端不限角色 ⇒ 前端**同样不拦**（否则出现「前端不让点、
 * 后端其实允许」的错位）。新增角色闸时两侧必须同步改。
 */

/** 采购域角色码（与后端白名单同源）。 */
export const PROCUREMENT_ROLES = {
  approver: "procurement-approver",
  drafter: "procurement-drafter",
  admin: "admin",
} as const;

/** 采购写操作四级（设计稿 §6.6）。 */
export type ProcurementActionLevel =
  | "READ"
  | "DRAFT_WRITE"
  | "APPROVED_WRITE"
  | "PROHIBITED_AUTO";

export interface ProcurementPermissions {
  /** 可读（登录即可）。 */
  canRead: boolean;
  /** 可生成/编辑草稿（当前后端不限角色 ⇒ 与后端一致：登录即可）。 */
  canDraft: boolean;
  /** 可做审批决策（approve/reject）——需审批角色。 */
  canApprove: boolean;
  /** 可取消草稿（当前后端不限角色 ⇒ 登录即可）。 */
  canCancel: boolean;
  /** 可发送（**外部副作用** ⇒ 后端 sendRfq 要求审批角色，前端同口径）。 */
  canSend: boolean;
  /** 首期恒 false：自动定标/签署/付款在代码层不可达。 */
  canProhibitedAuto: false;
}

/** 角色集合是否含给定角色码。 */
function hasRole(
  roles: readonly string[] | undefined | null,
  role: string,
): boolean {
  return (roles ?? []).includes(role);
}

/**
 * 按当前用户角色解析采购权限矩阵。
 *
 * @param roles 当前用户的角色码列表（来自 `authStore.user.roles`；未登录 = 空）
 */
export function resolveProcurementPermissions(
  roles: readonly string[] | undefined | null,
): ProcurementPermissions {
  const isApprover =
    hasRole(roles, PROCUREMENT_ROLES.approver) ||
    hasRole(roles, PROCUREMENT_ROLES.admin);
  return {
    canRead: true,
    // 后端 createDraft/validate/submit/cancel 未设角色闸 ⇒ 前端同口径不拦
    canDraft: true,
    canApprove: isApprover,
    canCancel: true,
    // 发送是外部副作用（写 ERP/SRM）⇒ 与审批同白名单（后端 sendRfq 调 _assertApproverRoles）
    canSend: isApprover,
    canProhibitedAuto: false,
  };
}

/**
 * 某草稿状态下的可用动作（前端裁剪）。
 *
 * @param status 草稿状态
 * @param permissions 当前用户权限
 * @param isCreator 当前用户是否草稿创建人（自批禁止的前端提示）
 */
export function availableRfqActions(
  status: string,
  permissions: ProcurementPermissions,
  isCreator: boolean,
): string[] {
  const actions: string[] = [];
  switch (status) {
    case "DRAFT":
      if (permissions.canDraft) actions.push("validate");
      if (permissions.canCancel) actions.push("cancel");
      break;
    case "VALIDATING":
      if (permissions.canDraft) actions.push("submit");
      break;
    case "PENDING_APPROVAL":
      // 审批动作需审批角色；创建人自己是创建人时仍显示（服务端会拒自批 ⇒ 前端提示）
      if (permissions.canApprove) actions.push("approve", "reject");
      break;
    case "READY_TO_SEND":
      // M6-1：发送出口（服务端要求 已审批 + 二次确认 + 幂等键 + 审批角色）
      if (permissions.canSend) actions.push("send");
      if (permissions.canCancel) actions.push("cancel");
      break;
    default:
      break;
  }
  void isCreator;
  return actions;
}

/** 自批提示文案键（前端提示，服务端仍是最终裁决方）。 */
export function selfApprovalHintKey(isCreator: boolean): string | null {
  return isCreator ? "procurement.rfq.selfApprovalHint" : null;
}

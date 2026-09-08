import { httpClient } from "./client";
import type {
  RoleRow,
  RoleCreatePayload,
  RoleUpdatePayload,
  MenuCodesUpdatePayload,
  SubjectPermissions,
} from "../types/rbac";

const PREFIX = "/roles";

export async function listRoles(): Promise<RoleRow[]> {
  const res = await httpClient.get<RoleRow[]>(PREFIX);
  return res.data;
}

export async function createRole(payload: RoleCreatePayload): Promise<RoleRow> {
  const res = await httpClient.post<RoleRow>(PREFIX, payload);
  return res.data;
}

export async function updateRole(
  roleId: number,
  payload: RoleUpdatePayload,
): Promise<RoleRow> {
  const res = await httpClient.put<RoleRow>(`${PREFIX}/${roleId}`, payload);
  return res.data;
}

export async function deleteRole(roleId: number): Promise<void> {
  await httpClient.delete(`${PREFIX}/${roleId}`);
}

/** set-replace：角色直接授权的菜单 code 全量。 */
export async function setRolePermissions(
  roleId: number,
  payload: MenuCodesUpdatePayload,
): Promise<void> {
  await httpClient.put(`${PREFIX}/${roleId}/permissions`, payload);
}

/** 角色维度直接授权视图（需求 #4）。 */
export async function getRolePermissions(
  roleId: number,
): Promise<SubjectPermissions> {
  const res = await httpClient.get<SubjectPermissions>(
    `${PREFIX}/${roleId}/permissions`,
  );
  return res.data;
}

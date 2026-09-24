import { httpClient } from "./client";
import type {
  UserEffectivePermissions,
  UserRow,
  UserCreatePayload,
  UserUpdatePayload,
  RoleIdsUpdatePayload,
  OrganizationIdsUpdatePayload,
  MenuCodesUpdatePayload,
  AdminResetPasswordPayload,
} from "../types/rbac";

const PREFIX = "/users";

export async function listUsers(): Promise<UserRow[]> {
  const res = await httpClient.get<UserRow[]>(PREFIX);
  return res.data;
}

export async function createUser(payload: UserCreatePayload): Promise<UserRow> {
  const res = await httpClient.post<UserRow>(PREFIX, payload);
  return res.data;
}

export async function updateUser(
  userId: number,
  payload: UserUpdatePayload,
): Promise<UserRow> {
  const res = await httpClient.put<UserRow>(`${PREFIX}/${userId}`, payload);
  return res.data;
}

export async function deleteUser(userId: number): Promise<void> {
  await httpClient.delete(`${PREFIX}/${userId}`);
}

/** set-replace：用户持有的角色 ID 全量。 */
export async function setUserRoles(
  userId: number,
  payload: RoleIdsUpdatePayload,
): Promise<void> {
  await httpClient.put(`${PREFIX}/${userId}/roles`, payload);
}

/** set-replace：用户所属组织 ID 全量。 */
export async function setUserOrganizations(
  userId: number,
  payload: OrganizationIdsUpdatePayload,
): Promise<void> {
  await httpClient.put(`${PREFIX}/${userId}/organizations`, payload);
}

/** set-replace：用户直接授权的菜单 code 全量。 */
export async function setUserPermissions(
  userId: number,
  payload: MenuCodesUpdatePayload,
): Promise<void> {
  await httpClient.put(`${PREFIX}/${userId}/permissions`, payload);
}

/** admin 重置用户密码（feat-admin-user-password）。
 * 走已有 PUT /users/{id}/password；后端自动吊销目标用户所有 session。 */
export async function adminResetPassword(
  userId: number,
  payload: AdminResetPasswordPayload,
): Promise<void> {
  await httpClient.put(`${PREFIX}/${userId}/password`, payload);
}

/** 用户有效权限视图（需求 #4）：三来源合集 + 拆分。 */
export async function getUserEffectivePermissions(
  userId: number,
): Promise<UserEffectivePermissions> {
  const res = await httpClient.get<UserEffectivePermissions>(
    `${PREFIX}/${userId}/permissions`,
  );
  return res.data;
}

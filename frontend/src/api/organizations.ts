import { httpClient } from "./client";
import type {
  OrganizationRow,
  OrganizationTreeNode,
  OrganizationCreatePayload,
  OrganizationUpdatePayload,
  MenuCodesUpdatePayload,
  SubjectPermissions,
} from "../types/rbac";

const PREFIX = "/organizations";

export async function listOrganizations(): Promise<OrganizationRow[]> {
  const res = await httpClient.get<OrganizationRow[]>(PREFIX);
  return res.data;
}

/** 嵌套组织树（多根；同 parent 下按 sortOrder 排序）。 */
export async function listOrganizationTree(): Promise<OrganizationTreeNode[]> {
  const res = await httpClient.get<OrganizationTreeNode[]>(`${PREFIX}/tree`);
  return res.data;
}

export async function createOrganization(
  payload: OrganizationCreatePayload,
): Promise<OrganizationRow> {
  const res = await httpClient.post<OrganizationRow>(PREFIX, payload);
  return res.data;
}

export async function updateOrganization(
  organizationId: number,
  payload: OrganizationUpdatePayload,
): Promise<OrganizationRow> {
  const res = await httpClient.put<OrganizationRow>(
    `${PREFIX}/${organizationId}`,
    payload,
  );
  return res.data;
}

export async function deleteOrganization(
  organizationId: number,
): Promise<void> {
  await httpClient.delete(`${PREFIX}/${organizationId}`);
}

/** set-replace：组织直接授权的菜单 code 全量。 */
export async function setOrganizationPermissions(
  organizationId: number,
  payload: MenuCodesUpdatePayload,
): Promise<void> {
  await httpClient.put(`${PREFIX}/${organizationId}/permissions`, payload);
}

/** 组织维度直接授权视图（需求 #4）。 */
export async function getOrganizationPermissions(
  organizationId: number,
): Promise<SubjectPermissions> {
  const res = await httpClient.get<SubjectPermissions>(
    `${PREFIX}/${organizationId}/permissions`,
  );
  return res.data;
}

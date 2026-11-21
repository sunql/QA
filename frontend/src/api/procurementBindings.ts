/** M4-2 采购源绑定 API 客户端（后端契约：/api/v1/procurement-bindings，M1-1.1）。
 *
 * 管理台专用（后端 requireAdmin）；**确认 = 版本冻结点**（确认后属性集合冻结，
 * 改绑须先失效再建新版本——前端所有 confirm 入口必须警示）。
 */

import { httpClient } from "./client";

const BASE = "/procurement-bindings";

export type BindingRole = "BASE" | "DENOMINATOR" | "FILTER" | "NUMERATOR";

export interface SourceBinding {
  id: number;
  classId: number;
  className: string | null;
  datasourceId: number;
  schemaName: string;
  sourceTable: string;
  bindingRole: string;
  filterExpression: string | null;
  filterAllowlist: string[];
  isCandidate: boolean;
  confirmedBy: string | null;
  confirmedTime: string | null;
  validFrom: string;
  validTo: string | null;
  createdBy: string | null;
  propertyCount: number;
}

export interface PropertyBinding {
  id: number;
  sourceBindingId: number;
  propertyId: number;
  propertyName: string | null;
  sourceColumn: string;
  keyOrdinal: number | null;
  transformExpression: string | null;
  isCandidate: boolean;
  confirmedBy: string | null;
  confirmedTime: string | null;
}

export interface SourceBindingDetail {
  binding: SourceBinding;
  properties: PropertyBinding[];
}

export interface SourceBindingCreateBody {
  classId: number;
  datasourceId: number;
  schemaName: string;
  sourceTable: string;
  bindingRole: BindingRole;
  filterExpression?: string | null;
  filterAllowlist?: string[];
}

export interface PropertyBindingCreateBody {
  propertyId: number;
  sourceColumn: string;
  keyOrdinal?: number | null;
  transformExpression?: string | null;
}

export async function listSourceBindings(params?: {
  classId?: number;
  datasourceId?: number;
  effectiveOnly?: boolean;
}): Promise<SourceBinding[]> {
  const res = await httpClient.get<SourceBinding[]>(BASE, { params });
  return res.data;
}

export async function getSourceBinding(id: number): Promise<SourceBindingDetail> {
  const res = await httpClient.get<SourceBindingDetail>(`${BASE}/${id}`);
  return res.data;
}

export async function createSourceBinding(
  body: SourceBindingCreateBody,
): Promise<SourceBinding> {
  const res = await httpClient.post<SourceBinding>(BASE, body);
  return res.data;
}

export async function createPropertyBinding(
  sourceBindingId: number,
  body: PropertyBindingCreateBody,
): Promise<PropertyBinding> {
  const res = await httpClient.post<PropertyBinding>(
    `${BASE}/${sourceBindingId}/properties`,
    body,
  );
  return res.data;
}

/** 确认 = 版本冻结点（前端入口必须 Popconfirm 警示）。 */
export async function confirmSourceBinding(id: number): Promise<SourceBinding> {
  const res = await httpClient.post<SourceBinding>(`${BASE}/${id}/confirm`);
  return res.data;
}

export async function confirmPropertyBinding(
  sourceBindingId: number,
  propertyBindingId: number,
): Promise<PropertyBinding> {
  const res = await httpClient.post<PropertyBinding>(
    `${BASE}/${sourceBindingId}/properties/${propertyBindingId}/confirm`,
  );
  return res.data;
}

export async function expireSourceBinding(id: number): Promise<SourceBinding> {
  const res = await httpClient.post<SourceBinding>(`${BASE}/${id}/expire`);
  return res.data;
}

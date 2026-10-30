import { httpClient } from "./client";
import type {
  DataDomain,
  DataDomainCreate,
  DataDomainUpdate,
} from "../types/dataDomain";

const BASE = "/data-domains";

/** 全量（含禁用，供管理页）；enabledOnly=true 供下拉。 */
export async function listDataDomains(
  enabledOnly = false,
): Promise<DataDomain[]> {
  const res = await httpClient.get<DataDomain[]>(BASE, {
    params: { enabledOnly },
  });
  return res.data;
}

export async function getDataDomain(code: string): Promise<DataDomain> {
  const res = await httpClient.get<DataDomain>(`${BASE}/${code}`);
  return res.data;
}

export async function createDataDomain(
  payload: DataDomainCreate,
): Promise<DataDomain> {
  const res = await httpClient.post<DataDomain>(BASE, payload);
  return res.data;
}

export async function updateDataDomain(
  code: string,
  payload: DataDomainUpdate,
): Promise<DataDomain> {
  const res = await httpClient.put<DataDomain>(`${BASE}/${code}`, payload);
  return res.data;
}

export async function deleteDataDomain(code: string): Promise<void> {
  await httpClient.delete(`${BASE}/${code}`);
}

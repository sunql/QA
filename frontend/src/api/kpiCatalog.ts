import { httpClient } from "./client";
import type {
  KpiCatalog,
  KpiCatalogCreate,
  KpiCatalogUpdate,
} from "../types/kpiCatalog";

const BASE = "/kpi-catalog";

export async function listKpis(): Promise<KpiCatalog[]> {
  const res = await httpClient.get<KpiCatalog[]>(BASE);
  return res.data;
}

export async function getKpi(id: number): Promise<KpiCatalog> {
  const res = await httpClient.get<KpiCatalog>(`${BASE}/${id}`);
  return res.data;
}

export async function createKpi(payload: KpiCatalogCreate): Promise<KpiCatalog> {
  const res = await httpClient.post<KpiCatalog>(BASE, payload);
  return res.data;
}

export async function updateKpi(
  id: number,
  payload: KpiCatalogUpdate,
): Promise<KpiCatalog> {
  const res = await httpClient.put<KpiCatalog>(`${BASE}/${id}`, payload);
  return res.data;
}

export async function deleteKpi(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/${id}`);
}

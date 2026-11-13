import { httpClient } from "./client";
import type {
  KpiCatalog,
  KpiCatalogCreate,
  KpiCatalogUpdate,
  KpiDatasourceSuggestion,
  KpiSyncMissingResult,
} from "../types/kpiCatalog";

const BASE = "/kpi-catalog";

export async function listKpis(unbound = false): Promise<KpiCatalog[]> {
  const res = await httpClient.get<KpiCatalog[]>(
    BASE,
    unbound ? { params: { unbound: true } } : undefined,
  );
  return res.data;
}

export async function getDatasourceSuggestions(
  kpiId: number,
): Promise<KpiDatasourceSuggestion[]> {
  const res = await httpClient.get<KpiDatasourceSuggestion[]>(
    `${BASE}/${kpiId}/datasource-suggestions`,
  );
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

/** 全量对账 KPI 向量 + 图（补缺失/删孤儿，admin-only）。 */
export async function syncMissingKpi(): Promise<KpiSyncMissingResult> {
  const res = await httpClient.post<KpiSyncMissingResult>(
    `${BASE}/embeddings/sync-missing`,
  );
  return res.data;
}

/** 单条 KPI 重建向量（admin-only；非 PUBLISHED 后端返 409）。 */
export async function resyncKpiEmbedding(id: number): Promise<void> {
  await httpClient.post(`${BASE}/${id}/embedding`);
}

import { httpClient } from "./client";
import type {
  OntologyClass,
  OntologyClassCreate,
  OntologyClassUpdate,
  OntologyProperty,
  OntologyPropertyCreate,
  OntologyPropertyUpdate,
  OntologyMetric,
  OntologyMetricCreate,
  OntologyMetricUpdate,
  OntologyJoin,
  OntologyJoinCreate,
  OntologySearchHit,
} from "../types/ontology";

const BASE = "/ontology";

// ===== Class =====

export async function listClasses(options?: { includeExpired?: boolean }): Promise<OntologyClass[]> {
  const res = await httpClient.get<OntologyClass[]>(`${BASE}/classes`, {
    params: { includeExpired: options?.includeExpired ?? false },
  });
  return res.data;
}

export async function listClassVersions(className: string): Promise<OntologyClass[]> {
  const res = await httpClient.get<OntologyClass[]>(
    `${BASE}/classes/${encodeURIComponent(className)}/versions`
  );
  return res.data;
}

export async function getClass(id: number): Promise<OntologyClass> {
  const res = await httpClient.get<OntologyClass>(`${BASE}/classes/${id}`);
  return res.data;
}

export async function createClass(payload: OntologyClassCreate): Promise<OntologyClass> {
  const res = await httpClient.post<OntologyClass>(`${BASE}/classes`, payload);
  return res.data;
}

export async function updateClass(
  id: number,
  payload: OntologyClassUpdate
): Promise<OntologyClass> {
  const res = await httpClient.put<OntologyClass>(`${BASE}/classes/${id}`, payload);
  return res.data;
}

export async function deleteClass(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/classes/${id}`);
}

// ===== Property =====

export async function listPropertiesByClass(
  classId: number
): Promise<OntologyProperty[]> {
  const res = await httpClient.get<OntologyProperty[]>(
    `${BASE}/classes/${classId}/properties`
  );
  return res.data;
}

/** 跨类列出全部本体属性（本体属性管理页 /ontology-properties 专用）。
 *  不带任何过滤条件；管理页自己做 className 过滤。 */
export async function listAllProperties(): Promise<OntologyProperty[]> {
  const res = await httpClient.get<OntologyProperty[]>(`${BASE}/properties`);
  return res.data;
}

export async function getProperty(id: number): Promise<OntologyProperty> {
  const res = await httpClient.get<OntologyProperty>(`${BASE}/properties/${id}`);
  return res.data;
}

export async function createProperty(
  payload: OntologyPropertyCreate
): Promise<OntologyProperty> {
  const res = await httpClient.post<OntologyProperty>(`${BASE}/properties`, payload);
  return res.data;
}

export async function updateProperty(
  id: number,
  payload: OntologyPropertyUpdate
): Promise<OntologyProperty> {
  const res = await httpClient.put<OntologyProperty>(`${BASE}/properties/${id}`, payload);
  return res.data;
}

export async function deleteProperty(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/properties/${id}`);
}

// ===== Metric =====

export async function listMetrics(): Promise<OntologyMetric[]> {
  const res = await httpClient.get<OntologyMetric[]>(`${BASE}/metrics`);
  return res.data;
}

export async function getMetric(id: number): Promise<OntologyMetric> {
  const res = await httpClient.get<OntologyMetric>(`${BASE}/metrics/${id}`);
  return res.data;
}

export async function createMetric(
  payload: OntologyMetricCreate
): Promise<OntologyMetric> {
  const res = await httpClient.post<OntologyMetric>(`${BASE}/metrics`, payload);
  return res.data;
}

export async function updateMetric(
  id: number,
  payload: OntologyMetricUpdate
): Promise<OntologyMetric> {
  const res = await httpClient.put<OntologyMetric>(`${BASE}/metrics/${id}`, payload);
  return res.data;
}

export async function deleteMetric(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/metrics/${id}`);
}

// ===== Join =====

export async function listJoins(): Promise<OntologyJoin[]> {
  const res = await httpClient.get<OntologyJoin[]>(`${BASE}/joins`);
  return res.data;
}

export async function createJoin(payload: OntologyJoinCreate): Promise<OntologyJoin> {
  const res = await httpClient.post<OntologyJoin>(`${BASE}/joins`, payload);
  return res.data;
}

export async function deleteJoin(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/joins/${id}`);
}

// ===== Semantic Search =====

export async function searchOntology(
  q: string,
  options?: { topK?: number; type?: OntologySearchHit["type"] }
): Promise<OntologySearchHit[]> {
  const res = await httpClient.get<OntologySearchHit[]>(`${BASE}/search`, {
    params: { q, topK: options?.topK, type: options?.type },
  });
  return res.data;
}

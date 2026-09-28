import { httpClient } from "./client";
import type {
  LineageEdgeCreate,
  LineageEdgeListFilter,
  LineageEdgeRead,
  LineageEdgeUpdate,
  LineageExtractResult,
} from "../types/lineage";

const BASE = "/lineage/edges";

export async function listEdges(
  filter?: LineageEdgeListFilter,
): Promise<LineageEdgeRead[]> {
  const res = await httpClient.get<LineageEdgeRead[]>(BASE, {
    params: filter as Record<string, string | boolean | undefined>,
  });
  return res.data;
}

export async function getEdge(id: number): Promise<LineageEdgeRead> {
  const res = await httpClient.get<LineageEdgeRead>(`${BASE}/${id}`);
  return res.data;
}

export async function createEdge(
  payload: LineageEdgeCreate,
): Promise<LineageEdgeRead> {
  const res = await httpClient.post<LineageEdgeRead>(BASE, payload);
  return res.data;
}

export async function updateEdge(
  id: number,
  payload: LineageEdgeUpdate,
): Promise<LineageEdgeRead> {
  const res = await httpClient.put<LineageEdgeRead>(`${BASE}/${id}`, payload);
  return res.data;
}

export async function deleteEdge(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/${id}`);
}

/** POST /lineage/edges/extract — 从 ontology 自动抽取并幂等写入血缘边。
 *
 * 抽取源与后端 scripts/lineage_auto_extract.py 一致（OntologyJoin + OntologyMetric.formula
 * + schema introspection）；返回本次新增边数（created=0 表示已是最新）。
 */
export async function extractLineage(): Promise<LineageExtractResult> {
  const res = await httpClient.post<LineageExtractResult>(`${BASE}/extract`);
  return res.data;
}
import { httpClient } from "./client";
import type {
  LineageEdgeCreate,
  LineageEdgeListFilter,
  LineageEdgeRead,
  LineageEdgeUpdate,
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
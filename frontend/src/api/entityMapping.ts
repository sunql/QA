import { httpClient } from "./client";
import type {
  EntityMappingCreate,
  EntityMappingListFilter,
  EntityMappingRead,
  EntityMappingUpdate,
} from "../types/entityMapping";

const BASE = "/entity-mappings";

export async function listMappings(
  filter?: EntityMappingListFilter,
): Promise<EntityMappingRead[]> {
  const res = await httpClient.get<EntityMappingRead[]>(BASE, {
    params: filter as Record<string, string | number | undefined>,
  });
  return res.data;
}

export async function getMapping(id: number): Promise<EntityMappingRead> {
  const res = await httpClient.get<EntityMappingRead>(`${BASE}/${id}`);
  return res.data;
}

export async function createMapping(
  payload: EntityMappingCreate,
): Promise<EntityMappingRead> {
  const res = await httpClient.post<EntityMappingRead>(BASE, payload);
  return res.data;
}

export async function updateMapping(
  id: number,
  payload: EntityMappingUpdate,
): Promise<EntityMappingRead> {
  const res = await httpClient.put<EntityMappingRead>(`${BASE}/${id}`, payload);
  return res.data;
}

export async function deleteMapping(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/${id}`);
}

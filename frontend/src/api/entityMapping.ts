import { httpClient } from "./client";
import type {
  EntityMappingCreate,
  EntityMappingListFilter,
  EntityMappingRead,
  EntityMappingSearchHit,
  EntityMappingUpdate,
  EntityType,
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

/** Phase 6.x：AutoComplete 模糊搜索。
 *
 * - `q` 长度 < 2 时直接返回 []（避免每个按键都打后端）。
 * - 后端会在 q 为空时也返空，所以这里传空串也是安全的；但前端防抖更友好。
 */
export async function searchMappings(
  q: string,
  opts: { entityType?: EntityType; limit?: number } = {},
): Promise<EntityMappingSearchHit[]> {
  const trimmed = q.trim();
  if (trimmed.length < 2) return [];
  const res = await httpClient.get<EntityMappingSearchHit[]>(`${BASE}/search`, {
    params: {
      q: trimmed,
      entityType: opts.entityType,
      limit: opts.limit ?? 20,
    },
  });
  return res.data;
}

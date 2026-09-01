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
 * - 1 字符即触发请求（用户已表达需求；后端已对 enterprise_code / source_code 建
 *   btree 索引，前缀 LIKE 'X%' 走索引，35w 行也能 < 200ms 返回）
 * - 空串不查（避免误触）
 */
export async function searchMappings(
  q: string,
  opts: { entityType?: EntityType; limit?: number } = {},
): Promise<EntityMappingSearchHit[]> {
  const trimmed = q.trim();
  if (trimmed.length === 0) return [];
  const res = await httpClient.get<EntityMappingSearchHit[]>(`${BASE}/search`, {
    params: {
      q: trimmed,
      entityType: opts.entityType,
      limit: opts.limit ?? 20,
    },
  });
  return res.data;
}

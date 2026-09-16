import { httpClient } from "./client";
import type {
  EntityMappingBulkResult,
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

/** 批量导入（feat-entity-mapping-bulk-import 2026-09-16）。
 *  - 上限 1000 行/请求（后端硬限）
 *  - 单事务；返回每行 EntityMappingBulkResultRow
 *  - 失败行不阻塞其它行
 */
export async function bulkImportMappings(
  items: EntityMappingCreate[],
): Promise<EntityMappingBulkResult> {
  const res = await httpClient.post<EntityMappingBulkResult>(
    `${BASE}/bulk`,
    items,
  );
  return res.data;
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

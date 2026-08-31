import { httpClient } from "./client";
import type { GraphTraversalRead } from "../types/graphTraversal";

const BASE = "/graph";

/** 业务图多跳遍历查询（GET /api/v1/graph/traverse，只读）。 */
export async function traverseGraph(
  startType: string,
  startKey: string,
  maxHops: number,
): Promise<GraphTraversalRead> {
  const res = await httpClient.get<GraphTraversalRead>(`${BASE}/traverse`, {
    params: { startType, startKey, maxHops },
  });
  return res.data;
}

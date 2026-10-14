import { httpClient } from "./client";

const BASE = "/system";

// ----- Neo4j -----

export interface GraphNode {
  /** 节点主键：`unified_id`（形如 obj:CLASS:9）。图内一切定位都用它。 */
  unifiedId: string;
  name: string;
  alias: string | null;
  description: string | null;
  // Class
  sourceTable?: string | null;
  // Property
  sourceColumn?: string | null;
  dataType?: string | null;
  isPrimaryKey?: boolean | null;
  isForeignKey?: boolean | null;
  // Metric
  formula?: string | null;
  aggFunction?: string | null;
}

export interface GraphRelation {
  relType: string;
  targetUid: string;
  targetName: string;
  targetLabel: string;
}

export async function listGraphNodes(
  label: string,
  search = "",
): Promise<GraphNode[]> {
  const res = await httpClient.get<GraphNode[]>(`${BASE}/graph/nodes`, {
    params: { label, search },
  });
  return res.data;
}

export async function getGraphRelations(
  label: string,
  unifiedId: string,
): Promise<GraphRelation[]> {
  // unified_id 含冒号（obj:CLASS:9），必须 encode —— 否则路径被截断成多段。
  const res = await httpClient.get<GraphRelation[]>(
    `${BASE}/graph/nodes/${label}/${encodeURIComponent(unifiedId)}/relationships`,
  );
  return res.data;
}

// ----- Milvus -----

export interface VectorEmbedding {
  ontology_id: number;
  type: string;
  name: string;
  alias: string | null;
  description: string | null;
  dim: number;
}

export interface VectorStats {
  class: number;
  property: number;
  metric: number;
}

export async function listEmbeddings(
  type?: string,
  search = "",
): Promise<VectorEmbedding[]> {
  const res = await httpClient.get<VectorEmbedding[]>(
    `${BASE}/vectors/embeddings`,
    { params: { type: type || undefined, search } },
  );
  return res.data;
}

export async function getVectorStats(): Promise<VectorStats> {
  const res = await httpClient.get<VectorStats>(`${BASE}/vectors/stats`);
  return res.data;
}

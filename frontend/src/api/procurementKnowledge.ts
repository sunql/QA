/** M4-3 采购知识/场景目录 API 客户端（M2 四端点 + M3-3 catalog）。
 *
 * 后端契约（均已进 main @ 8efe3d6d+/db567b38）：
 * - POST /procurement/knowledge/query（P4 薄别名，域固定 PROCUREMENT）
 * - GET  /procurement/knowledge/entities/search
 * - GET  /procurement/knowledge/graph/neighbors
 * - POST /procurement/knowledge/documents/search
 * - GET  /procurement/agents/catalog（场景目录，不暴露 handlerRef）
 * 开关：ENABLE_KNOWLEDGE_FACADE 关 ⇒ 整组 404（前端按需降级提示）。
 */

import { httpClient } from "./client";

const KBASE = "/procurement/knowledge";

export interface KnowledgeEntityHint {
  type: string;
  code: string;
}

export interface KnowledgeQueryResult {
  question: string;
  domain: string;
  release?: { id?: number; version?: string | null; manifestHash?: string; degraded?: boolean };
  entities: { type: string; code: string; name?: string | null }[];
  evidence: KnowledgeEvidence[];
  routeResults?: {
    channel: string;
    status: string;
    itemCount: number;
    degradedReason: string | null;
  }[];
}

export interface KnowledgeEvidence {
  evidenceId: string;
  channel: string;
  kind: string;
  ref: string;
  title: string;
  snippet?: string | null;
  releaseId?: number | null;
  releaseVersion?: string | null;
  source: string;
  confidence?: number | null;
  security?: { level: string | null; scope: string[] } | null;
  sourceRef?: {
    system?: string | null;
    object?: string | null;
    recordKey?: string | null;
    documentId?: string | null;
    queryId?: string | null;
  } | null;
  observedAt?: string | null;
  validAt?: string | null;
  ruleVersion?: string | null;
}

export async function queryKnowledge(question: string, topK = 5): Promise<KnowledgeQueryResult> {
  const res = await httpClient.post<KnowledgeQueryResult>(
    `${KBASE}/query`,
    { question, channels: ["GRAPH", "SQL", "RAG", "METRIC"], topK },
  );
  return res.data;
}

export interface EntityMatch {
  entityType: string;
  enterpriseKey: number;
  enterpriseCode: string;
  name: string | null;
  matchScore: number;
  sourceMappings: {
    sourceSystem: string;
    sourceKey: string;
    sourceCode: string;
    matchRule: string;
  }[];
}

export async function searchEntities(q: string, limit = 20): Promise<EntityMatch[]> {
  const res = await httpClient.get<EntityMatch[]>(`${KBASE}/entities/search`, {
    params: { q, limit },
  });
  return res.data;
}

export interface GraphNeighbors {
  entityType: string;
  entityKey: string;
  maxHops: number;
  effectiveRelationTypes: string[];
  nodes: { entityType: string; key: string; code?: string | null; name?: string | null }[];
  edges: {
    depth: number;
    fromType: string;
    fromKey: string;
    relType: string;
    toType: string;
    toKey: string;
  }[];
  truncated: boolean;
}

export async function getGraphNeighbors(
  entityType: string,
  entityKey: string,
  maxHops = 2,
): Promise<GraphNeighbors> {
  const res = await httpClient.get<GraphNeighbors>(`${KBASE}/graph/neighbors`, {
    params: { entityType, entityKey, maxHops },
  });
  return res.data;
}

export interface DocumentSearchHit extends KnowledgeEvidence {}

export async function searchProcurementDocuments(
  question: string,
  topK = 5,
): Promise<KnowledgeEvidence[]> {
  const res = await httpClient.post<KnowledgeEvidence[]>(
    `${KBASE}/documents/search`,
    { question, topK },
  );
  return res.data;
}

export interface AgentCatalogEntry {
  agentCode: string;
  agentName: string;
  description: string | null;
  status: string;
  runnable: boolean;
  tool: { name: string; description?: string | null } | null;
}

export async function getAgentCatalog(): Promise<AgentCatalogEntry[]> {
  const res = await httpClient.get<AgentCatalogEntry[]>("/procurement/agents/catalog");
  return res.data;
}

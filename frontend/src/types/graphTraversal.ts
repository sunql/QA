// 知识图谱多跳推理相关类型（对齐后端 GraphTraversalRead / GraphTraversalHop，Phase 6.3）

// 业务实体类型白名单（与后端 BUSINESS_ENTITY_LABELS 对齐）
export const BUSINESS_ENTITY_TYPES = [
  "Supplier",
  "Material",
  "PurchaseOrder",
  "GoodsReceipt",
  "IncomingInspection",
  "NCR",
  "Contract",
] as const;

export type BusinessEntityType = (typeof BUSINESS_ENTITY_TYPES)[number];

// 一跳遍历记录（camelCase JSON 契约对齐后端 GraphTraversalHop）
export interface GraphTraversalHop {
  depth: number;
  fromKey: string;
  fromCode: string;
  fromName: string | null;
  fromType: string;
  relType: string;
  toKey: string;
  toCode: string;
  toName: string | null;
  toType: string;
}

// 多跳推理结果（对齐后端 GraphTraversalRead）
export interface GraphTraversalRead {
  startKey: string;
  startType: string;
  maxHops: number;
  hops: GraphTraversalHop[];
  reachableTypes: string[];
  fetchedAt: string;
}

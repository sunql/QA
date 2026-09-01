/** Phase 5.1/5.2 Document Catalog + RAG 类型契约。
 *
 * 与后端 DocumentRead / DocumentCreate / DocumentUpdate / DocEntityRelationCreate /
 * DocEntityRelationRead 1:1 对齐（camelCase）。
 */

import type { EntityType } from "./entityMapping";

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

/** 文档类型。 */
export type DocumentType =
  | "CONTRACT"
  | "8D_REPORT"
  | "AUDIT_REPORT"
  | "SPEC"
  | "SOP"
  | "QUALITY_AGREEMENT"
  | "INSPECTION_SPEC"
  | "REMEDIATION_REPORT"
  | "PURCHASE_SPEC"
  | "MEETING_MINUTES"
  | "SAFETY_SHEET"
  | "OTHER";

export const DOCUMENT_TYPE_OPTIONS: { value: DocumentType; label: string }[] = [
  { value: "CONTRACT", label: "合同" },
  { value: "8D_REPORT", label: "8D 报告" },
  { value: "AUDIT_REPORT", label: "审计报告" },
  { value: "SPEC", label: "规格书" },
  { value: "SOP", label: "SOP" },
  { value: "QUALITY_AGREEMENT", label: "质量协议" },
  { value: "INSPECTION_SPEC", label: "检验规范" },
  { value: "REMEDIATION_REPORT", label: "整改报告" },
  { value: "PURCHASE_SPEC", label: "采购规格" },
  { value: "MEETING_MINUTES", label: "会议纪要" },
  { value: "SAFETY_SHEET", label: "安全数据表" },
  { value: "OTHER", label: "其他" },
];

/** 文档状态。 */
export type DocumentStatus = "ACTIVE" | "EXPIRED";

export const DOCUMENT_STATUS_OPTIONS: { value: DocumentStatus; label: string }[] = [
  { value: "ACTIVE", label: "有效" },
  { value: "EXPIRED", label: "已失效" },
];

/** 安全级别。 */
export type DocumentSecurityLevel = "L1" | "L2" | "L3";

export const DOCUMENT_SECURITY_OPTIONS: { value: DocumentSecurityLevel; label: string }[] = [
  { value: "L1", label: "L1（公开）" },
  { value: "L2", label: "L2（内部）" },
  { value: "L3", label: "L3（机密）" },
];

/** 文档-实体关联类型。 */
export type DocEntityRelationType =
  | "CONTRACT"
  | "8D_REPORT"
  | "AUDIT_REPORT"
  | "SPEC"
  | "SOP"
  | "QUALITY_AGREEMENT"
  | "INSPECTION_SPEC"
  | "REMEDIATION_REPORT"
  | "OTHER";

export const DOC_RELATION_TYPE_OPTIONS: { value: DocEntityRelationType; label: string }[] = [
  { value: "CONTRACT", label: "合同" },
  { value: "8D_REPORT", label: "8D 报告" },
  { value: "AUDIT_REPORT", label: "审计报告" },
  { value: "SPEC", label: "规格书" },
  { value: "SOP", label: "SOP" },
  { value: "QUALITY_AGREEMENT", label: "质量协议" },
  { value: "INSPECTION_SPEC", label: "检验规范" },
  { value: "REMEDIATION_REPORT", label: "整改报告" },
  { value: "OTHER", label: "其他" },
];

// ---------------------------------------------------------------------------
// Document
// ---------------------------------------------------------------------------

export interface DocumentRead {
  id: number;
  documentId: string;
  documentName: string;
  documentType: DocumentType;
  version: string;
  status: DocumentStatus;
  owner: string | null;
  effectiveDate: string | null;
  securityLevel: DocumentSecurityLevel;
  storageUrl: string | null;
  contentHash: string | null;
  createdTime: string;
  updatedTime: string;
}

export interface DocumentCreate {
  documentId: string;
  documentName: string;
  documentType: DocumentType;
  version?: string;
  owner?: string;
  effectiveDate?: string;
  securityLevel?: DocumentSecurityLevel;
}

export interface DocumentUpdate {
  documentName?: string;
  documentType?: DocumentType;
  version?: string;
  status?: DocumentStatus;
  owner?: string;
  effectiveDate?: string;
  securityLevel?: string;
}

// ---------------------------------------------------------------------------
// Document-Entity Relation
// ---------------------------------------------------------------------------

export interface DocEntityRelationRead {
  id: number;
  documentId: string;
  entityType: EntityType;
  entityKey: number;
  relationType: DocEntityRelationType;
}

export interface DocEntityRelationCreate {
  documentId: string;
  entityType: EntityType;
  entityKey: number;
  relationType?: DocEntityRelationType;
}

// ---------------------------------------------------------------------------
// RAG Search
// ---------------------------------------------------------------------------

export interface RagSearchResult {
  document_id: string;
  document_name: string;
  chunk_text: string;
  score: number;
}

export interface RagSearchResponse {
  query: string;
  results: RagSearchResult[];
}

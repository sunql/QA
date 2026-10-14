/**
 * Wiki ↔ Ontology 链接的类型定义。
 * 后端 API：GET/POST/DELETE/PATCH /api/v1/admin/wiki-links
 */

export type WikiLinkType = "class" | "property" | "metric";

export interface WikiLink {
  id: number;
  page_id: string;
  chunk_id: string | null;
  ontology_type: WikiLinkType;
  ontology_id: number;
  ontology_name: string | null;
  ontology_alias: string | null;
  weight: number;
  note: string | null;
  created_by: number;
  revoked_time: string | null;
}

export interface WikiLinkableTarget {
  id: number;
  type: WikiLinkType;
  name: string;
  alias: string | null;
  description: string | null;
}

export interface CreateWikiLinkRequest {
  page_id: string;
  chunk_id?: string | null;
  ontology_type: WikiLinkType;
  ontology_id: number;
  weight?: number;
  note?: string | null;
}

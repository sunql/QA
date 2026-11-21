/** Phase 5.1/5.2 Document Catalog + RAG API 客户端。
 *
 * 底层走 httpClient（client.ts 中的 axios 实例，统一带 auth headers）。
 * 文件上传走 FormData，直接用 axios 以便灵活设置 Content-Type。
 */

import axios from "axios";
import { httpClient } from "./client";
import { API_BASE_URL } from "../config";
import { authHeaders } from "./authHeaders";
import type {
  DocumentCreate,
  DocumentRead,
  DocumentUpdate,
  DocEntityRelationCreate,
  DocEntityRelationRead,
  RagSearchResult,
  DocQaRequestPayload,
  DocQaSseEvent,
} from "../types/document";

// ---------------------------------------------------------------------------
// Document Catalog
// ---------------------------------------------------------------------------

const BASE = "/documents";

export interface DocumentFilters {
  documentType?: string;
  securityLevel?: string;
  status?: string;
  limit?: number;
  offset?: number;
}

export async function listDocuments(
  filters: DocumentFilters = {},
): Promise<DocumentRead[]> {
  const params = new URLSearchParams();
  if (filters.documentType) params.set("documentType", filters.documentType);
  if (filters.securityLevel) params.set("securityLevel", filters.securityLevel);
  if (filters.status) params.set("status", filters.status);
  if (filters.limit != null) params.set("limit", String(filters.limit));
  if (filters.offset != null) params.set("offset", String(filters.offset));
  const qs = params.toString();
  const res = await httpClient.get<DocumentRead[]>(BASE + (qs ? `?${qs}` : ""));
  return res.data;
}

export async function getDocument(id: number): Promise<DocumentRead> {
  const res = await httpClient.get<DocumentRead>(`${BASE}/${id}`);
  return res.data;
}

export async function createDocument(
  payload: DocumentCreate,
): Promise<DocumentRead> {
  const res = await httpClient.post<DocumentRead>(BASE, payload);
  return res.data;
}

export async function updateDocument(
  id: number,
  payload: DocumentUpdate,
): Promise<DocumentRead> {
  const res = await httpClient.put<DocumentRead>(`${BASE}/${id}`, payload);
  return res.data;
}

export async function deleteDocument(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/${id}`);
}

// ---------------------------------------------------------------------------
// 原文阅览 / 向量化（0163）
// ---------------------------------------------------------------------------

/**
 * 原文 URL（浏览器直接打开）。
 *
 * 用 `window.open` 而不是 fetch：PDF 要**浏览器原生查看器**渲染（保真 100%），
 * fetch 回来的是 blob 下载不了也没法内嵌。
 *
 * ⚠️ 需要带 Authorization 头 ⇒ 不能用裸 `<a href>`（不会带 Bearer）。
 *    这里返回带 token 的 URL，token 走 query 参数会被后端拒绝 ⇒
 *    实际做法见 `openDocumentSource`：先 fetch 拿 blob 再 objectURL。
 */
export async function fetchDocumentSource(
  id: number,
  disposition: "inline" | "attachment" = "inline",
): Promise<{ url: string; filename: string; revoke: () => void }> {
  const res = await httpClient.get(`${BASE}/${id}/source`, {
    params: { disposition },
    responseType: "blob",
  });
  const blob = res.data as Blob;
  const url = URL.createObjectURL(blob);
  const dispositionHeader = res.headers["content-disposition"] ?? "";
  const utf8Match = dispositionHeader.match(/filename\*=UTF-8''([^;]+)/i);
  const plainMatch = dispositionHeader.match(/filename="([^"]+)"/i);
  const filename = utf8Match
    ? decodeURIComponent(utf8Match[1])
    : (plainMatch?.[1] ?? "document");
  return { url, filename, revoke: () => URL.revokeObjectURL(url) };
}

/** 预览 URL（统一 HTML；PDF/图片由后端 302 到原件）。 */
export function documentPreviewUrl(id: number): string {
  return `${API_BASE_URL}${BASE}/${id}/preview`;
}

/** 向量化：把已入库文档的原文灌进向量库（wiki 导入的文件靠它才能被检索到）。 */
export async function vectorizeDocument(
  id: number,
): Promise<{ documentId: string; chunks: number; replaced: boolean }> {
  const res = await httpClient.post(`${BASE}/${id}/vectorize`);
  return res.data;
}

// ---------------------------------------------------------------------------
// Document-Entity Relations
// ---------------------------------------------------------------------------

export async function listRelations(filters: {
  documentId?: string;
  entityType?: string;
  entityKey?: number;
} = {}): Promise<DocEntityRelationRead[]> {
  const params = new URLSearchParams();
  if (filters.documentId) params.set("documentId", filters.documentId);
  if (filters.entityType) params.set("entityType", filters.entityType);
  if (filters.entityKey != null) params.set("entityKey", String(filters.entityKey));
  const qs = params.toString();
  const res = await httpClient.get<DocEntityRelationRead[]>(
    `${BASE}/relations${qs ? `?${qs}` : ""}`,
  );
  return res.data;
}

export async function createRelation(
  payload: DocEntityRelationCreate,
): Promise<DocEntityRelationRead> {
  const res = await httpClient.post<DocEntityRelationRead>(
    `${BASE}/relations`,
    payload,
  );
  return res.data;
}

export async function deleteRelation(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/relations/${id}`);
}

// ---------------------------------------------------------------------------
// RAG: Upload & Search
// ---------------------------------------------------------------------------

/** 上传文档并触发 RAG 向量化入库。 */
export async function uploadDocument(
  file: File,
  opts: {
    documentType?: string;
    version?: string;
    owner?: string;
    effectiveDate?: string;
    securityLevel?: string;
  } = {},
): Promise<{ documentId: string; chunks: number }> {
  const form = new FormData();
  form.append("file", file);
  if (opts.documentType) form.set("documentType", opts.documentType);
  if (opts.version) form.set("version", opts.version);
  if (opts.owner) form.set("owner", opts.owner);
  if (opts.effectiveDate) form.set("effectiveDate", opts.effectiveDate);
  if (opts.securityLevel) form.set("securityLevel", opts.securityLevel);

  const res = await axios.postForm<{ documentId: string; chunks: number }>(
    `${API_BASE_URL}${BASE}/upload`,
    form,
    {
      headers: authHeaders(),
    },
  );
  return res.data;
}

/** 语义检索文档 chunks。 */
export async function searchDocuments(
  q: string,
  opts: {
    securityLevel?: string;
    topK?: number;
  } = {},
): Promise<RagSearchResult[]> {
  const params = new URLSearchParams({ q });
  if (opts.securityLevel) params.set("securityLevel", opts.securityLevel);
  if (opts.topK != null) params.set("topK", String(opts.topK));
  const res = await httpClient.post<RagSearchResult[]>(
    `${BASE}/search?${params.toString()}`,
    {}, // POST body required
  );
  return res.data;
}

// ---------------------------------------------------------------------------
// Doc-Qa SSE client (documents-knowledge-qa, Task 7)
// ---------------------------------------------------------------------------

function evTypeToKind(t: string | undefined): DocQaSseEvent["kind"] {
  switch (t) {
    case "qa_meta": return "meta";
    case "qa_citations": return "citations";
    case "token": return "token";
    case "qa_done": return "done";
    case "error": return "error";
    default: return "error";
  }
}

export async function searchDocumentsQa(
  payload: DocQaRequestPayload,
  onEvent: (event: DocQaSseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  // SSE 流式 —— 走裸 fetch，不经 httpClient 拦截器，
  // 故用 authHeaders()（SSOT）手动注入 Authorization + X-Tenant-Id。
  const res = await fetch("/api/v1/documents/qa", {
    method: "POST",
    credentials: "include",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // 按 \n\n 拆 SSE event
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      const dataLine = part.split("\n").find((l) => l.startsWith("data: "));
      if (!dataLine) continue;
      const dataStr = dataLine.slice("data: ".length);
      try {
        const obj = JSON.parse(dataStr);
        // kind 由 event 字段推断
        const evType = part.split("\n").find((l) => l.startsWith("event: "))?.slice("event: ".length).trim();
        onEvent({ ...obj, kind: evTypeToKind(evType) });
      } catch {
        // 跳过解析失败
      }
    }
  }
}

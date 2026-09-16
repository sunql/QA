import axios from "axios";
import { httpClient, showMessageError } from "./client";
import { API_BASE_URL, DEFAULT_TENANT_ID, DEFAULT_USER_ID } from "../config";
import { i18n } from "../i18n";
import type {
  BatchRelationRequest,
  BatchRelationResult,
  BatchTemplateKind,
  OntologyClass,
  OntologyClassCreate,
  OntologyClassUpdate,
  OntologyProperty,
  OntologyPropertyCreate,
  OntologyPropertyUpdate,
  OntologyMetric,
  OntologyMetricCreate,
  OntologyMetricUpdate,
  OntologyJoin,
  OntologyJoinCreate,
  OntologySemanticRelation,
  OntologySemanticRelationCreate,
  OntologyCsvParseResult,
  RelationBackfillResult,
  OntologySearchHit,
  EmbeddingSyncMissingResult,
} from "../types/ontology";

const BASE = "/ontology";

// ===== Class =====

export async function listClasses(options?: { includeExpired?: boolean }): Promise<OntologyClass[]> {
  const res = await httpClient.get<OntologyClass[]>(`${BASE}/classes`, {
    params: { includeExpired: options?.includeExpired ?? false },
  });
  return res.data;
}

export async function listClassVersions(className: string): Promise<OntologyClass[]> {
  const res = await httpClient.get<OntologyClass[]>(
    `${BASE}/classes/${encodeURIComponent(className)}/versions`
  );
  return res.data;
}

export async function getClass(id: number): Promise<OntologyClass> {
  const res = await httpClient.get<OntologyClass>(`${BASE}/classes/${id}`);
  return res.data;
}

export async function createClass(payload: OntologyClassCreate): Promise<OntologyClass> {
  const res = await httpClient.post<OntologyClass>(`${BASE}/classes`, payload);
  return res.data;
}

export async function updateClass(
  id: number,
  payload: OntologyClassUpdate
): Promise<OntologyClass> {
  const res = await httpClient.put<OntologyClass>(`${BASE}/classes/${id}`, payload);
  return res.data;
}

export async function deleteClass(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/classes/${id}`);
}

// ===== Property =====

export async function listPropertiesByClass(
  classId: number
): Promise<OntologyProperty[]> {
  const res = await httpClient.get<OntologyProperty[]>(
    `${BASE}/classes/${classId}/properties`
  );
  return res.data;
}

/** 跨类列出全部本体属性（本体属性管理页 /ontology-properties 专用）。
 *  不带任何过滤条件；管理页自己做 className 过滤。 */
export async function listAllProperties(): Promise<OntologyProperty[]> {
  const res = await httpClient.get<OntologyProperty[]>(`${BASE}/properties`);
  return res.data;
}

export async function getProperty(id: number): Promise<OntologyProperty> {
  const res = await httpClient.get<OntologyProperty>(`${BASE}/properties/${id}`);
  return res.data;
}

export async function createProperty(
  payload: OntologyPropertyCreate
): Promise<OntologyProperty> {
  const res = await httpClient.post<OntologyProperty>(`${BASE}/properties`, payload);
  return res.data;
}

export async function updateProperty(
  id: number,
  payload: OntologyPropertyUpdate
): Promise<OntologyProperty> {
  const res = await httpClient.put<OntologyProperty>(`${BASE}/properties/${id}`, payload);
  return res.data;
}

export async function deleteProperty(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/properties/${id}`);
}

// ===== Metric =====

export async function listMetrics(): Promise<OntologyMetric[]> {
  const res = await httpClient.get<OntologyMetric[]>(`${BASE}/metrics`);
  return res.data;
}

export async function getMetric(id: number): Promise<OntologyMetric> {
  const res = await httpClient.get<OntologyMetric>(`${BASE}/metrics/${id}`);
  return res.data;
}

export async function createMetric(
  payload: OntologyMetricCreate
): Promise<OntologyMetric> {
  const res = await httpClient.post<OntologyMetric>(`${BASE}/metrics`, payload);
  return res.data;
}

export async function updateMetric(
  id: number,
  payload: OntologyMetricUpdate
): Promise<OntologyMetric> {
  const res = await httpClient.put<OntologyMetric>(`${BASE}/metrics/${id}`, payload);
  return res.data;
}

export async function deleteMetric(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/metrics/${id}`);
}

// ===== Join =====

export async function listJoins(): Promise<OntologyJoin[]> {
  const res = await httpClient.get<OntologyJoin[]>(`${BASE}/joins`);
  return res.data;
}

export async function createJoin(payload: OntologyJoinCreate): Promise<OntologyJoin> {
  const res = await httpClient.post<OntologyJoin>(`${BASE}/joins`, payload);
  return res.data;
}

export async function deleteJoin(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/joins/${id}`);
}

// ===== Semantic Relation =====

export async function listSemanticRelations(): Promise<OntologySemanticRelation[]> {
  const res = await httpClient.get<OntologySemanticRelation[]>(`${BASE}/relations`);
  return res.data;
}

export async function createSemanticRelation(
  payload: OntologySemanticRelationCreate
): Promise<OntologySemanticRelation> {
  const res = await httpClient.post<OntologySemanticRelation>(`${BASE}/relations`, payload);
  return res.data;
}

export async function deleteSemanticRelation(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/relations/${id}`);
}

/** 一键补关系：join 全量入图 + X3 外键补 ref_class_id（幂等修复）。 */
export async function backfillRelations(): Promise<RelationBackfillResult> {
  const res = await httpClient.post<RelationBackfillResult>(`${BASE}/relations/backfill`);
  return res.data;
}

// ===== Batch Relation Engine（通用批量关系引擎） =====

/** POST /ontology/batch — 执行：syncGraph / inferJoins / applyManifest（任选其一或多个）。 */
export async function runOntologyBatch(payload: BatchRelationRequest): Promise<BatchRelationResult> {
  const res = await httpClient.post<BatchRelationResult>(`${BASE}/batch`, payload);
  return res.data;
}

/** POST /ontology/batch/preview — 只读预览：推断候选 + 冲突预判计数，不写库。 */
export async function previewOntologyBatch(
  payload: BatchRelationRequest
): Promise<BatchRelationResult> {
  const res = await httpClient.post<BatchRelationResult>(`${BASE}/batch/preview`, payload);
  return res.data;
}

/**
 * GET /ontology/batch/template?kind=joins|relations
 * 下载 CSV 模板（UTF-8 BOM，Excel 友好）。
 */
export async function downloadBatchTemplate(kind: BatchTemplateKind): Promise<Blob> {
  const res = await httpClient.get(`${BASE}/batch/template`, {
    params: { kind },
    responseType: "blob",
  });
  return res.data as unknown as Blob;
}

/** POST /ontology/batch/parse-csv — 上传 CSV 清单 → 按类名反解为 manifest JSON。
 *
 * 走 axios.postForm 而非 httpClient：httpClient 实例默认 `Content-Type: application/json`，
 * 会让 axios 不自动补 multipart boundary，后端 `request.form()` 读不到 kind/file → 422
 * missing field（与 document.ts 上传一致绕开 httpClient）。显式携带 auth 头，失败 toast
 * 文案镜像 httpClient 拦截器，再 rethrow 供调用方兜底。
 */
export async function parseBatchCsv(
  file: File,
  kind: BatchTemplateKind = "relations"
): Promise<OntologyCsvParseResult> {
  const form = new FormData();
  form.append("kind", kind);
  form.append("file", file);
  try {
    const res = await axios.postForm<OntologyCsvParseResult>(
      `${API_BASE_URL}${BASE}/batch/parse-csv`,
      form,
      {
        headers: {
          "X-Tenant-Id": DEFAULT_TENANT_ID,
          "X-User-Id": DEFAULT_USER_ID,
        },
      }
    );
    return res.data;
  } catch (error) {
    // 走裸 axios 的请求不经过拦截器 —— 用 client.ts 暴露的出口弹错，
    // 避免静态 ``message`` 调用脱离 React 上下文触发 antd 警告。
    showMessageError(_describeUploadError(error));
    throw error;
  }
}

/** 镜像 httpClient 拦截器的错误文案：后端 error/detail 优先，再按 status 兜底。 */
function _describeUploadError(error: unknown): string {
  const response = (error as { response?: { status?: number; data?: unknown } }).response;
  const status = response?.status;
  const data = response?.data as { error?: string; detail?: unknown } | undefined;
  if (typeof data?.error === "string" && data.error !== "") return data.error;
  if (typeof data?.detail === "string") return data.detail;
  if (typeof status === "number") return i18n.t("errors.requestFailedHttp", { status: String(status) });
  return i18n.t("errors.networkError");
}

// ===== Semantic Search =====

export async function searchOntology(
  q: string,
  options?: { topK?: number; type?: OntologySearchHit["type"] }
): Promise<OntologySearchHit[]> {
  const res = await httpClient.get<OntologySearchHit[]>(`${BASE}/search`, {
    params: { q, topK: options?.topK, type: options?.type },
  });
  return res.data;
}

// ===== Embedding 手动同步（向量对账） =====

/** 手动同步单个类向量：服务端重新生成 embedding 并覆盖 Milvus。 */
export async function syncClassEmbedding(id: number): Promise<void> {
  await httpClient.post(`${BASE}/classes/${id}/embedding`);
}

/** 向量对账：为 PG 有而 Milvus 缺失的类补生成向量，返回摘要。 */
export async function syncMissingEmbeddings(): Promise<EmbeddingSyncMissingResult> {
  const res = await httpClient.post<EmbeddingSyncMissingResult>(
    `${BASE}/embeddings/sync-missing`
  );
  return res.data;
}

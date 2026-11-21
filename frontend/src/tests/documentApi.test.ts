import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

const axiosMock = vi.hoisted(() => ({ postForm: vi.fn() }));
vi.mock("axios", () => ({ default: axiosMock }));

import {
  listDocuments,
  getDocument,
  createDocument,
  updateDocument,
  deleteDocument,
  listRelations,
  createRelation,
  deleteRelation,
  uploadDocument,
  searchDocuments,
  searchDocumentsQa,
} from "../api/document";
import { useAuthStore } from "../stores/authStore";
import { DEFAULT_TENANT_ID } from "../config";

describe("api/document", () => {
  beforeEach(() => { vi.clearAllMocks(); });
  afterEach(() => {
    vi.unstubAllGlobals();
    useAuthStore.setState({ token: null });
  });

  it("listDocuments 无 filters", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listDocuments();
    expect(httpMock.get).toHaveBeenCalledWith("/documents");
  });

  it("listDocuments 携带所有 filters", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listDocuments({
      documentType: "CONTRACT",
      securityLevel: "L1",
      status: "ACTIVE",
      limit: 10,
      offset: 0,
    });
    expect(httpMock.get).toHaveBeenCalledWith(
      "/documents?documentType=CONTRACT&securityLevel=L1&status=ACTIVE&limit=10&offset=0",
    );
  });

  it("getDocument GET /documents/:id", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 5, documentId: "D-5" } });
    const result = await getDocument(5);
    expect(httpMock.get).toHaveBeenCalledWith("/documents/5");
    expect(result.documentId).toBe("D-5");
  });

  it("createDocument POST /documents", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 1 } });
    await createDocument({ documentCode: "D-1" } as never);
    expect(httpMock.post).toHaveBeenCalledWith("/documents", { documentCode: "D-1" });
  });

  it("updateDocument PUT /documents/:id", async () => {
    httpMock.put.mockResolvedValue({ data: { id: 5 } });
    await updateDocument(5, { title: "新" } as never);
    expect(httpMock.put).toHaveBeenCalledWith("/documents/5", { title: "新" });
  });

  it("deleteDocument DELETE /documents/:id", async () => {
    httpMock.delete.mockResolvedValue({});
    await deleteDocument(7);
    expect(httpMock.delete).toHaveBeenCalledWith("/documents/7");
  });

  it("listRelations 无 filters", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listRelations();
    expect(httpMock.get).toHaveBeenCalledWith("/documents/relations");
  });

  it("listRelations 携带 filters", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listRelations({ documentId: "5", entityType: "CONTRACT", entityKey: 100 });
    expect(httpMock.get).toHaveBeenCalledWith(
      "/documents/relations?documentId=5&entityType=CONTRACT&entityKey=100",
    );
  });

  it("createRelation POST /documents/relations", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 1 } });
    await createRelation({ documentId: "5" } as never);
    expect(httpMock.post).toHaveBeenCalledWith("/documents/relations", { documentId: "5" });
  });

  it("deleteRelation DELETE /documents/relations/:id", async () => {
    httpMock.delete.mockResolvedValue({});
    await deleteRelation(99);
    expect(httpMock.delete).toHaveBeenCalledWith("/documents/relations/99");
  });

  it("uploadDocument 走 axios.postForm + 携带 tenant headers", async () => {
    axiosMock.postForm.mockResolvedValue({ data: { documentId: "5", chunks: 3 } });
    const file = new File(["x"], "test.txt");
    const result = await uploadDocument(file, {
      documentType: "CONTRACT",
      version: "v1",
      owner: "alice",
      effectiveDate: "2026-09-01",
      securityLevel: "L1",
    });
    expect(axiosMock.postForm).toHaveBeenCalledTimes(1);
    const call = axiosMock.postForm.mock.calls[0];
    expect(call[0]).toMatch(/\/documents\/upload$/);
    expect(call[2].headers["X-Tenant-Id"]).toBeDefined();
    // feat-user-auth: 不再注入 X-User-Id，由 Authorization: Bearer 替代
    expect(call[2].headers["X-User-Id"]).toBeUndefined();
    // 没有登录态时也不应有 Authorization —— 测试环境裸 axios 直调
    expect(call[2].headers["Authorization"]).toBeUndefined();
    expect(result).toEqual({ documentId: "5", chunks: 3 });
  });

  it("searchDocuments POST /documents/search with q + opts", async () => {
    httpMock.post.mockResolvedValue({ data: [] });
    await searchDocuments("质量协议", { securityLevel: "L2", topK: 5 });
    const call = httpMock.post.mock.calls[0];
    expect(call[0]).toMatch(/^\/documents\/search\?q=/);
    expect(call[0]).toContain("securityLevel=L2");
    expect(call[0]).toContain("topK=5");
    expect(call[1]).toEqual({});
  });

  it("searchDocumentsQa 登录态注入 SSOT auth headers（Authorization + X-Tenant-Id）", async () => {
    useAuthStore.setState({ token: "test-jwt" });
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { controller.close(); },
    });
    const fetchMock = vi.fn().mockResolvedValue(new Response(stream, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await searchDocumentsQa({ question: "门槛" } as never, () => undefined);

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers).toMatchObject({
      Authorization: "Bearer test-jwt",
      "X-Tenant-Id": DEFAULT_TENANT_ID,
    });
  });
});
// ---------------------------------------------------------------------------
// 原文阅览 / 向量化（0163）
// ---------------------------------------------------------------------------

describe("api/document 原文阅览与向量化", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("fetchDocumentSource 取 blob 并解析中文文件名（RFC 5987）", async () => {
    const revoke = vi.fn();
    const createObjectURL = vi.fn(() => "blob:mock");
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL,
      revokeObjectURL: revoke,
    });
    httpMock.get.mockResolvedValue({
      data: new Blob(["x"]),
      headers: {
        "content-disposition":
          "inline; filename=\"document.pdf\"; filename*=UTF-8''%E9%87%87%E8%B4%AD.pdf",
      },
    });

    const { fetchDocumentSource } = await import("../api/document");
    const result = await fetchDocumentSource(5);

    expect(httpMock.get).toHaveBeenCalledWith("/documents/5/source", {
      params: { disposition: "inline" },
      responseType: "blob",
    });
    expect(result.url).toBe("blob:mock");
    // 中文名从 filename* 解出来（不是 ASCII 回退名）
    expect(result.filename).toBe("采购.pdf");
    result.revoke();
    expect(revoke).toHaveBeenCalled();
  });

  it("documentPreviewUrl 指向预览端点", async () => {
    const { documentPreviewUrl } = await import("../api/document");
    const url = documentPreviewUrl(7);
    expect(url).toContain("/documents/7/preview");
  });

  it("vectorizeDocument 走 POST", async () => {
    httpMock.post.mockResolvedValue({
      data: { documentId: "DOC-1", chunks: 12, replaced: false },
    });

    const { vectorizeDocument } = await import("../api/document");
    const result = await vectorizeDocument(3);

    expect(httpMock.post).toHaveBeenCalledWith("/documents/3/vectorize");
    expect(result.chunks).toBe(12);
  });
});

describe("api/document 预览鉴权", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("fetchDocumentPreview 走 httpClient（带 token）而不是裸 URL", async () => {
    // 裸 window.open(URL) 不带 Authorization ⇒ 后端判「请先登录」
    const revoke = vi.fn();
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:preview"),
      revokeObjectURL: revoke,
    });
    httpMock.get.mockResolvedValue({ data: new Blob(["<html>"]) });

    const { fetchDocumentPreview } = await import("../api/document");
    const result = await fetchDocumentPreview("DOC-ABC");

    expect(httpMock.get).toHaveBeenCalledWith("/documents/DOC-ABC/preview", {
      responseType: "blob",
    });
    expect(result.url).toBe("blob:preview");
  });
});

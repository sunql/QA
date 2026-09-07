import { describe, it, expect, vi, beforeEach } from "vitest";

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
} from "../api/document";

describe("api/document", () => {
  beforeEach(() => { vi.clearAllMocks(); });

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
    expect(call[2].headers["X-User-Id"]).toBeDefined();
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
});
import { describe, it, expect, vi, beforeEach } from "vitest";

// mock httpClient（对齐 api.test.ts 的模式）
const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  listEmbeddingProviders,
  createEmbeddingProvider,
  updateEmbeddingProvider,
  activateEmbeddingProvider,
  deactivateEmbeddingProvider,
} from "../api/embeddingProviders";
import type {
  EmbeddingProvider,
  EmbeddingProviderCreate,
  EmbeddingProviderUpdate,
} from "../types/embeddingProvider";

describe("api/embeddingProviders", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("listEmbeddingProviders 查询列表", async () => {
    const data = [{ id: 1 }] as EmbeddingProvider[];
    httpMock.get.mockResolvedValue({ data });
    const result = await listEmbeddingProviders();
    expect(httpMock.get).toHaveBeenCalledWith("/embedding-providers");
    expect(result).toEqual(data);
  });

  it("createEmbeddingProvider 发起 POST 并返回新记录", async () => {
    const payload: EmbeddingProviderCreate = {
      name: "Ollama bge-m3",
      providerType: "ollama",
      baseUrl: "http://localhost:11434/v1",
      modelName: "bge-m3:latest",
      apiKey: "ollama",
      dimension: 1024,
    };
    httpMock.post.mockResolvedValue({ data: { id: 9, ...payload } });
    const result = await createEmbeddingProvider(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/embedding-providers", payload);
    expect(result.id).toBe(9);
  });

  it("updateEmbeddingProvider 发起 PUT 并返回更新后记录", async () => {
    const payload: EmbeddingProviderUpdate = {
      baseUrl: "http://localhost:8888/v1",
      isActive: true,
    };
    httpMock.put.mockResolvedValue({ data: { id: 2, ...payload } });
    const result = await updateEmbeddingProvider(2, payload);
    expect(httpMock.put).toHaveBeenCalledWith("/embedding-providers/2", payload);
    expect(result.baseUrl).toBe("http://localhost:8888/v1");
  });

  it("activateEmbeddingProvider 发起 POST /activate", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 1, isActive: true } });
    const result = await activateEmbeddingProvider(1);
    expect(httpMock.post).toHaveBeenCalledWith("/embedding-providers/1/activate");
    expect(result.isActive).toBe(true);
  });

  it("deactivateEmbeddingProvider 发起 DELETE", async () => {
    httpMock.delete.mockResolvedValue({ data: null });
    await deactivateEmbeddingProvider(7);
    expect(httpMock.delete).toHaveBeenCalledWith("/embedding-providers/7");
  });
});

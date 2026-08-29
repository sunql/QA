import { describe, it, expect, vi, beforeEach } from "vitest";

// mock httpClient（对齐 embeddingProvidersApi.test.ts 的模式）
const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { listTerms, createTerm, deleteTerm } from "../api/termDictionary";
import type { TermDictionary, TermDictionaryCreate } from "../types/termDictionary";

describe("api/termDictionary", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("listTerms 查询术语列表", async () => {
    const data = [{ id: 1, term: "占比" }] as TermDictionary[];
    httpMock.get.mockResolvedValue({ data });
    const result = await listTerms();
    expect(httpMock.get).toHaveBeenCalledWith("/term-dictionary");
    expect(result).toEqual(data);
  });

  it("createTerm 发起 POST 并返回新术语", async () => {
    const payload: TermDictionaryCreate = {
      term: "占比",
      definition: "某值占总量的比例",
      mappedClassName: "PRECEIPTD",
      mappedPropertyName: "收货数量",
      formulaHint: "SUM(数量) / SUM(SUM(数量)) OVER ()",
    };
    httpMock.post.mockResolvedValue({ data: { id: 7, ...payload } });
    const result = await createTerm(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/term-dictionary", payload);
    expect(result.id).toBe(7);
    expect(result.term).toBe("占比");
  });

  it("deleteTerm 发起 DELETE /term-dictionary/{id}", async () => {
    httpMock.delete.mockResolvedValue({ data: null });
    await deleteTerm(3);
    expect(httpMock.delete).toHaveBeenCalledWith("/term-dictionary/3");
  });
});

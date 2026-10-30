// Wiki ↔ Ontology 链接管理 API 测试（feat-wiki-link）。
//
// 注意范式差异：本模块不走 httpClient（axios 实例），而是裸 fetch + authHeaders()
// 手动注入 X-Tenant-Id / Authorization。因此这里 mock 的是 global fetch，
// 并复用真实 authStore（照 documentApi.test.ts 的 fetch mock 范式）。

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useAuthStore } from "../stores/authStore";
import { DEFAULT_TENANT_ID } from "../config";
import {
  listWikiLinks,
  createWikiLink,
  revokeWikiLink,
  updateWikiLink,
  listLinkableTargets,
} from "../api/adminWikiLinks";

const BASE = "/api/v1/admin/wiki-links";

function jsonResponse(payload: unknown) {
  return { json: async () => payload };
}

describe("api/adminWikiLinks", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    fetchMock.mockResolvedValue(jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    useAuthStore.setState({ token: null });
  });

  it("listWikiLinks 无 filter ⇒ 不带 query string", async () => {
    const result = await listWikiLinks();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(BASE);
    expect((init as RequestInit).method).toBeUndefined();
    expect(result).toEqual([]);
  });

  it("listWikiLinks 携带 page_id filter", async () => {
    await listWikiLinks({ page_id: "PAGE-A" });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE}?page_id=PAGE-A`);
  });

  it("listWikiLinks 同时携带 page_id 与 ontology_type", async () => {
    await listWikiLinks({ page_id: "PAGE-A", ontology_type: "class" });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE}?page_id=PAGE-A&ontology_type=class`);
  });

  it("listWikiLinks 注入 auth headers（X-Tenant-Id + Authorization）", async () => {
    useAuthStore.setState({ token: "test-jwt" });
    await listWikiLinks();
    const [, init] = fetchMock.mock.calls[0];
    expect((init as RequestInit).headers).toMatchObject({
      "X-Tenant-Id": DEFAULT_TENANT_ID,
      Authorization: "Bearer test-jwt",
    });
  });

  it("createWikiLink POST /api/v1/admin/wiki-links（body 序列化为 JSON）", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 1 }));
    const body = {
      page_id: "PAGE-A",
      chunk_id: null,
      ontology_type: "class" as const,
      ontology_id: 10,
    };
    const result = await createWikiLink(body);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(BASE);
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify(body));
    expect(result).toEqual({ id: 1 });
  });

  it("revokeWikiLink DELETE /api/v1/admin/wiki-links/:id", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 3 }));
    const result = await revokeWikiLink(3);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE}/3`);
    expect(init.method).toBe("DELETE");
    expect(init.body).toBeUndefined();
    expect(result).toEqual({ id: 3 });
  });

  it("updateWikiLink PATCH /api/v1/admin/wiki-links/:id（weight / note）", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 3, weight: 0.8 }));
    const body = { weight: 0.8, note: "备注" };
    const result = await updateWikiLink(3, body);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE}/3`);
    expect(init.method).toBe("PATCH");
    expect(init.body).toBe(JSON.stringify(body));
    expect(result.weight).toBe(0.8);
  });

  it("listLinkableTargets GET /api/v1/admin/wiki-links/linkables?type=...", async () => {
    await listLinkableTargets("class");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE}/linkables?type=class`);
    expect((init as RequestInit).method).toBeUndefined();
  });

  it("listLinkableTargets 带 query ⇒ 追加 q 参数", async () => {
    await listLinkableTargets("property", "abc");
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE}/linkables?type=property&q=abc`);
  });
});

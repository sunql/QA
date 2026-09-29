import { describe, expect, it, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { fetchMenuConfig } from "../api/menuConfig";

const MOCK_RESPONSE = {
  version: "2026-09-01",
  sections: [
    {
      code: "ai",
      labelKey: "appLayout.menu.models",
      iconCode: "robot",
      sortOrder: 1,
      permissionCode: null,
      roles: [],
      path: null,
      children: [],
    },
  ],
};

describe("fetchMenuConfig", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("resolves with MenuConfig on 2xx response", async () => {
    httpMock.get.mockResolvedValue({ data: MOCK_RESPONSE });

    const result = await fetchMenuConfig();
    expect(result).toEqual(MOCK_RESPONSE);
    expect(httpMock.get).toHaveBeenCalledWith("/menu-config");
  });

  it("throws on non-2xx response", async () => {
    // httpClient 的 response 拦截器把 !success 的 ApiResponse 转换为 Error(message)
    httpMock.get.mockRejectedValue(new Error("request failed (HTTP 401)"));

    await expect(fetchMenuConfig()).rejects.toThrow("HTTP 401");
  });

  it("throws on network error", async () => {
    httpMock.get.mockRejectedValue(new Error("Network failure"));

    await expect(fetchMenuConfig()).rejects.toThrow("Network failure");
  });
});
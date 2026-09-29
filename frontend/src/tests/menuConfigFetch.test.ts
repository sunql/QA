import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { fetchMenuConfig } from "../api/menuConfig";

describe("api/menuConfig — fetchMenuConfig", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("GET /menu-config（相对路径，httpClient.baseURL 已含 /api/v1）", async () => {
    const data = { version: "2026-09-02", sections: [] };
    httpMock.get.mockResolvedValue({ data });

    const result = await fetchMenuConfig();
    // 必须传相对路径 "/menu-config"，不能拼 "/api/v1/menu-config"：
    // 否则 axios 会在 baseURL 之上再叠 /api/v1，请求变成 /api/v1/api/v1/menu-config → 404
    expect(httpMock.get).toHaveBeenCalledWith("/menu-config");
    expect(result).toEqual(data);
  });

  it("非 2xx 响应抛出 Error（含 status）", async () => {
    // 模拟 axios 抛错：httpClient 的 response 拦截器会把非 success 的 ApiResponse
    // 转换成 Error(message)，4xx/5xx 由 axios 本身 reject。
    httpMock.get.mockRejectedValue(new Error("Request failed with status code 503"));
    await expect(fetchMenuConfig()).rejects.toThrow("503");
  });
});
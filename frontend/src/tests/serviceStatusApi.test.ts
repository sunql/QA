import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { getServiceStatus } from "../api/serviceStatus";
import type { ServiceStatusResponse } from "../types/serviceStatus";

const STATUS: ServiceStatusResponse = {
  services: [
    {
      name: "postgres",
      status: "up",
      latencyMs: 12,
      endpoint: "jdbc:postgresql://db:5432/qa",
      detail: null,
    },
  ],
  checkedAt: "2026-10-30T08:00:00Z",
};

describe("api/serviceStatus", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("getServiceStatus GET /system/status", async () => {
    httpMock.get.mockResolvedValue({ data: STATUS });
    const result = await getServiceStatus();
    expect(httpMock.get).toHaveBeenCalledWith("/system/status");
    expect(result).toEqual(STATUS);
  });

  it("请求失败时把后端错误原样抛出（不吞异常）", async () => {
    httpMock.get.mockRejectedValue(new Error("Request failed with status code 500"));
    await expect(getServiceStatus()).rejects.toThrow("500");
  });
});

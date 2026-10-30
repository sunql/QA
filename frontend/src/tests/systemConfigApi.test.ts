import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  listSystemConfig,
  getSystemConfig,
  updateSystemConfig,
} from "../api/systemConfig";

describe("api/systemConfig", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listSystemConfig GET /admin/system-config", async () => {
    httpMock.get.mockResolvedValue({
      data: [
        {
          key: "KPI_TECHNICAL_FORM_ENABLED",
          value: "true",
          description: "开关",
          updatedTime: "2026-10-30T00:00:00Z",
        },
      ],
    });
    const result = await listSystemConfig();
    expect(httpMock.get).toHaveBeenCalledWith("/admin/system-config");
    expect(result).toHaveLength(1);
    expect(result[0].key).toBe("KPI_TECHNICAL_FORM_ENABLED");
  });

  it("getSystemConfig GET /admin/system-config/:key", async () => {
    httpMock.get.mockResolvedValue({
      data: {
        key: "SOME_KEY",
        value: null,
        description: null,
        updatedTime: "2026-10-30T00:00:00Z",
      },
    });
    const result = await getSystemConfig("SOME_KEY");
    expect(httpMock.get).toHaveBeenCalledWith("/admin/system-config/SOME_KEY");
    expect(result.value).toBeNull();
  });

  it("updateSystemConfig PUT /admin/system-config/:key", async () => {
    httpMock.put.mockResolvedValue({
      data: {
        key: "SOME_KEY",
        value: "new",
        description: null,
        updatedTime: "2026-10-30T00:00:00Z",
      },
    });
    const result = await updateSystemConfig("SOME_KEY", { value: "new" });
    expect(httpMock.put).toHaveBeenCalledWith("/admin/system-config/SOME_KEY", {
      value: "new",
    });
    expect(result.value).toBe("new");
  });

  it("updateSystemConfig 允许把 value 置空（null 原样透传）", async () => {
    httpMock.put.mockResolvedValue({
      data: {
        key: "SOME_KEY",
        value: null,
        description: null,
        updatedTime: "2026-10-30T00:00:00Z",
      },
    });
    await updateSystemConfig("SOME_KEY", { value: null });
    expect(httpMock.put).toHaveBeenCalledWith("/admin/system-config/SOME_KEY", {
      value: null,
    });
  });
});

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fetchMenuConfig } from "../api/menuConfig";

describe("api/menuConfig — fetchMenuConfig", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("GET /api/v1/menu-config 并解析 JSON", async () => {
    const data = { version: "2026-09-02", sections: [] };
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => data,
    });

    const result = await fetchMenuConfig();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/menu-config",
      expect.objectContaining({
        headers: { Accept: "application/json" },
        credentials: "include",
      }),
    );
    expect(result).toEqual(data);
  });

  it("非 OK 响应抛出 Error（含 status）", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 503,
      statusText: "Service Unavailable",
    });

    await expect(fetchMenuConfig()).rejects.toThrow(
      /fetchMenuConfig failed: 503/,
    );
  });
});
import { describe, expect, it, vi } from "vitest";
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
  it("resolves with MenuConfig on 2xx response", async () => {
    const jsonStub = vi.fn().mockResolvedValue(MOCK_RESPONSE);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: jsonStub,
      status: 200,
      statusText: "OK",
    }));

    const result = await fetchMenuConfig();
    expect(result).toEqual(MOCK_RESPONSE);
    expect(jsonStub).toHaveBeenCalled();
  });

  it("throws on non-2xx response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      statusText: "Unauthorized",
    }));

    await expect(fetchMenuConfig()).rejects.toThrow("fetchMenuConfig failed: 401 Unauthorized");
  });

  it("throws on network error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Network failure")));

    await expect(fetchMenuConfig()).rejects.toThrow("Network failure");
  });
});

import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { fetchCurrentUserMe } from "../api/userProfile";
import type { CurrentUserMe } from "../api/userProfile";

const CURRENT_USER: CurrentUserMe = {
  userId: "u-1001",
  displayName: "张三",
  email: "zhangsan@example.com",
  roleCodes: ["ADMIN"],
  departmentCodes: ["D-1"],
  dbUserId: 42,
};

describe("api/userProfile", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("fetchCurrentUserMe GET /users/me（当前用户身份，非 admin-only）", async () => {
    httpMock.get.mockResolvedValue({ data: CURRENT_USER });
    const result = await fetchCurrentUserMe();
    expect(httpMock.get).toHaveBeenCalledWith("/users/me");
    expect(result).toEqual(CURRENT_USER);
  });

  it("桩回退场景：dbUserId 为 null 时仍原样返回", async () => {
    const stubUser: CurrentUserMe = {
      ...CURRENT_USER,
      displayName: "u-1001",
      email: null,
      dbUserId: null,
    };
    httpMock.get.mockResolvedValue({ data: stubUser });
    const result = await fetchCurrentUserMe();
    expect(result.dbUserId).toBeNull();
    expect(result.email).toBeNull();
  });

  it("请求失败时把后端错误原样抛出（不吞异常）", async () => {
    httpMock.get.mockRejectedValue(new Error("Request failed with status code 401"));
    await expect(fetchCurrentUserMe()).rejects.toThrow("401");
  });
});

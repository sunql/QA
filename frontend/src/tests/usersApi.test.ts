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
  listUsers,
  createUser,
  updateUser,
  deleteUser,
  setUserRoles,
  setUserOrganizations,
  setUserPermissions,
  adminResetPassword,
  getUserEffectivePermissions,
} from "../api/users";

describe("api/users", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listUsers GET /users", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    const result = await listUsers();
    expect(httpMock.get).toHaveBeenCalledWith("/users");
    expect(result).toEqual([]);
  });

  it("createUser POST /users", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 7, username: "bob" } });
    const payload = {
      username: "bob",
      displayName: "Bob",
      password: "Secret123",
    };
    const result = await createUser(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/users", payload);
    expect(result.id).toBe(7);
  });

  it("updateUser PUT /users/:userId", async () => {
    httpMock.put.mockResolvedValue({ data: { id: 7, displayName: "Bobby" } });
    await updateUser(7, { displayName: "Bobby", enabled: false });
    expect(httpMock.put).toHaveBeenCalledWith("/users/7", {
      displayName: "Bobby",
      enabled: false,
    });
  });

  it("deleteUser DELETE /users/:userId", async () => {
    httpMock.delete.mockResolvedValue({});
    await deleteUser(7);
    expect(httpMock.delete).toHaveBeenCalledWith("/users/7");
  });

  it("setUserRoles PUT /users/:userId/roles", async () => {
    httpMock.put.mockResolvedValue({});
    await setUserRoles(7, { roleIds: [1, 2] });
    expect(httpMock.put).toHaveBeenCalledWith("/users/7/roles", {
      roleIds: [1, 2],
    });
  });

  it("setUserOrganizations PUT /users/:userId/organizations", async () => {
    httpMock.put.mockResolvedValue({});
    await setUserOrganizations(7, { organizationIds: [3] });
    expect(httpMock.put).toHaveBeenCalledWith("/users/7/organizations", {
      organizationIds: [3],
    });
  });

  it("setUserPermissions PUT /users/:userId/permissions", async () => {
    httpMock.put.mockResolvedValue({});
    await setUserPermissions(7, { menuCodes: ["kpi:read"] });
    expect(httpMock.put).toHaveBeenCalledWith("/users/7/permissions", {
      menuCodes: ["kpi:read"],
    });
  });

  it("adminResetPassword PUT /users/:userId/password", async () => {
    httpMock.put.mockResolvedValue({});
    await adminResetPassword(42, {
      newPassword: "NewPass456",
      forceChangeOnNextLogin: true,
    });
    expect(httpMock.put).toHaveBeenCalledWith("/users/42/password", {
      newPassword: "NewPass456",
      forceChangeOnNextLogin: true,
    });
  });

  it("getUserEffectivePermissions GET /users/:userId/permissions", async () => {
    httpMock.get.mockResolvedValue({
      data: {
        userId: 7,
        isSuperuser: false,
        roleCodes: [],
        organizationCodes: [],
        menuCodes: [],
        directGrants: [],
        roleGrants: [],
        organizationGrants: [],
      },
    });
    const result = await getUserEffectivePermissions(7);
    expect(httpMock.get).toHaveBeenCalledWith("/users/7/permissions");
    expect(result.roleCodes).toEqual([]);
  });
});

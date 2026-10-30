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
  listRoles,
  createRole,
  updateRole,
  deleteRole,
  setRolePermissions,
  getRolePermissions,
} from "../api/roles";

describe("api/roles", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listRoles GET /roles", async () => {
    httpMock.get.mockResolvedValue({ data: [{ id: 1, code: "ADMIN", name: "管理员" }] });
    const result = await listRoles();
    expect(httpMock.get).toHaveBeenCalledWith("/roles");
    expect(result).toHaveLength(1);
    expect(result[0].code).toBe("ADMIN");
  });

  it("createRole POST /roles", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 3, code: "AUDITOR", name: "审计员" } });
    const payload = { code: "AUDITOR", name: "审计员", description: "只读" };
    const result = await createRole(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/roles", payload);
    expect(result.id).toBe(3);
  });

  it("updateRole PUT /roles/:id", async () => {
    httpMock.put.mockResolvedValue({ data: { id: 3, name: "新名" } });
    await updateRole(3, { name: "新名" });
    expect(httpMock.put).toHaveBeenCalledWith("/roles/3", { name: "新名" });
  });

  it("deleteRole DELETE /roles/:id", async () => {
    httpMock.delete.mockResolvedValue({});
    await deleteRole(3);
    expect(httpMock.delete).toHaveBeenCalledWith("/roles/3");
  });

  it("setRolePermissions PUT /roles/:id/permissions", async () => {
    httpMock.put.mockResolvedValue({});
    await setRolePermissions(3, { menuCodes: ["m1", "m2"] });
    expect(httpMock.put).toHaveBeenCalledWith("/roles/3/permissions", {
      menuCodes: ["m1", "m2"],
    });
  });

  it("getRolePermissions GET /roles/:id/permissions", async () => {
    httpMock.get.mockResolvedValue({ data: { subjectId: 3, menuCodes: ["m1"] } });
    const result = await getRolePermissions(3);
    expect(httpMock.get).toHaveBeenCalledWith("/roles/3/permissions");
    expect(result.subjectId).toBe(3);
  });
});

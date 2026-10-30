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
  listOrganizations,
  listOrganizationTree,
  createOrganization,
  updateOrganization,
  deleteOrganization,
  setOrganizationPermissions,
  getOrganizationPermissions,
} from "../api/organizations";

describe("api/organizations", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listOrganizations GET /organizations", async () => {
    httpMock.get.mockResolvedValue({ data: [{ id: 1, code: "HQ", name: "总部" }] });
    const result = await listOrganizations();
    expect(httpMock.get).toHaveBeenCalledWith("/organizations");
    expect(result).toHaveLength(1);
    expect(result[0].code).toBe("HQ");
  });

  it("listOrganizationTree GET /organizations/tree", async () => {
    httpMock.get.mockResolvedValue({
      data: [{ id: 1, code: "HQ", name: "总部", sortOrder: 0, description: null, children: [] }],
    });
    const result = await listOrganizationTree();
    expect(httpMock.get).toHaveBeenCalledWith("/organizations/tree");
    expect(result[0].children).toEqual([]);
  });

  it("createOrganization POST /organizations", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 2, code: "SUB", name: "分公司" } });
    const payload = { code: "SUB", name: "分公司", parentId: 1, sortOrder: 10 };
    const result = await createOrganization(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/organizations", payload);
    expect(result.id).toBe(2);
  });

  it("updateOrganization PUT /organizations/:id", async () => {
    httpMock.put.mockResolvedValue({ data: { id: 2, name: "新名" } });
    await updateOrganization(2, { name: "新名" });
    expect(httpMock.put).toHaveBeenCalledWith("/organizations/2", { name: "新名" });
  });

  it("deleteOrganization DELETE /organizations/:id", async () => {
    httpMock.delete.mockResolvedValue({});
    await deleteOrganization(2);
    expect(httpMock.delete).toHaveBeenCalledWith("/organizations/2");
  });

  it("setOrganizationPermissions PUT /organizations/:id/permissions", async () => {
    httpMock.put.mockResolvedValue({});
    await setOrganizationPermissions(2, { menuCodes: ["m1", "m2"] });
    expect(httpMock.put).toHaveBeenCalledWith("/organizations/2/permissions", {
      menuCodes: ["m1", "m2"],
    });
  });

  it("getOrganizationPermissions GET /organizations/:id/permissions", async () => {
    httpMock.get.mockResolvedValue({ data: { subjectId: 2, menuCodes: ["m1"] } });
    const result = await getOrganizationPermissions(2);
    expect(httpMock.get).toHaveBeenCalledWith("/organizations/2/permissions");
    expect(result.menuCodes).toEqual(["m1"]);
  });
});

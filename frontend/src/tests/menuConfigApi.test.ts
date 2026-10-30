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
  fetchMenuConfig,
  listMenuRows,
  listMenuTree,
  createMenu,
  updateMenu,
  deleteMenu,
} from "../api/menuConfig";
import type { MenuConfig, MenuConfigTreeNode, MenuRow } from "../types/menuConfig";

const MENU_CONFIG: MenuConfig = {
  version: "2026-09-01",
  sections: [],
};

const MENU_ROW: MenuRow = {
  id: 11,
  code: "model.chat",
  parentId: 1,
  parentCode: "model",
  labelKey: "appLayout.menu.models.chat",
  iconCode: "chat",
  sortOrder: 2,
  path: "/chat",
  permissionCode: "chat:read",
  visible: true,
  hasChildren: false,
};

const MENU_TREE_NODE: MenuConfigTreeNode = {
  code: "model",
  labelKey: "appLayout.menu.models",
  iconCode: null,
  sortOrder: 1,
  path: null,
  permissionCode: null,
  visible: true,
  parentCode: null,
  children: [],
};

describe("api/menuConfig", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("fetchMenuConfig GET /menu-config（不带 /api/v1 前缀）", async () => {
    httpMock.get.mockResolvedValue({ data: MENU_CONFIG });
    const result = await fetchMenuConfig();
    expect(httpMock.get).toHaveBeenCalledWith("/menu-config");
    expect(result).toEqual(MENU_CONFIG);
  });

  it("listMenuRows GET /menu-config/admin（管理面全量扁平行）", async () => {
    httpMock.get.mockResolvedValue({ data: [MENU_ROW] });
    const result = await listMenuRows();
    expect(httpMock.get).toHaveBeenCalledWith("/menu-config/admin");
    expect(result).toEqual([MENU_ROW]);
  });

  it("listMenuTree GET /menu-config/tree（嵌套树形结构）", async () => {
    httpMock.get.mockResolvedValue({ data: [MENU_TREE_NODE] });
    const result = await listMenuTree();
    expect(httpMock.get).toHaveBeenCalledWith("/menu-config/tree");
    expect(result).toEqual([MENU_TREE_NODE]);
  });

  it("createMenu POST /menu-config（透传 payload）", async () => {
    httpMock.post.mockResolvedValue({ data: MENU_ROW });
    const payload = {
      code: "model.chat",
      labelKey: "appLayout.menu.models.chat",
      path: "/chat",
      parentCode: "model",
    };
    const result = await createMenu(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/menu-config", payload);
    expect(result).toEqual(MENU_ROW);
  });

  it("updateMenu PUT /menu-config/:code（code 做 URL 编码）", async () => {
    httpMock.put.mockResolvedValue({ data: MENU_ROW });
    const result = await updateMenu("model/chat v2", { labelKey: "appLayout.menu.models.chat" });
    expect(httpMock.put).toHaveBeenCalledWith(
      "/menu-config/model%2Fchat%20v2",
      { labelKey: "appLayout.menu.models.chat" },
    );
    expect(result).toEqual(MENU_ROW);
  });

  it("deleteMenu DELETE /menu-config/:code（code 做 URL 编码）", async () => {
    httpMock.delete.mockResolvedValue({});
    await deleteMenu("model/chat v2");
    expect(httpMock.delete).toHaveBeenCalledWith("/menu-config/model%2Fchat%20v2");
  });

  it("请求失败时把后端错误原样抛出（不吞异常）", async () => {
    httpMock.get.mockRejectedValue(new Error("Request failed with status code 503"));
    await expect(listMenuRows()).rejects.toThrow("503");
  });
});

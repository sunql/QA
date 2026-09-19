/**
 * OntologyPropertyAdminPage 行为测试（jsdom 渲染）。
 *
 * 2026-09-18 报障回归：编辑属性描述「保存成功但重开为空」。
 * 两层候选根因：
 * 1. openEdit 不回填 description（已修：rec.description ?? ""）
 * 2. destroyOnHidden 下 Form 未挂载时 setFieldsValue 被丢弃（antd 时序，
 *    ClassTab 有同款注释修复：写入时机放到 Modal.afterOpenChange(true)）
 * 本测试锁定：点击「编辑」后弹窗 textarea 必须显示现有描述。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import OntologyPropertyAdminPage from "../OntologyPropertyAdminPage";
import type { OntologyClass, OntologyProperty } from "../../types/ontology";

const mockClass: OntologyClass = {
  id: 99,
  className: "DIM_SUPPLIER",
  classAlias: null,
  description: null,
  sourceTable: "DIM_SUPPLIER",
  parentClassId: null,
  objectType: null,
  objectOwner: null,
  createdBy: null,
  createdTime: "2026-09-18T00:00:00Z",
  updatedTime: "2026-09-18T00:00:00Z",
  version: 1,
  validFrom: "2026-09-18T00:00:00Z",
  validTo: null,
};

const mockProperty: OntologyProperty = {
  id: 4918,
  classId: 99,
  propertyName: "BPSNAM_0",
  propertyAlias: "供应商名称",
  dataType: "STRING",
  description: "供应商名称（Sage X3 BPSNAM，公司全称）",
  isPrimaryKey: false,
  isForeignKey: false,
  refClassId: null,
  sourceColumn: "BPSNAM_0",
  createdTime: "2026-09-18T00:00:00Z",
  updatedTime: "2026-09-18T00:00:00Z",
};

const api = vi.hoisted(() => ({
  listAllProperties: vi.fn(),
  listClasses: vi.fn(),
  updateProperty: vi.fn(),
}));

vi.mock("../../api/ontology", () => api);

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <OntologyPropertyAdminPage />
    </ConfigProvider>,
  );
}

describe("OntologyPropertyAdminPage — 编辑回填行为", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listAllProperties.mockResolvedValue([mockProperty]);
    api.listClasses.mockResolvedValue([mockClass]);
    api.updateProperty.mockResolvedValue({ ...mockProperty });
  });

  it("点击编辑后弹窗 textarea 必须回填现有描述", async () => {
    renderPage();
    expect(await screen.findByText("BPSNAM_0")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /编辑/ }));

    const modal = await screen.findByRole("dialog");
    expect(modal).toBeInTheDocument();

    await waitFor(() => {
      const textarea = document.body.querySelector("textarea");
      expect(textarea).not.toBeNull();
      expect((textarea as HTMLTextAreaElement).value).toContain("Sage X3 BPSNAM");
    });
  });

  it("修改描述保存后调用 updateProperty 携带新描述", async () => {
    renderPage();
    expect(await screen.findByText("BPSNAM_0")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /编辑/ }));
    await screen.findByRole("dialog");

    const textarea = document.body.querySelector("textarea") as HTMLTextAreaElement;
    await waitFor(() => expect(textarea.value).not.toBe(""));

    await user.clear(textarea);
    if (document.activeElement !== textarea) textarea.focus();
    await user.type(textarea, "新的描述内容");
    await user.click(screen.getByRole("button", { name: /保 存|保存/ }));

    await waitFor(() => expect(api.updateProperty).toHaveBeenCalled());
    const payload = api.updateProperty.mock.calls[0][1];
    expect(payload.description).toBe("新的描述内容");
  });
});

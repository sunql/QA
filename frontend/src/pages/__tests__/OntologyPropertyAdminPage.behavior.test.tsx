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

const mockPropertyOther: OntologyProperty = {
  id: 4919,
  classId: 100,
  propertyName: "BPSNUM_0",
  propertyAlias: "供应商编号",
  dataType: "STRING",
  description: "供应商唯一编号（Sage X3 BPSNUM）",
  isPrimaryKey: false,
  isForeignKey: false,
  refClassId: null,
  sourceColumn: "BPSNUM_0",
  createdTime: "2026-09-18T00:00:00Z",
  updatedTime: "2026-09-18T00:00:00Z",
};

const mockClassOther: OntologyClass = {
  id: 100,
  className: "DIM_SUPPLIER_ID",
  classAlias: null,
  description: null,
  sourceTable: "DIM_SUPPLIER_ID",
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

  it("编辑弹窗标题必须包含属性名，不留 {{name}} 字面量", async () => {
    // 2026-09-19 报障：编辑属性弹窗标题渲染成「编辑属性：{{name}}」，
    // 根因是 i18n 字符串写的是 {{name}}（双大括号）但 i18n 配置 prefix/suffix
    // 是单大括号 {name}，react-i18next 找不到占位符就字面残留。
    // 修复：把 4 处 i18n 字符串改为单大括号（modal.editTitle / ruleCodeAutoHint × 2 lang）。
    renderPage();
    expect(await screen.findByText("BPSNAM_0")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /编辑/ }));
    const modal = await screen.findByRole("dialog");
    expect(modal).toBeInTheDocument();

    // antd Modal 标题渲染为 .ant-modal-title；存在性 + 内容双重断言。
    const modalTitle = modal.querySelector(".ant-modal-title");
    expect(modalTitle).not.toBeNull();
    expect(modalTitle!.textContent).toContain("BPSNAM_0");
    expect(modalTitle!.textContent).not.toContain("{{name}}");
    expect(modalTitle!.textContent).not.toContain("{{");
  });

  it("关键字过滤（feat-ontology-property-search）：按 propertyName / alias / 描述模糊命中", async () => {
    // 2026-09-19 加需求：本体属性管理加搜索框，可对属性名/别名/物理列/描述
    // 做不区分大小写的子串过滤。本测试锁定核心 4 个场景：
    //  1. 默认全显（2 条属性都在）
    //  2. 搜 propertyName 命中其一
    //  3. 搜 description 命中其一
    //  4. 搜别名（propertyAlias）命中
    //  5. 关键字不匹配时表格空
    api.listAllProperties.mockResolvedValue([mockProperty, mockPropertyOther]);
    api.listClasses.mockResolvedValue([mockClass, mockClassOther]);
    renderPage();
    expect(await screen.findByText("BPSNAM_0")).toBeInTheDocument();
    expect(screen.getByText("BPSNUM_0")).toBeInTheDocument();

    const search = screen.getByPlaceholderText(/搜索属性名/) as HTMLInputElement;
    const user = userEvent.setup();

    // 1. propertyName 子串
    await user.clear(search);
    await user.type(search, "BPSNUM");
    await waitFor(() => {
      expect(screen.queryByText("BPSNAM_0")).not.toBeInTheDocument();
      expect(screen.getByText("BPSNUM_0")).toBeInTheDocument();
    });

    // 2. description 子串（"Sage X3 BPSNAM" 含 BPSNAM）
    await user.clear(search);
    await user.type(search, "Sage X3 BPSNAM");
    await waitFor(() => {
      expect(screen.getByText("BPSNAM_0")).toBeInTheDocument();
      expect(screen.queryByText("BPSNUM_0")).not.toBeInTheDocument();
    });

    // 3. 别名「供应商编号」
    await user.clear(search);
    await user.type(search, "供应商编号");
    await waitFor(() => {
      expect(screen.queryByText("BPSNAM_0")).not.toBeInTheDocument();
      expect(screen.getByText("BPSNUM_0")).toBeInTheDocument();
    });

    // 4. 不匹配 → 表空（两个属性都消失）
    await user.clear(search);
    await user.type(search, "ZZZ_NOTHING_MATCHES");
    await waitFor(() => {
      expect(screen.queryByText("BPSNAM_0")).not.toBeInTheDocument();
      expect(screen.queryByText("BPSNUM_0")).not.toBeInTheDocument();
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

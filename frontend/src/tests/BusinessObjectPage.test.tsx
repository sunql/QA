import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../i18n";

const { listClassesMock } = vi.hoisted(() => ({ listClassesMock: vi.fn() }));
vi.mock("../api/businessObject", () => ({
  listBusinessObjects: vi.fn().mockResolvedValue([]),
  createBusinessObject: vi.fn().mockResolvedValue({}),
  updateBusinessObject: vi.fn().mockResolvedValue({}),
  deleteBusinessObject: vi.fn(),
}));
vi.mock("../api/ontology", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/ontology")>();
  return { ...actual, listClasses: listClassesMock };
});

import BusinessObjectPage from "../pages/BusinessObjectPage";

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <ConfigProvider locale={zhCN}>
    <I18nextProvider i18n={i18n}>{children}</I18nextProvider>
  </ConfigProvider>
);

const supplierClass = {
  id: 11,
  className: "Supplier",
  classAlias: "供应商",
  description: null,
  sourceTable: null,
  parentClassId: null,
  objectType: "Master",
  objectOwner: null,
  createdBy: null,
  createdTime: null,
  updatedTime: null,
  version: 1,
  validFrom: null,
  validTo: null,
};
const purchaseOrderClass = {
  id: 22,
  className: "PurchaseOrder",
  classAlias: "采购订单",
  description: null,
  sourceTable: null,
  parentClassId: null,
  objectType: "Transaction",
  objectOwner: null,
  createdBy: null,
  createdTime: null,
  updatedTime: null,
  version: 1,
  validFrom: null,
  validTo: null,
};

const modalScope = () => {
  const modal = document.querySelector(".ant-modal-content") as HTMLElement;
  if (!modal) throw new Error("modal not found");
  return modal;
};

describe("BusinessObjectPage", () => {
  beforeEach(async () => {
    listClassesMock.mockReset();
    listClassesMock.mockResolvedValue([supplierClass, purchaseOrderClass]);
    const { listBusinessObjects } = await import("../api/businessObject");
    vi.mocked(listBusinessObjects).mockReset();
    vi.mocked(listBusinessObjects).mockResolvedValue([
      {
        code: "SUPPLIER",
        name: "供应商",
        graphLabel: "Supplier",
        headerClassId: null,
        description: null,
        createdTime: "2026-09-04T00:00:00Z",
        updatedTime: "2026-09-04T00:00:00Z",
      },
    ]);
  });

  it("renders title and new button", async () => {
    render(<BusinessObjectPage />, { wrapper });
    await waitFor(() => {
      expect(screen.getByText("业务对象")).toBeInTheDocument();
    });
  });

  it("loads and renders business objects", async () => {
    const { listBusinessObjects } = await import("../api/businessObject");
    vi.mocked(listBusinessObjects).mockResolvedValue([
      {
        code: "SUPPLIER",
        name: "供应商",
        graphLabel: "Supplier",
        headerClassId: null,
        description: null,
        createdTime: "2026-09-04T00:00:00Z",
        updatedTime: "2026-09-04T00:00:00Z",
      },
    ]);
    render(<BusinessObjectPage />, { wrapper });
    await waitFor(() => {
      expect(screen.getByText("供应商")).toBeInTheDocument();
    });
  });

  it("打开新建弹窗时加载本体类列表（一次）", async () => {
    render(<BusinessObjectPage />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /新建业务对象/ }));
    await waitFor(() => expect(listClassesMock).toHaveBeenCalled());
    expect(listClassesMock).toHaveBeenCalledTimes(1);
  });

  it("headerClassId 下拉框含 showSearch，可按 className 与 classAlias 模糊过滤", async () => {
    render(<BusinessObjectPage />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /新建业务对象/ }));
    await waitFor(() => expect(listClassesMock).toHaveBeenCalled());

    // 限定到 modal 内（避免与 Table column header 冲突）
    const modal = await waitFor(() => modalScope());
    // 用 Form.Item label 精确定位 headerClassId 对应的 ant-select
    const headerClassItem = (within(modal).getByText("头表类") as HTMLElement).closest(
      ".ant-form-item",
    ) as HTMLElement;
    const headerSelect = headerClassItem.querySelector(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(headerSelect);

    // 两条都应该可见（dropdown 渲染到 body，但文字可全局查找）
    await within(document.body).findByText("Supplier（供应商）");
    expect(within(document.body).queryByText("PurchaseOrder（采购订单）")).toBeInTheDocument();

    // showSearch 的搜索输入框（antd 渲染在 selector 内）
    const searchInput = headerClassItem.querySelector(
      "input.ant-select-selection-search-input",
    ) as HTMLInputElement;

    // 输入 "采购" → Supplier 被过滤，PurchaseOrder 保留
    fireEvent.change(searchInput, { target: { value: "采购" } });
    await waitFor(() => {
      expect(within(document.body).queryByText("Supplier（供应商）")).toBeNull();
    });
    expect(within(document.body).queryByText("PurchaseOrder（采购订单）")).toBeInTheDocument();

    // "sup" 大小写不敏感匹配 Supplier
    fireEvent.change(searchInput, { target: { value: "sup" } });
    await waitFor(() => {
      expect(within(document.body).queryByText("Supplier（供应商）")).toBeInTheDocument();
    });
    expect(within(document.body).queryByText("PurchaseOrder（采购订单）")).toBeNull();
  });

  it("选择 headerClassId 后 graphLabel 自动填充本体类名，且 graphLabel 输入框被禁用", async () => {
    render(<BusinessObjectPage />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /新建业务对象/ }));
    await waitFor(() => expect(listClassesMock).toHaveBeenCalled());

    const modal = await waitFor(() => modalScope());
    const headerClassItem = within(modal).getByText("头表类").closest(".ant-form-item") as HTMLElement;
    const headerSelect = headerClassItem.querySelector(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(headerSelect);
    fireEvent.click(await within(document.body).findByText("Supplier（供应商）"));

    // graphLabel 字段应该是 Input.disabled，且值为 "Supplier"
    const graphLabelInput = (await screen.findByDisplayValue("Supplier")) as HTMLInputElement;
    expect(graphLabelInput).toBeDisabled();
  });

  it("清空 headerClassId 时 graphLabel 也被清空", async () => {
    render(<BusinessObjectPage />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /新建业务对象/ }));
    await waitFor(() => expect(listClassesMock).toHaveBeenCalled());

    const modal = await waitFor(() => modalScope());
    const headerClassItem = within(modal).getByText("头表类").closest(".ant-form-item") as HTMLElement;
    const headerSelect = headerClassItem.querySelector(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(headerSelect);
    fireEvent.click(await within(document.body).findByText("Supplier（供应商）"));
    await screen.findByDisplayValue("Supplier");

    // antd 在选中后才把 .ant-select-clear 放进 DOM；先等它出现
    const clearIcon = await waitFor(() => {
      const el = headerClassItem.querySelector(".ant-select-clear") as HTMLElement | null;
      if (!el) throw new Error("clear icon not rendered yet");
      return el;
    });
    fireEvent.mouseDown(clearIcon);

    await waitFor(() => {
      expect(screen.queryByDisplayValue("Supplier")).toBeNull();
    });
  });

  it("提交时 payload 携带 headerClassId + 自动派生的 graphLabel", async () => {
    const { createBusinessObject } = await import("../api/businessObject");
    vi.mocked(createBusinessObject).mockClear();
    render(<BusinessObjectPage />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /新建业务对象/ }));
    await waitFor(() => expect(listClassesMock).toHaveBeenCalled());

    const modal = await waitFor(() => modalScope());

    // 填 code — 现在是自由输入的 Input（必须能输入新 code，否则无新增 UI）
    const codeInput = within(modal).getByLabelText("代码") as HTMLInputElement;
    fireEvent.change(codeInput, { target: { value: "INVOICE" } });

    const nameInput = within(modal).getByLabelText("名称") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "发票对象" } });

    // 选 headerClassId → Supplier
    const headerClassItem = within(modal).getByText("头表类").closest(".ant-form-item") as HTMLElement;
    const headerSelect = headerClassItem.querySelector(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(headerSelect);
    fireEvent.click(await within(document.body).findByText("Supplier（供应商）"));

    // 提交
    fireEvent.click(within(modal).getByRole("button", { name: /确\s*定/ }));

    await waitFor(() => expect(createBusinessObject).toHaveBeenCalled());
    const payload = vi.mocked(createBusinessObject).mock.calls[0][0];
    expect(payload.code).toBe("INVOICE");
    expect(payload.name).toBe("发票对象");
    expect(payload.headerClassId).toBe(11);
    expect(payload.graphLabel).toBe("Supplier");
  });

  it("新建时 code 字段必须是自由输入（不是已有 code 的下拉），且格式非法时阻止提交", async () => {
    const { createBusinessObject } = await import("../api/businessObject");
    vi.mocked(createBusinessObject).mockClear();
    render(<BusinessObjectPage />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /新建业务对象/ }));
    await waitFor(() => expect(listClassesMock).toHaveBeenCalled());

    const modal = await waitFor(() => modalScope());

    // code 字段是 <input>，不是 <select>
    const codeInput = within(modal).getByLabelText("代码") as HTMLInputElement;
    expect(codeInput.tagName).toBe("INPUT");
    expect(codeInput.hasAttribute("disabled")).toBe(false);

    // 格式非法（小写 + 含 -）→ 提交时应被前端校验拦截，不调用 API
    fireEvent.change(codeInput, { target: { value: "bad-code" } });
    const nameInput = within(modal).getByLabelText("名称") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "非法 code 测试" } });
    fireEvent.click(within(modal).getByRole("button", { name: /确\s*定/ }));

    // 等待验证错误出现
    await waitFor(() => {
      expect(within(modal).getByText(/代码必须为大写/)).toBeInTheDocument();
    });
    expect(createBusinessObject).not.toHaveBeenCalled();

    // 修正为合法 code（INVOICE_2026） → 提交成功
    fireEvent.change(codeInput, { target: { value: "INVOICE_2026" } });
    fireEvent.click(within(modal).getByRole("button", { name: /确\s*定/ }));
    await waitFor(() => expect(createBusinessObject).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createBusinessObject).mock.calls[0][0].code).toBe("INVOICE_2026");
  });

  it("编辑模式 code 字段被禁用", async () => {
    render(<BusinessObjectPage />, { wrapper });
    await waitFor(() => expect(screen.getByText("供应商")).toBeInTheDocument());

    // 限定到表格行内（避免与其他同名按钮冲突）；antd 中文按钮默认字间加空格，用正则匹配
    const supplierRow = screen.getByText("供应商").closest("tr") as HTMLElement;
    const editButton = within(supplierRow).getByRole("button", { name: /编\s*辑/ });
    fireEvent.click(editButton);
    const modal = await waitFor(() => modalScope());

    const codeInput = within(modal).getByLabelText("代码") as HTMLInputElement;
    expect(codeInput.tagName).toBe("INPUT");
    expect(codeInput).toBeDisabled();
    expect(codeInput.value).toBe("SUPPLIER");
  });
});

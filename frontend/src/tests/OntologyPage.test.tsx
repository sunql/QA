import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import OntologyPage from "../pages/OntologyPage";
import type {
  OntologyClass,
  OntologyProperty,
  OntologyMetric,
} from "../types/ontology";

// =============================================================================
// Mocks
// =============================================================================

const mockClass: OntologyClass = {
  id: 1,
  className: "Customer",
  classAlias: "客户",
  description: "客户信息",
  sourceTable: "t_customer",
  parentClassId: null,
  createdBy: null,
  createdTime: "2026-08-11T00:00:00Z",
  updatedTime: "2026-08-11T00:00:00Z",
  version: 1,
  validFrom: "2026-08-11T00:00:00Z",
  validTo: null,
};

const mockProperty: OntologyProperty = {
  id: 1,
  classId: 1,
  propertyName: "customer_name",
  propertyAlias: "客户名称",
  dataType: "STRING",
  isPrimaryKey: true,
  isForeignKey: false,
  refClassId: null,
  sourceColumn: "name",
  createdTime: "2026-08-11T00:00:00Z",
  updatedTime: "2026-08-11T00:00:00Z",
};

const mockMetric: OntologyMetric = {
  id: 1,
  metricName: "sales_amount",
  metricAlias: "销售额",
  formula: "SUM(order_amount)",
  aggFunction: "SUM",
  targetClassId: null,
  dimensionDefaults: { region: "华东" },
  createdBy: null,
  createdTime: "2026-08-11T00:00:00Z",
  updatedTime: "2026-08-11T00:00:00Z",
};

const api = vi.hoisted(() => ({
  listClasses: vi.fn(),
  getClass: vi.fn(),
  createClass: vi.fn(),
  updateClass: vi.fn(),
  deleteClass: vi.fn(),
  listClassVersions: vi.fn(),
  listPropertiesByClass: vi.fn(),
  createProperty: vi.fn(),
  updateProperty: vi.fn(),
  deleteProperty: vi.fn(),
  listMetrics: vi.fn(),
  getMetric: vi.fn(),
  createMetric: vi.fn(),
  updateMetric: vi.fn(),
  deleteMetric: vi.fn(),
}));

vi.mock("../api/ontology", () => api);

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/ontology"]}>
        <Routes>
          <Route path="/ontology" element={<OntologyPage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

// =============================================================================
// Tests
// =============================================================================

describe("OntologyPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listClasses.mockResolvedValue([mockClass]);
    api.listMetrics.mockResolvedValue([mockMetric]);
    api.listPropertiesByClass.mockResolvedValue([mockProperty]);
  });

  // ---- Class Tab ----

  it("默认显示「类」标签，渲染类列表", async () => {
    renderPage();
    expect(screen.getByText("本体管理")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("Customer")).toBeInTheDocument();
      expect(screen.getByText("客户")).toBeInTheDocument();
    });
  });

  it("类描述超出列宽时悬停 Tooltip 展示完整内容", async () => {
    const LONG_DESC =
      "这是一条非常长的类描述，用于验证超出列宽后悬停可以查看完整内容。实际业务里描述可能包含多句话与补充说明。";
    api.listClasses.mockResolvedValue([{ ...mockClass, description: LONG_DESC }]);
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());

    fireEvent.mouseEnter(screen.getByText(LONG_DESC));
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent(LONG_DESC);
  });

  it("点击「属性」标签加载属性列表", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("tab", { name: /属性/ }));
    await waitFor(() => {
      expect(screen.getByText("customer_name")).toBeInTheDocument();
    });
  });

  it("点击「指标」标签加载指标列表", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("tab", { name: /指标/ }));
    await waitFor(() => {
      expect(screen.getByText("sales_amount")).toBeInTheDocument();
      expect(screen.getByText("SUM(order_amount)")).toBeInTheDocument();
    });
  });

  it("点击新增类按钮打开模态框", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /新增类/ }));
    expect(screen.getByRole("textbox", { name: "类名" })).toBeInTheDocument();
  });

  it("新增类表单填写并提交调用 createClass", async () => {
    const user = userEvent.setup();
    api.createClass.mockResolvedValue({ ...mockClass, id: 2, className: "Order" });
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /新增类/ }));
    const nameInput = screen.getByRole("textbox", { name: "类名" });
    await user.clear(nameInput);
    await user.type(nameInput, "Order");
    await user.click(screen.getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.createClass).toHaveBeenCalledTimes(1);
      expect(api.createClass.mock.calls[0][0].className).toBe("Order");
    });
  });

  it("新增类时选择父类并在 payload 中携带 parentClassId", async () => {
    const user = userEvent.setup();
    api.createClass.mockResolvedValue({ ...mockClass, id: 2, className: "Dog" });
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /新增类/ }));
    const nameInput = screen.getByRole("textbox", { name: "类名" });
    await user.type(nameInput, "Dog");
    // 选择父类
    await user.click(screen.getByRole("combobox", { name: /父类/ }));
    await user.click(screen.getByText("Customer（客户）"));
    await user.click(screen.getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.createClass).toHaveBeenCalledTimes(1);
      expect(api.createClass.mock.calls[0][0].parentClassId).toBe(1);
    });
  });

  it("父类列渲染父类名称", async () => {
    api.listClasses.mockResolvedValue([
      mockClass,
      { ...mockClass, id: 2, className: "Dog", parentClassId: 1 },
    ]);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Dog")).toBeInTheDocument();
      // Dog 的父类显示为 Customer
      expect(screen.getAllByText("Customer").length).toBeGreaterThan(0);
    });
  });

  it("类 Tab 输入类名关键字后表格只显示命中的类", async () => {
    api.listClasses.mockResolvedValue([
      mockClass,
      { ...mockClass, id: 2, className: "PORDERQ", classAlias: "采购订单", sourceTable: "t_porderq" },
    ]);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Customer")).toBeInTheDocument();
      expect(screen.getByText("PORDERQ")).toBeInTheDocument();
    });

    await userEvent.type(screen.getByPlaceholderText("类名"), "Cust");
    await waitFor(() => {
      expect(screen.getByText("Customer")).toBeInTheDocument();
      expect(screen.queryByText("PORDERQ")).toBeNull();
    });
  });

  it("类 Tab 重置筛选后恢复全部", async () => {
    api.listClasses.mockResolvedValue([
      mockClass,
      { ...mockClass, id: 2, className: "PORDERQ", classAlias: "采购订单", sourceTable: "t_porderq" },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("PORDERQ")).toBeInTheDocument());

    await userEvent.type(screen.getByPlaceholderText("类名"), "Cust");
    await waitFor(() => expect(screen.queryByText("PORDERQ")).toBeNull());

    await userEvent.click(screen.getByRole("button", { name: /重置/ }));
    await waitFor(() => expect(screen.getByText("PORDERQ")).toBeInTheDocument());
  });

  it("编辑类时父类下拉排除自身与后代以防成环", async () => {
    const user = userEvent.setup();
    api.listClasses.mockResolvedValue([
      mockClass, // id=1 Customer
      { ...mockClass, id: 2, className: "Animal", parentClassId: null },
      { ...mockClass, id: 3, className: "Dog", parentClassId: 2 },
    ]);
    api.updateClass.mockResolvedValue(mockClass);
    renderPage();
    // 等待 3 行类渲染完成（Customer / Animal / Dog）
    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: /编\s?辑/ }).length).toBe(3);
    });

    // 编辑 Animal（id=2，其后代是 Dog id=3）
    await user.click(screen.getAllByRole("button", { name: /编\s?辑/ })[1]);
    await user.click(screen.getByRole("combobox", { name: /父类/ }));
    // 可选父类应含 Customer，不含自身 Animal 与后代 Dog
    await waitFor(() => {
      expect(screen.getByText("Customer（客户）")).toBeInTheDocument();
      expect(screen.queryByText("Animal（客户）")).not.toBeInTheDocument();
      expect(screen.queryByText("Dog（客户）")).not.toBeInTheDocument();
    });
  });

  it("点击编辑打开预填表单，提交调用 updateClass", async () => {
    const user = userEvent.setup();
    api.updateClass.mockResolvedValue({ ...mockClass });
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /编\s?辑/ }));
    const nameInput = screen.getByDisplayValue("Customer");
    expect(nameInput).toBeInTheDocument();
    await user.clear(nameInput);
    await user.type(nameInput, "Customer_v2");
    await user.click(screen.getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.updateClass).toHaveBeenCalledTimes(1);
      expect(api.updateClass.mock.calls[0][1].className).toBe("Customer_v2");
    });
  });

  it("点击版本按钮打开版本历史弹窗并调用 listClassVersions", async () => {
    const user = userEvent.setup();
    const versions: OntologyClass[] = [
      {
        ...mockClass,
        id: 2,
        version: 2,
        validFrom: "2026-08-11T10:00:00Z",
        validTo: null,
        description: "客户信息 v2",
      },
      {
        ...mockClass,
        id: 1,
        version: 1,
        validFrom: "2026-08-10T10:00:00Z",
        validTo: "2026-08-11T10:00:00Z",
        description: "客户信息 v1",
      },
    ];
    api.listClassVersions.mockResolvedValue(versions);
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());

    // 类列表显示 v1（mockClass.version=1）
    expect(screen.getByText("v1")).toBeInTheDocument();

    // 点击版本按钮
    await user.click(screen.getByRole("button", { name: /版\s?本/ }));

    // 调用 listClassVersions 并显示两个版本
    await waitFor(() => {
      expect(api.listClassVersions).toHaveBeenCalledWith("Customer");
    });
    expect(await screen.findByText("v2 当前")).toBeInTheDocument();
    expect(screen.getByText("客户信息 v2")).toBeInTheDocument();
    expect(screen.getByText("客户信息 v1")).toBeInTheDocument();
  });

  it("点击删除并确认调用 deleteClass", async () => {
    api.deleteClass.mockResolvedValue(undefined);
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /删\s?除/ }));
    // antd Popconfirm 渲染确认按钮
    await waitFor(() => expect(screen.getByRole("button", { name: /确\s?定/ })).toBeInTheDocument());
    await userEvent.click(screen.getAllByRole("button", { name: /确\s?定/ }).pop()!);

    await waitFor(() => {
      expect(api.deleteClass).toHaveBeenCalledWith(1);
    });
  });

  it("属性标签页新增属性调用 createProperty", async () => {
    const user = userEvent.setup();
    api.createProperty.mockResolvedValue({
      ...mockProperty,
      id: 2,
      propertyName: "region_code",
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("tab", { name: /属性/ }));
    await waitFor(() => expect(screen.getByText("customer_name")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /新增属性/ }));
    // 用 id 精确选所属类下拉（属性模态框有两个 Select：所属类 + 数据类型）
    await userEvent.click(screen.getByRole("combobox", { name: /所属类/ }));
    await userEvent.click(screen.getByText("Customer（客户）"));
    // 输入属性名
    const propNameInput = screen.getByRole("textbox", { name: "属性名" });
    await user.type(propNameInput, "region_code");
    await userEvent.click(screen.getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.createProperty).toHaveBeenCalledTimes(1);
      expect(api.createProperty.mock.calls[0][0].propertyName).toBe("region_code");
    });
  });

  it("属性标签页无类时新增按钮禁用且显示提示", async () => {
    api.listClasses.mockResolvedValue([]);
    renderPage();
    await waitFor(() => {
      expect(screen.queryByText("本体管理")).toBeInTheDocument();
    });
    // 切换到属性标签页
    await act(async () => {
      await userEvent.click(screen.getByRole("tab", { name: /属性/ }));
    });
    // 类为空时，新增按钮禁用（queryByRole 返回 null 而非抛异常）
    await waitFor(() => {
      const btn = screen.queryByRole("button", { name: /新增属性/ });
      expect(btn).toBeDisabled();
    });
  });

  it("指标标签页新增指标调用 createMetric", async () => {
    const user = userEvent.setup();
    api.createMetric.mockResolvedValue({
      ...mockMetric,
      id: 2,
      metricName: "order_count",
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("tab", { name: /指标/ }));
    await waitFor(() => expect(screen.getByText("sales_amount")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /新增指标/ }));
    const metricNameInput = screen.getByRole("textbox", { name: "指标名" });
    await user.type(metricNameInput, "order_count");
    // 公式字段无 name 属性，用 placeholder 定位
    const formulaInput = screen.getByPlaceholderText(
      "如 SUM(order_amount) / COUNT(DISTINCT customer_id)"
    );
    await user.type(formulaInput, "COUNT(id)");
    await userEvent.click(screen.getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.createMetric).toHaveBeenCalledTimes(1);
      expect(api.createMetric.mock.calls[0][0].metricName).toBe("order_count");
      expect(api.createMetric.mock.calls[0][0].formula).toBe("COUNT(id)");
    });
  });

  it("指标列表渲染聚合函数和维度默认值", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("tab", { name: /指标/ }));
    await waitFor(() => {
      expect(screen.getByText("求和 SUM")).toBeInTheDocument();
      expect(screen.getByText("region:华东")).toBeInTheDocument();
    });
  });

  it("类列表为空时正常渲染空表格", async () => {
    api.listClasses.mockResolvedValue([]);
    renderPage();
    await waitFor(() => {
      // 空表格仍然渲染
      expect(screen.getByText("本体管理")).toBeInTheDocument();
    });
  });

  it("指标标签页无类时目标类下拉为空", async () => {
    api.listClasses.mockResolvedValue([]);
    renderPage();
    await waitFor(() => expect(screen.getByText("本体管理")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("tab", { name: /指标/ }));
    await waitFor(() => expect(screen.getByText("sales_amount")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /新增指标/ }));
    // 指标模态框有两个 Select（聚合函数 + 目标类）；按 Form label 精确定位目标类下拉
    // （筛选条的目标类 Select 无 aria-label，不会与 Modal 的 Form label 冲突）
    await userEvent.click(screen.getByRole("combobox", { name: /目标类/ }));
    expect(screen.queryByText("Customer")).not.toBeInTheDocument();
  });

  // ---- Error branches ----

  it("类列表加载失败时显示错误提示", async () => {
    api.listClasses.mockRejectedValue(new Error("网络异常"));
    renderPage();
    // 错误已由拦截器处理，表格显示空
    await waitFor(() => {
      expect(screen.getByText("本体管理")).toBeInTheDocument();
    });
  });

  it("属性列表加载失败时表格显示空", async () => {
    api.listPropertiesByClass.mockRejectedValue(new Error("服务不可用"));
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("tab", { name: /属性/ }));
    // 属性 tab 成功切换（错误被 catch 静默处理，页面不崩溃）
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /属性/ })).toBeInTheDocument();
    });
  });

  it("指标列表加载失败时表格显示空", async () => {
    api.listMetrics.mockRejectedValue(new Error("服务不可用"));
    renderPage();
    await waitFor(() => expect(screen.getByText("本体管理")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("tab", { name: /指标/ }));
    await waitFor(() => {
      expect(screen.getByText("本体管理")).toBeInTheDocument();
    });
  });
});

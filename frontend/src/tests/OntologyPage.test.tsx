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
  OntologySemanticRelation,
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
  objectType: null,
  objectOwner: null,
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
  listSemanticRelations: vi.fn(),
  createSemanticRelation: vi.fn(),
  deleteSemanticRelation: vi.fn(),
  backfillRelations: vi.fn(),
  runOntologyBatch: vi.fn(),
  previewOntologyBatch: vi.fn(),
  downloadBatchTemplate: vi.fn(),
  parseBatchCsv: vi.fn(),
  searchOntology: vi.fn(),
  syncClassEmbedding: vi.fn(),
  syncMissingEmbeddings: vi.fn(),
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

  it("类行「同步向量」按钮点击后调用 syncClassEmbedding", async () => {
    const user = userEvent.setup();
    api.syncClassEmbedding.mockResolvedValue(undefined);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Customer")).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /同步向量/ }));
    await waitFor(() => {
      expect(api.syncClassEmbedding).toHaveBeenCalledWith(1);
    });
  });

  it("头部「补同步缺失向量」按钮点击后调用 syncMissingEmbeddings", async () => {
    const user = userEvent.setup();
    api.syncMissingEmbeddings.mockResolvedValue({
      totalClasses: 96,
      missingCount: 69,
      syncedCount: 69,
      failedCount: 0,
      failures: [],
    });
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Customer")).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /补同步缺失向量/ }));
    await waitFor(() => {
      expect(api.syncMissingEmbeddings).toHaveBeenCalledTimes(1);
    });
  });

  it("头部「批量关系」按钮打开批量关系引擎弹窗", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: /批量关系/ }));
    // 弹窗标题 + 三个动作 + 冲突策略 + 预览/执行
    expect(await screen.findByText("批量关系引擎")).toBeInTheDocument();
    expect(screen.getByText("本体入图")).toBeInTheDocument();
    expect(screen.getByText("按共享列推断物理关联")).toBeInTheDocument();
    expect(screen.getByText("应用关系清单")).toBeInTheDocument();
    expect(screen.getByText("跳过（保留原样）")).toBeInTheDocument();
    expect(screen.getByText("覆盖（更新差异字段）")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /预\s*览/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /执\s*行/ })).toBeDisabled();
    // 关闭弹窗后内容卸载
    await userEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
    await waitFor(() => expect(screen.queryByText("批量关系引擎")).not.toBeInTheDocument());
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

  it("点击「语义关系」标签加载语义关系列表", async () => {
    const mockRelation: OntologySemanticRelation = {
      id: 1,
      sourceClassId: 1,
      targetClassId: 2,
      relationType: "SUPPLIES",
      description: "供应商供货",
      createdBy: null,
      createdTime: "2026-08-11T00:00:00Z",
      updatedTime: "2026-08-11T00:00:00Z",
    };
    api.listSemanticRelations.mockResolvedValue([mockRelation]);
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("tab", { name: /语义\s?关系/ }));
    await waitFor(() => {
      expect(api.listSemanticRelations).toHaveBeenCalled();
      expect(screen.getByText("供货 SUPPLIES")).toBeInTheDocument();
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

  it("类列表渲染对象类型 Tag 与责任部门（治理字段）", async () => {
    api.listClasses.mockResolvedValue([
      { ...mockClass, objectType: "Transaction", objectOwner: "采购部" },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());
    expect(screen.getByText("交易单据 Transaction")).toBeInTheDocument();
    expect(screen.getByText("采购部")).toBeInTheDocument();
  });

  it("新增类时携带对象类型与责任部门治理字段", async () => {
    const user = userEvent.setup();
    api.createClass.mockResolvedValue({ ...mockClass, id: 2, className: "Order" });
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /新增类/ }));
    const nameInput = screen.getByRole("textbox", { name: "类名" });
    await user.type(nameInput, "Order");
    // 选择对象类型 Transaction
    await user.click(screen.getByRole("combobox", { name: /对象类型/ }));
    await user.click(screen.getByText("交易单据 Transaction"));
    // 填写责任部门
    const ownerInput = screen.getByRole("textbox", { name: /责任部门/ });
    await user.type(ownerInput, "采购部");
    await user.click(screen.getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.createClass).toHaveBeenCalledTimes(1);
      const payload = api.createClass.mock.calls[0][0];
      expect(payload.objectType).toBe("Transaction");
      expect(payload.objectOwner).toBe("采购部");
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
    // 预填在 afterOpenChange（弹窗动画结束）时写入 —— 异步，需 waitFor
    const nameInput = await screen.findByDisplayValue("Customer");
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

  // ---- 语义搜索（searchOntology） ----

  it("语义搜索：空 query 不调用 searchOntology", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());
    // antd Input.Search 的 enterButton 是 span，直接 click 触发空字符串搜索
    await userEvent.click(screen.getByRole("button", { name: /搜\s?索/ }));
    expect(api.searchOntology).not.toHaveBeenCalled();
  });

  it("语义搜索：成功时弹窗展示结果表格（含类型/别名/描述 Tooltip/评分）", async () => {
    api.searchOntology.mockResolvedValue([
      {
        type: "class",
        id: 1,
        name: "Customer",
        alias: "客户",
        description: "客户实体描述",
        score: 0.85,
      },
      {
        type: "property",
        id: 2,
        name: "customer_name",
        alias: null,
        description: null,
        score: 0.42,
      },
      {
        type: "metric",
        id: 3,
        name: "sales_amount",
        alias: "销售额",
        description: "销售总额",
        score: 0.7,
      },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());

    const searchInput = screen.getByPlaceholderText("语义搜索：如 客户销售额");
    await userEvent.type(searchInput, "客户");
    await userEvent.click(screen.getByRole("button", { name: /搜\s?索/ }));

    await waitFor(() => {
      expect(api.searchOntology).toHaveBeenCalledWith("客户", { topK: 15 });
    });
    // 弹窗标题
    expect(screen.getByText("语义检索结果")).toBeInTheDocument();
    // 三类命中均渲染（Tag 内文本 = i18n 实体类型）；限定到 Modal 内避免与 Tab 标签冲突
    const modal = screen.getByRole("dialog");
    expect(modal).toHaveTextContent("类");
    expect(modal).toHaveTextContent("属性");
    expect(modal).toHaveTextContent("指标");
    // 评分百分比 (0.85 -> 85%)
    expect(screen.getByText("85%")).toBeInTheDocument();
    expect(screen.getByText("42%")).toBeInTheDocument();
    // alias=null 时回退到 dash
    expect(screen.getAllByText("-").length).toBeGreaterThan(0);
    // 描述字段渲染 Tooltip（fireEvent 触发）
    fireEvent.mouseEnter(screen.getByText("客户实体描述"));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("客户实体描述");
  });

  it("语义搜索：失败时不弹窗且不抛错", async () => {
    api.searchOntology.mockRejectedValue(new Error("向量服务不可用"));
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());

    const searchInput = screen.getByPlaceholderText("语义搜索：如 客户销售额");
    await userEvent.type(searchInput, "客户");
    await userEvent.click(screen.getByRole("button", { name: /搜\s?索/ }));

    await waitFor(() => {
      expect(api.searchOntology).toHaveBeenCalledWith("客户", { topK: 15 });
    });
    // 错误被 catch 静默，弹窗不打开
    expect(screen.queryByText("语义检索结果")).not.toBeInTheDocument();
  });

  it("语义搜索：无结果时弹窗显示空状态", async () => {
    api.searchOntology.mockResolvedValue([]);
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());

    const searchInput = screen.getByPlaceholderText("语义搜索：如 客户销售额");
    await userEvent.type(searchInput, "空查询");
    await userEvent.click(screen.getByRole("button", { name: /搜\s?索/ }));

    await waitFor(() => {
      expect(api.searchOntology).toHaveBeenCalledWith("空查询", { topK: 15 });
    });
    expect(await screen.findByText("未匹配到相关本体。请确认已通过编辑表单或 /embeddings/sync 写入向量。")).toBeInTheDocument();
  });

  it("语义搜索弹窗关闭按钮可关闭弹窗", async () => {
    api.searchOntology.mockResolvedValue([
      { type: "class", id: 1, name: "Customer", alias: null, description: null, score: 0.5 },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("Customer")).toBeInTheDocument());

    const searchInput = screen.getByPlaceholderText("语义搜索：如 客户销售额");
    await userEvent.type(searchInput, "客户");
    await userEvent.click(screen.getByRole("button", { name: /搜\s?索/ }));

    await waitFor(() => expect(screen.getByText("语义检索结果")).toBeInTheDocument());

    // 点击关闭按钮（右上角 X）
    await userEvent.click(screen.getByRole("button", { name: /Close/ }));
    await waitFor(() => {
      expect(screen.queryByText("语义检索结果")).not.toBeInTheDocument();
    });
  });
});

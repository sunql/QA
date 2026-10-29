import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider, message } from "antd";
import zhCN from "antd/locale/zh_CN";
import KpiCatalogPage from "../pages/KpiCatalogPage";
import type { KpiCatalog, KpiDatasourceSuggestion } from "../types/kpiCatalog";
import type { OntologyMetric } from "../types/ontology";
import {
  buildKpiCreatePayload,
  buildKpiUpdatePayload,
  emptyKpiForm,
} from "../utils/kpiCatalogPayload";

const { mockKpi } = vi.hoisted(() => ({
  mockKpi: {
    id: 1,
    kpiCode: "KPI_SUPPLIER_OTD",
    kpiName: "供应商准时交付率",
    businessDefinition: "按采购订单行项按时签收比例",
    formula: "COUNT(RECEIVED_QTY<=ORDER_QTY)/COUNT(*)",
    numerator: "按时签收订单数",
    denominator: "总订单数",
    grain: "供应商+月",
    unit: "%",
    dataSource: "DWS_SUPPLIER_MONTHLY",
    datasourceId: 7,
    owner: "采购部",
    version: "v1.0",
    revisionCount: 0,
    status: "PUBLISHED",
    metricId: null,
    semanticKeywords: ["准时交付"],
    // 刻意取**非默认值**：若 openEdit 漏回填 matchThreshold，表单会退回 0.75 ⇒ 断言才有判别力
    matchThreshold: 0.6,
    grainKeys: ["supplier_code"],
    numeratorColumns: ["QTY"],
    denominatorColumns: ["AMOUNT"],
    createdBy: null,
    createdTime: "2026-08-30T00:00:00Z",
    updatedTime: "2026-08-30T00:00:00Z",
  } as KpiCatalog,
}));

const api = vi.hoisted(() => ({
  listKpis: vi.fn(),
  createKpi: vi.fn(),
  updateKpi: vi.fn(),
  deleteKpi: vi.fn(),
  getDatasourceSuggestions: vi.fn(),
}));

vi.mock("../api/kpiCatalog", () => api);

const datasourceApi = vi.hoisted(() => ({
  listDataSources: vi.fn(),
}));

vi.mock("../api/datasource", () => datasourceApi);

const ontologyApi = vi.hoisted(() => ({
  listMetrics: vi.fn(),
}));

vi.mock("../api/ontology", () => ontologyApi);

/** 本体指标工厂：避免同一份 19 字段字面量在多处重复。 */
const makeMetric = (over: Partial<OntologyMetric> = {}): OntologyMetric =>
  ({
    id: 42,
    metricName: "供应商准时率",
    metricAlias: "supplier_otd",
    formula: "SELECT 1 FROM DUAL",
    aggFunction: "SUM",
    targetClassId: null,
    dimensionDefaults: null,
    createdBy: null,
    createdTime: null,
    updatedTime: null,
    metricType: "RATIO",
    numeratorMeasure: null,
    denominatorMeasure: null,
    aggregationOrder: "WEIGHTED_RATIO",
    grainKeys: ["supplier_code"],
    filterExpression: null,
    status: "PUBLISHED",
    owner: null,
    executionSource: "COMPILED",
    ...over,
  }) as OntologyMetric;

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/kpi-catalog"]}>
        <Routes>
          <Route path="/kpi-catalog" element={<KpiCatalogPage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("KpiCatalogPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listKpis.mockResolvedValue([mockKpi]);
    datasourceApi.listDataSources.mockResolvedValue([
      { id: 7, name: "DWS_TEST", isActive: true },
    ]);
    api.getDatasourceSuggestions.mockResolvedValue([]);
    // 页面挂载即拉取本体指标列表；既有 18 条用例不关心指标内容
    ontologyApi.listMetrics.mockResolvedValue([]);
  });

  it("渲染标题与表格，并加载 KPI 列表", async () => {
    renderPage();
    // antd 按钮 a11y name = "plus 新 建 KPI"（图标+断字）
    expect(
      screen.getByRole("button", { name: /plus.*新.*建.*KPI/ }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument();
      expect(screen.getByText("供应商准时交付率")).toBeInTheDocument();
      // 状态 Tag 渲染的是翻译后的中文（enums.kpiStatus.PUBLISHED = 已发布）
      expect(screen.getByText("已发布")).toBeInTheDocument();
    });
  });

  it("渲染操作列按钮", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/编\s?辑/)).toBeInTheDocument();
      expect(screen.getByText(/删\s?除/)).toBeInTheDocument();
    });
  });

  it("点击新建并提交调用 createKpi（camelCase payload）", async () => {
    const user = userEvent.setup();
    api.createKpi.mockResolvedValue({ ...mockKpi, id: 2 });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: /plus.*新.*建.*KPI/ }));

    const codeInput = screen.getByPlaceholderText(/KPI_/);
    // Modal 的 name 输入框 placeholder = "如：供应商准时交付率"，FilterBar 用 label 做 placeholder 是 "KPI 名称"
    const nameInput = screen.getByPlaceholderText(/供应商准时交付率/);
    await user.type(codeInput, "KPI_TEST_NEW");
    await user.type(nameInput, "新 KPI");

    // F2：创建 KPI 必须绑定数据源（后端 datasourceId 必填）
    await user.click(screen.getByRole("combobox", { name: /绑定数据源/ }));
    await user.click(await screen.findByTitle("DWS_TEST"));

    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.createKpi).toHaveBeenCalled();
      const payload = api.createKpi.mock.calls[0][0];
      expect(payload.kpiCode).toBe("KPI_TEST_NEW");
      expect(payload.kpiName).toBe("新 KPI");
      expect(payload.datasourceId).toBe(7);
    });
  });

  it("列表渲染状态 Tag 颜色：DRAFT/PUBLISHED/DEPRECATED", async () => {
    api.listKpis.mockResolvedValue([
      { ...mockKpi, id: 1, kpiCode: "K1", status: "DRAFT" },
      { ...mockKpi, id: 2, kpiCode: "K2", status: "PUBLISHED" },
      { ...mockKpi, id: 3, kpiCode: "K3", status: "DEPRECATED" },
    ]);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("K1")).toBeInTheDocument();
      expect(screen.getByText("K2")).toBeInTheDocument();
      expect(screen.getByText("K3")).toBeInTheDocument();
    });
  });

  // ---- Edit / Delete / Refresh / Filter ----

  it("点击编辑打开预填表单并提交调用 updateKpi（payload 字段）", async () => {
    const user = userEvent.setup();
    api.updateKpi.mockResolvedValue({ ...mockKpi, kpiName: "供应商准时交付率 v2" });
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    // 编辑模式下 Modal title 是"编辑 KPI"
    expect(await screen.findByText(/编\s?辑.*KPI/)).toBeInTheDocument();

    // 修改 KPI 名称后提交
    const nameInput = screen.getByDisplayValue("供应商准时交付率");
    await user.clear(nameInput);
    await user.type(nameInput, "供应商准时交付率 v2");

    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.updateKpi).toHaveBeenCalledTimes(1);
      const [id, payload] = api.updateKpi.mock.calls[0];
      expect(id).toBe(1);
      expect(payload.kpiCode).toBe("KPI_SUPPLIER_OTD");
      expect(payload.kpiName).toBe("供应商准时交付率 v2");
      expect(payload.status).toBe("PUBLISHED");
      // 编辑表单预填 record.datasourceId，提交时原样带回
      expect(payload.datasourceId).toBe(7);
    });
  });

  it("编辑时展示数据源建议，点击采用后提交建议值", async () => {
    const user = userEvent.setup();
    api.getDatasourceSuggestions.mockResolvedValue([
      {
        datasourceId: 9,
        name: "DWS_SUGGESTED",
        score: 0.9,
        reasons: ["legacy_name_exact"],
      },
    ]);
    datasourceApi.listDataSources.mockResolvedValue([
      { id: 7, name: "DWS_TEST", isActive: true },
      { id: 9, name: "DWS_SUGGESTED", isActive: true },
    ]);
    api.updateKpi.mockResolvedValue(mockKpi);
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));

    expect(
      await screen.findByText(/建议绑定数据源：DWS_SUGGESTED/),
    ).toBeInTheDocument();
    expect(screen.getByText(/完全一致/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /采\s?用/ }));
    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.updateKpi).toHaveBeenCalledTimes(1);
      const [, payload] = api.updateKpi.mock.calls[0];
      expect(payload.datasourceId).toBe(9);
    });
  });

  it("建议与当前绑定相同时不提示", async () => {
    const user = userEvent.setup();
    api.getDatasourceSuggestions.mockResolvedValue([
      {
        datasourceId: 7,
        name: "DWS_TEST",
        score: 0.9,
        reasons: ["legacy_name_exact"],
      },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));

    await waitFor(() =>
      expect(api.getDatasourceSuggestions).toHaveBeenCalledWith(1),
    );
    expect(screen.queryByText(/建议绑定数据源/)).not.toBeInTheDocument();
  });

  it("切换编辑目标时在途建议不会串到另一个 KPI", async () => {
    const user = userEvent.setup();
    api.listKpis.mockResolvedValue([
      { ...mockKpi, id: 1, kpiCode: "KPI_A" },
      { ...mockKpi, id: 2, kpiCode: "KPI_B", datasourceId: null },
    ]);
    let resolveA: (rows: KpiDatasourceSuggestion[]) => void = () => {};
    api.getDatasourceSuggestions.mockImplementation((id: number) => {
      if (id === 1) {
        return new Promise<KpiDatasourceSuggestion[]>((resolve) => {
          resolveA = resolve;
        });
      }
      return Promise.resolve([
        {
          datasourceId: 9,
          name: "B_SUGGESTED",
          score: 0.9,
          reasons: ["legacy_name_exact" as const],
        },
      ]);
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_A")).toBeInTheDocument());

    const editButtons = screen.getAllByRole("button", { name: /编\s?辑/ });
    await user.click(editButtons[0]); // A：请求挂起
    await user.click(editButtons[1]); // B：立即返回
    expect(
      await screen.findByText(/建议绑定数据源：B_SUGGESTED/),
    ).toBeInTheDocument();

    // A 的迟到的响应必须被丢弃
    resolveA([
      {
        datasourceId: 8,
        name: "A_SUGGESTED",
        score: 0.9,
        reasons: ["legacy_name_exact"],
      },
    ]);
    await waitFor(() =>
      expect(screen.queryByText(/A_SUGGESTED/)).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/建议绑定数据源：B_SUGGESTED/)).toBeInTheDocument();
  });

  it("点击删除并确认调用 deleteKpi", async () => {
    const user = userEvent.setup();
    const successSpy = vi.spyOn(message, "success").mockReturnValue(1 as unknown as ReturnType<typeof message.success>);
    api.deleteKpi.mockResolvedValue(undefined);
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /删\s?除/ }));
    // Popconfirm 确认按钮
    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.deleteKpi).toHaveBeenCalledWith(1);
      expect(successSpy).toHaveBeenCalled();
    });
    successSpy.mockRestore();
  });

  it("删除失败时静默 catch 不抛错", async () => {
    const user = userEvent.setup();
    api.deleteKpi.mockRejectedValue(new Error("删除失败"));
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /删\s?除/ }));
    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.deleteKpi).toHaveBeenCalledWith(1);
    });
  });

  it("刷新按钮重新调用 listKpis", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());
    expect(api.listKpis).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /刷\s?新/ }));

    await waitFor(() => {
      expect(api.listKpis).toHaveBeenCalledTimes(2);
    });
  });

  it("listKpis 失败时表格为空（catch 分支）", async () => {
    api.listKpis.mockRejectedValue(new Error("服务不可用"));
    renderPage();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /plus.*新.*建.*KPI/ })).toBeInTheDocument();
    });
    // KPI 不在表格中
    expect(screen.queryByText("KPI_SUPPLIER_OTD")).not.toBeInTheDocument();
  });

  it("筛选：输入 KPI 编码关键字后表格只显示命中的行", async () => {
    api.listKpis.mockResolvedValue([
      mockKpi,
      { ...mockKpi, id: 2, kpiCode: "KPI_OTHER" },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    const codeInput = screen.getByPlaceholderText("KPI 编码");
    await userEvent.type(codeInput, "OTD");

    await waitFor(() => {
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument();
      expect(screen.queryByText("KPI_OTHER")).not.toBeInTheDocument();
    });
  });

  it("重置筛选后恢复全部 KPI", async () => {
    api.listKpis.mockResolvedValue([
      mockKpi,
      { ...mockKpi, id: 2, kpiCode: "KPI_OTHER" },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_OTHER")).toBeInTheDocument());

    const codeInput = screen.getByPlaceholderText("KPI 编码");
    await userEvent.type(codeInput, "OTD");
    await waitFor(() => expect(screen.queryByText("KPI_OTHER")).not.toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /重\s?置/ }));
    await waitFor(() => expect(screen.getByText("KPI_OTHER")).toBeInTheDocument());
  });

  it("未绑定数据源的 KPI 在列表显示「未绑定」标签（反向：有绑定则显示数据源名）", async () => {
    api.listKpis.mockResolvedValue([
      { ...mockKpi, id: 1, kpiCode: "K_BOUND", datasourceId: 7 },
      { ...mockKpi, id: 2, kpiCode: "K_UNBOUND", datasourceId: null },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("K_BOUND")).toBeInTheDocument());

    // 有绑定 → 按 id 映射名称
    expect(screen.getByText("DWS_TEST")).toBeInTheDocument();
    // 未绑定 → Tag
    expect(screen.getByText("未绑定")).toBeInTheDocument();
  });

  it("业务定义长文本渲染 Tooltip", async () => {
    const LONG = "这是一段非常长的业务定义描述文本，用于验证超出列宽时悬停可查看完整内容。";
    api.listKpis.mockResolvedValue([{ ...mockKpi, businessDefinition: LONG }]);
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    fireEvent.mouseEnter(screen.getByText(LONG));
    expect(await screen.findByRole("tooltip")).toHaveTextContent(LONG);
  });

  it("版本列：当 revisionCount>0 时附加 (+N)", async () => {
    api.listKpis.mockResolvedValue([
      { ...mockKpi, version: "v1.0", revisionCount: 3 },
    ]);
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());
    expect(screen.getByText(/v1\.0 \(\+3\)/)).toBeInTheDocument();
  });

  it("createKpi 提交失败时 catch 静默", async () => {
    const user = userEvent.setup();
    api.createKpi.mockRejectedValue(new Error("创建失败"));
    renderPage();
    await waitFor(() => expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /plus.*新.*建.*KPI/ }));
    const codeInput = screen.getByPlaceholderText(/KPI_/);
    const nameInput = screen.getByPlaceholderText(/供应商准时交付率/);
    await user.type(codeInput, "KPI_FAIL");
    await user.type(nameInput, "失败 KPI");
    await user.click(screen.getByRole("combobox", { name: /绑定数据源/ }));
    await user.click(await screen.findByTitle("DWS_TEST"));
    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.createKpi).toHaveBeenCalled();
    });
  });
});

describe("KpiCatalogPage — 结构化口径与技术形态挂靠", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listKpis.mockResolvedValue([{ ...mockKpi, metricId: 42 }]);
    datasourceApi.listDataSources.mockResolvedValue([
      { id: 7, name: "DWS_TEST", isActive: true },
    ]);
    api.getDatasourceSuggestions.mockResolvedValue([]);
    ontologyApi.listMetrics.mockResolvedValue([makeMetric()]);
  });

  it("列表显示挂靠的指标名与指标状态（定位法：行内找 Tag）", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("供应商准时率")).toBeInTheDocument();
    });
    // enums.metricStatus.PUBLISHED 与 enums.kpiStatus.PUBLISHED 中文都是「已发布」
    // ⇒ 只能靠位置辨认：找到指标名所在单元格，在单元格内断言。
    const cell = screen.getByText("供应商准时率").closest("td");
    expect(cell).not.toBeNull();
    expect(cell).toHaveTextContent("已发布");
  });

  it("未挂靠指标的 KPI 显示「未挂靠指标」标签", async () => {
    api.listKpis.mockResolvedValue([{ ...mockKpi, metricId: null }]);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("未挂靠指标")).toBeInTheDocument();
    });
  });

  it("编辑回填并提交时带上 semanticKeywords / grainKeys / metricId / matchThreshold", async () => {
    const user = userEvent.setup();
    api.updateKpi.mockResolvedValue({ ...mockKpi });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    await user.click(await screen.findByRole("button", { name: /确\s*定/ }));

    await waitFor(() => expect(api.updateKpi).toHaveBeenCalled());
    const payload = api.updateKpi.mock.calls[0][1];
    expect(payload.semanticKeywords).toEqual(["准时交付"]);
    expect(payload.grainKeys).toEqual(["supplier_code"]);
    expect(payload.numeratorColumns).toEqual(["QTY"]);
    expect(payload.denominatorColumns).toEqual(["AMOUNT"]);
    expect(payload.metricId).toBe(42);
    // 0.6 ≠ DEFAULT_MATCH_THRESHOLD(0.75) ⇒ 证明回填路径真的生效，而非默认值兜底
    expect(payload.matchThreshold).toBe(0.6);
  });

  it("语义关键词 tags Select 可输入新标签并进入 payload", async () => {
    const user = userEvent.setup();
    api.updateKpi.mockResolvedValue({ ...mockKpi });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    // antd Select 的 placeholder 渲染成 span（不是 input 的 placeholder 属性），
    // 且已有值时不显示 ⇒ 按 Form.Item label 定位到内部的搜索输入框
    const kwItem = (await screen.findByText("语义关键词")).closest(
      ".ant-form-item",
    );
    expect(kwItem).not.toBeNull();
    const kwInput = kwItem!.querySelector("input");
    expect(kwInput).not.toBeNull();
    await user.click(kwInput!);
    await user.type(kwInput!, "准时率");
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: /确\s*定/ }));

    await waitFor(() => expect(api.updateKpi).toHaveBeenCalled());
    const payload = api.updateKpi.mock.calls[0][1];
    expect(payload.semanticKeywords).toContain("准时率");
    expect(payload.semanticKeywords).toContain("准时交付");
  });

  it("清空「挂靠本体指标」后提交显式 null（解除挂靠）", async () => {
    const user = userEvent.setup();
    api.updateKpi.mockResolvedValue({ ...mockKpi });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    const metricItem = (await screen.findByText("挂靠本体指标")).closest(
      ".ant-form-item",
    );
    expect(metricItem).not.toBeNull();
    const clearIcon = metricItem!.querySelector(".ant-select-clear");
    expect(clearIcon).not.toBeNull();
    fireEvent.mouseDown(clearIcon!);

    await user.click(screen.getByRole("button", { name: /确\s*定/ }));

    await waitFor(() => expect(api.updateKpi).toHaveBeenCalled());
    const payload = api.updateKpi.mock.calls[0][1];
    expect(payload.metricId).toBeNull();
  });

  it("清空「匹配阈值」后提交复位为默认 0.75（不是 null / undefined）", async () => {
    const user = userEvent.setup();
    api.updateKpi.mockResolvedValue({ ...mockKpi });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    const thresholdItem = (await screen.findByText("匹配阈值")).closest(
      ".ant-form-item",
    );
    expect(thresholdItem).not.toBeNull();
    const thresholdInput = thresholdItem!.querySelector("input");
    expect(thresholdInput).not.toBeNull();
    // rc-input-number 清空 ⇒ onChange(null) ⇒ Form 值变 null（校验规则不拦空值）
    await user.clear(thresholdInput!);

    await user.click(screen.getByRole("button", { name: /确\s*定/ }));

    await waitFor(() => expect(api.updateKpi).toHaveBeenCalled());
    const payload = api.updateKpi.mock.calls[0][1];
    // 后端 match_threshold 是 NOT NULL 列 ⇒ 前端必须发显式默认值，发 null 会 500
    expect(payload.matchThreshold).toBe(0.75);
  });

  it("提交的数组字段不持有 record 的同一实例（禁止别名共享）", async () => {
    const user = userEvent.setup();
    api.updateKpi.mockResolvedValue({ ...mockKpi });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("KPI_SUPPLIER_OTD")).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    await user.click(await screen.findByRole("button", { name: /确\s*定/ }));

    await waitFor(() => expect(api.updateKpi).toHaveBeenCalled());
    const payload = api.updateKpi.mock.calls[0][1];
    // 值相等但实例不同：payload 不得与 fixture record 的数组共享同一实例
    expect(payload.semanticKeywords).toEqual(mockKpi.semanticKeywords);
    expect(payload.semanticKeywords).not.toBe(mockKpi.semanticKeywords);
    expect(payload.grainKeys).not.toBe(mockKpi.grainKeys);
    expect(payload.numeratorColumns).not.toBe(mockKpi.numeratorColumns);
    expect(payload.denominatorColumns).not.toBe(mockKpi.denominatorColumns);
  });
});

/**
 * 上一条 UI 用例无法区分「payload 拷贝」与「rc-select 挂载时归一」——
 * 即便 buildKpi*Payload 去掉展开拷贝，表单值仍会被 rc-select 换成新数组 ⇒ 用例照样绿。
 * 故在这里直接对工厂函数做实例身份断言，真正锁住「payload 不持有入参实例」的不变量。
 */
describe("kpiCatalogPayload — 数组字段不与入参共享实例", () => {
  it("buildKpiUpdatePayload / buildKpiCreatePayload 各自拷贝四个数组", () => {
    const values = {
      ...emptyKpiForm(),
      semanticKeywords: ["准时交付"],
      grainKeys: ["supplier_code"],
      numeratorColumns: ["QTY"],
      denominatorColumns: ["AMOUNT"],
    };
    const payloads = [
      buildKpiUpdatePayload(values),
      buildKpiCreatePayload(values),
    ];
    for (const payload of payloads) {
      expect(payload.semanticKeywords).toEqual(values.semanticKeywords);
      expect(payload.semanticKeywords).not.toBe(values.semanticKeywords);
      expect(payload.grainKeys).not.toBe(values.grainKeys);
      expect(payload.numeratorColumns).not.toBe(values.numeratorColumns);
      expect(payload.denominatorColumns).not.toBe(values.denominatorColumns);
    }
  });
});

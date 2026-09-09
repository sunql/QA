import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import ImportWizard from "../components/localImport/ImportWizard";
import * as api from "../api/localImport";
import * as datasourceApi from "../api/datasource";
import type { SchemaIntrospectResponse, TableSchema } from "../types/datasource";
import type { ImportPreviewResponse, ImportRuleConfig } from "../types/localImport";

vi.mock("../api/localImport");
vi.mock("../api/datasource");

function table(name: string, columns: string[]): TableSchema {
  return {
    tableName: name,
    owner: "X3",
    columns: columns.map((columnName) => ({
      columnName,
      dataType: "VARCHAR",
      nullable: true,
    })),
    primaryKeys: columns.slice(0, 1),
    foreignKeys: [],
  };
}

// 向导第一步的 schema 源：表名须覆盖各测试 preview 里出现的 sourceTable。
const SCHEMA_TABLES: TableSchema[] = [
  table("orders", ["id", "customer_id", "supplier_id"]),
  table("customers", ["id"]),
  table("suppliers", ["id"]),
];

function schemaResponse(): SchemaIntrospectResponse {
  return { tables: SCHEMA_TABLES, cachedAt: "2026-01-01T00:00:00Z" };
}

function previewResponse(
  overrides: Partial<ImportPreviewResponse> = {},
): ImportPreviewResponse {
  return {
    datasourceId: 1,
    proposedClasses: [],
    proposedJoins: [],
    conflicts: [],
    filterSuggestions: { recommendedBlacklistPatterns: [], excludedTables: [] },
    llmUsage: { modelName: null, promptTokens: 0, completionTokens: 0 },
    ...overrides,
  };
}

const CLASS_ORDERS = {
  sourceTable: "orders",
  className: "orders",
  classAlias: null,
  description: null,
  isSelected: true,
  properties: [],
};
const CLASS_CUSTOMERS = {
  sourceTable: "customers",
  className: "customers",
  classAlias: null,
  description: null,
  isSelected: true,
  properties: [],
};
const CLASS_SUPPLIERS = {
  sourceTable: "suppliers",
  className: "suppliers",
  classAlias: null,
  description: null,
  isSelected: true,
  properties: [],
};

const JOIN_ORDERS_CUSTOMERS = {
  sourceTable: "orders",
  sourceColumns: ["customer_id"],
  targetTable: "customers",
  targetColumns: ["id"],
  joinType: "INNER",
  relationType: "foreign_key",
  isSelected: true,
  inferredBy: "name_convention",
};
const JOIN_ORDERS_SUPPLIERS = {
  sourceTable: "orders",
  sourceColumns: ["supplier_id"],
  targetTable: "suppliers",
  targetColumns: ["id"],
  joinType: "INNER",
  relationType: "foreign_key",
  isSelected: true,
  inferredBy: "name_convention",
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(datasourceApi.getDatasourceSchema).mockResolvedValue(schemaResponse());
  vi.mocked(datasourceApi.introspectDatasource).mockResolvedValue(schemaResponse());
});

// 第一步：等待 schema 就绪 → 全选表 → 点「下一步」进入预览。返回后即可断言预览 UI。
async function openPreviewStep() {
  render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
  // schema 加载后第一步的表列表出现
  await screen.findByText("orders");
  fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
  fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /确认导入/i })).toBeInTheDocument(),
  );
}

describe("ImportWizard", () => {
  it("renders rule config step by default", async () => {
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    // Steps 标题 + schema 表标题均存在
    expect(screen.getByText(/规则配置/i)).toBeInTheDocument();
    expect(await screen.findByText(/选择要导入的表/i)).toBeInTheDocument();
  });

  it("next 禁用直到选中表；选中后调用 preview API", async () => {
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("orders");
    // 未选表时「下一步」禁用
    expect(screen.getByRole("button", { name: /下一步/i })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    expect(screen.getByRole("button", { name: /下一步/i })).not.toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() =>
      expect(api.getImportPreview).toHaveBeenCalledWith(1, expect.any(Object)),
    );
  });

  it("预览请求携带选表列表与规则（非仅 rules）", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue(previewResponse());
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("orders");
    // 只选 orders 一张表：直接勾其行 checkbox
    const tableEl = screen.getByTestId("ruleTable");
    const ordersRow = within(tableEl).getByText("orders").closest("tr")!;
    fireEvent.click(
      within(ordersRow).getByRole("checkbox"),
    );
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() =>
      expect(api.getImportPreview).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ selectedTables: ["orders"] }),
      ),
    );
  });

  it("shows success result after successful import", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue(
      previewResponse({ proposedClasses: [CLASS_ORDERS] }),
    );
    vi.mocked(api.executeImport).mockResolvedValue({
      success: true,
      createdClasses: 1,
      createdProperties: 2,
      createdJoins: 0,
      skippedConflicts: 0,
      overwrittenConflicts: 0,
      errors: [],
    });

    await openPreviewStep();
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    fireEvent.click(screen.getByRole("button", { name: /确认导入/i }));

    await waitFor(() =>
      expect(document.querySelector(".ant-result-title")).toHaveTextContent("导入完成")
    );
    expect(api.executeImport).toHaveBeenCalled();
    expect(document.querySelector(".ant-result-success")).toBeInTheDocument();
  });

  it("shows error result with error details after failed import", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue(
      previewResponse({ proposedClasses: [CLASS_ORDERS] }),
    );
    vi.mocked(api.executeImport).mockResolvedValue({
      success: false,
      createdClasses: 1,
      createdProperties: 1,
      createdJoins: 0,
      skippedConflicts: 0,
      overwrittenConflicts: 0,
      errors: [
        { type: "property", name: "orders.customer_id", message: "NOT NULL violation" },
      ],
    });

    await openPreviewStep();
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    fireEvent.click(screen.getByRole("button", { name: /确认导入/i }));

    await waitFor(() =>
      expect(screen.getByText("导入未完全成功")).toBeInTheDocument()
    );
    expect(document.querySelector(".ant-result-error")).toBeInTheDocument();
    expect(screen.getByText("property")).toBeInTheDocument();
    expect(screen.getByText("orders.customer_id")).toBeInTheDocument();
    expect(screen.getByText("NOT NULL violation")).toBeInTheDocument();
  });

  it("batch 搜索累计勾选类；join 只提交两端勾选且启用的边；未勾选端点的 join 被禁用", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue(
      previewResponse({
        proposedClasses: [CLASS_ORDERS, CLASS_CUSTOMERS, CLASS_SUPPLIERS],
        proposedJoins: [JOIN_ORDERS_CUSTOMERS, JOIN_ORDERS_SUPPLIERS],
      }),
    );
    vi.mocked(api.executeImport).mockResolvedValue({
      success: true,
      createdClasses: 2,
      createdProperties: 2,
      createdJoins: 1,
      skippedConflicts: 0,
      overwrittenConflicts: 0,
      errors: [],
    });

    await openPreviewStep();

    // 未勾选任何类时，确认按钮禁用
    expect(screen.getByRole("button", { name: /确认导入/i })).toBeDisabled();

    // 批次1：搜索 order → 类表仅 orders 一行
    fireEvent.change(screen.getByPlaceholderText(/搜索表名\/类名/i), {
      target: { value: "order" },
    });
    const classTableEl = screen.getByTestId("classTable");
    expect(classTableEl.querySelectorAll(".ant-table-row")).toHaveLength(1);
    expect(within(classTableEl).queryByText("customers")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));

    // 批次2：搜索 customer → 类表仅 customers 一行；join 表仍在（orders→customers 可勾）
    fireEvent.change(screen.getByPlaceholderText(/搜索表名\/类名/i), {
      target: { value: "customer" },
    });
    expect(classTableEl.querySelectorAll(".ant-table-row")).toHaveLength(1);
    expect(within(classTableEl).queryByText("orders")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    expect(screen.getByText(/已选 2/)).toBeInTheDocument();

    // 两端都选时 orders→customers 默认勾选；orders→suppliers 因 suppliers 未选被禁用
    const joinTable = screen.getByTestId("joinTable");
    expect(joinTable.querySelectorAll(".ant-table-row")).toHaveLength(2);
    const supplierRow = within(joinTable)
      .getByText("orders.supplier_id")
      .closest("tr")!;
    expect(within(supplierRow).getByRole("checkbox")).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /确认导入/i }));

    await waitFor(() => expect(api.executeImport).toHaveBeenCalled());
    const request = vi.mocked(api.executeImport).mock.calls[0][1];
    // 只提交勾选的类；join 只保留两端都在子集内的（orders->suppliers 被丢弃）
    expect(request.confirmedClasses.map((c) => c.sourceTable)).toEqual([
      "orders",
      "customers",
    ]);
    expect(request.confirmedJoins.map((j) => j.targetTable)).toEqual(["customers"]);
    expect(request.confirmedJoins[0].inferredBy).toBe("name_convention");
    expect(request.confirmedClasses[0].isSelected).toBe(true);
  });

  it("getImportPreview 失败时显示错误提示，不进入预览步骤", async () => {
    vi.mocked(api.getImportPreview).mockRejectedValue(new Error("预览失败"));
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("orders");
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() => {
      expect(api.getImportPreview).toHaveBeenCalled();
    });
    // 仍在第一步（规则配置）
    expect(screen.getByText(/选择要导入的表/i)).toBeInTheDocument();
  });

  it("executeImport 失败时显示错误提示", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue(
      previewResponse({ proposedClasses: [CLASS_ORDERS] }),
    );
    vi.mocked(api.executeImport).mockRejectedValue(new Error("导入失败"));
    await openPreviewStep();
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    fireEvent.click(screen.getByRole("button", { name: /确认导入/i }));
    await waitFor(() => {
      expect(api.executeImport).toHaveBeenCalled();
    });
  });

  it("上一步按钮：从预览页返回到规则配置页", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue(previewResponse());
    await openPreviewStep();
    fireEvent.click(screen.getByRole("button", { name: /上一步/i }));
    expect(screen.getByText(/选择要导入的表/i)).toBeInTheDocument();
  });

  it("单选一张表时展示列勾选组；勾选真子集后预览携带 selectedColumns", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue(
      previewResponse({ proposedClasses: [CLASS_ORDERS] }),
    );
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("orders");
    // 勾选 orders 行 → 单表模式出现列选择（含表名提示）
    const ruleTable = screen.getByTestId("ruleTable");
    const ordersRow = within(ruleTable).getByText("orders").closest("tr")!;
    fireEvent.click(within(ordersRow).getByRole("checkbox"));
    expect(screen.getByText(/选择 orders 要导入的列/i)).toBeInTheDocument();

    // 默认全列勾选；取消 customer_id 与 supplier_id，只剩 id（真子集）
    fireEvent.click(screen.getByRole("checkbox", { name: "customer_id" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "supplier_id" }));
    expect(screen.getByText(/已选 1 \/ 共 3 列/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() =>
      expect(api.getImportPreview).toHaveBeenCalledWith(
        1,
        expect.objectContaining({
          selectedTables: ["orders"],
          selectedColumns: { orders: ["id"] },
        }),
      ),
    );
  });

  it("默认透传 joinInference 两开关为开（声明外键 + 列名约定）", async () => {
    let captured: unknown;
    vi.mocked(api.getImportPreview).mockImplementation(async (_, req) => {
      captured = req;
      return previewResponse();
    });
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("orders");
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() => expect(captured).toBeDefined());
    const req = captured as { rules: ImportRuleConfig };
    expect(req.rules.joinInference).toEqual({
      inferDeclaredFk: true,
      inferNameConvention: true,
    });
  });

  it("关闭「列名约定」推断开关后，预览请求携带 joinInference.inferNameConvention=false", async () => {
    let captured: unknown;
    vi.mocked(api.getImportPreview).mockImplementation(async (_, req) => {
      captured = req;
      return previewResponse();
    });
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("orders");

    // 规则配置区的 join 推断双 Switch：每对是 Switch + 说明 Tag（声明外键蓝 / 列名约定紫）。
    // antd Space 会把每个孩子包进 .ant-space-item，故从「Sage X3 列名约定」Tag 向上取
    // space-item 的前一个兄弟，再取其中的 Switch 并点击关闭。
    const tag = screen.getByText("Sage X3 列名约定");
    const prevItem = tag.closest(".ant-space-item")?.previousElementSibling;
    const conventionSwitch = prevItem?.querySelector(".ant-switch") ?? null;
    expect(conventionSwitch).not.toBeNull();
    fireEvent.click(conventionSwitch as HTMLElement);

    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() => expect(captured).toBeDefined());
    const req = captured as { rules: ImportRuleConfig };
    // 只关列名约定；声明外键开关保持默认开
    expect(req.rules.joinInference).toEqual({
      inferDeclaredFk: true,
      inferNameConvention: false,
    });
  });

  it("规则配置表支持按表名搜索，过滤后仅剩命中表", async () => {
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("orders");
    // 第一步搜索框（占位「搜索表名」，区别于预览步骤的「搜索表名/类名」）
    fireEvent.change(screen.getByPlaceholderText(/^搜索表名$/), {
      target: { value: "customer" },
    });
    const ruleTable = screen.getByTestId("ruleTable");
    expect(within(ruleTable).getByText("customers")).toBeInTheDocument();
    expect(within(ruleTable).queryByText("orders")).not.toBeInTheDocument();
    expect(within(ruleTable).queryByText("suppliers")).not.toBeInTheDocument();
  });
});

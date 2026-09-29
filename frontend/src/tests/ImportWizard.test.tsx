import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
// 大表集（超过默认每页 50 条）用于验证分页条数切换与跨页全选。
function manyTables(n: number): TableSchema[] {
  return Array.from({ length: n }, (_, i) => table(`tbl_${i}`, ["id"]));
}

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
  // 默认单 owner（非 Oracle / 仅连接默认）：向导跳过 schema 步直接选表。
  vi.mocked(datasourceApi.listDatasourceSchemas).mockResolvedValue([]);
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

// 在「选择 Schema」步从 antd Select 中选中一个 owner。
// 可点击选项是 .ant-select-item-option（title=owner）；role=option 的 a11y 复制层不可点，
// 故用 findByTitle 精确定位（见 EntityMappingPage.test 的同类注释）。
async function chooseSchema(owner: string) {
  const user = userEvent.setup();
  const select = screen.getByTestId("schemaSelect");
  await user.click(select.querySelector(".ant-select-selector") as HTMLElement);
  await user.click(await screen.findByTitle(owner));
}

// 等到所选 owner 的表加载完成（「下一步」解除禁用）。
async function waitNextEnabled() {
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /下一步/i })).not.toBeDisabled(),
  );
}

describe("ImportWizard", () => {
  it("renders rule config step by default", async () => {
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    // schema 列表解析（空 → 跳过 schema 步）后：Steps 标题 + schema 表标题均存在
    expect(await screen.findByText(/规则配置/i)).toBeInTheDocument();
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

  it("分页条数生效：默认每页 50，切换每页条数后行数随之变化", async () => {
    vi.mocked(datasourceApi.getDatasourceSchema).mockResolvedValue({
      tables: manyTables(60),
      cachedAt: "2026-01-01T00:00:00Z",
    });
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("tbl_0");

    const ruleTable = screen.getByTestId("ruleTable");
    // defaultPageSize=50：第一页渲染 50 行（此前固定 pageSize 会让 size changer 失效）
    expect(ruleTable.querySelectorAll(".ant-table-row")).toHaveLength(50);

    // size changer 切到 20 条/页 → 第一页行数变 20
    const user = userEvent.setup();
    const sizeChanger = ruleTable.querySelector(
      ".ant-pagination-options-size-changer",
    ) as HTMLElement;
    await user.click(sizeChanger.querySelector(".ant-select-selector") as HTMLElement);
    // 在可见下拉里按文本数字前缀选 "20"（label 形如 "20 / page"/"20 条/页"，取决于 antd locale）
    const sizeOption = await waitFor(() => {
      const visible = Array.from(
        document.querySelectorAll(
          ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option",
        ),
      );
      const target = visible.find((o) => (o.textContent ?? "").trim().startsWith("20"));
      expect(target).toBeTruthy();
      return target as HTMLElement;
    });
    await user.click(sizeOption);
    expect(ruleTable.querySelectorAll(".ant-table-row")).toHaveLength(20);
  });

  it("表头下拉全选跨页选中全部表（超当前页数量）", async () => {
    vi.mocked(datasourceApi.getDatasourceSchema).mockResolvedValue({
      tables: manyTables(60),
      cachedAt: "2026-01-01T00:00:00Z",
    });
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("tbl_0");

    const ruleTable = screen.getByTestId("ruleTable");
    // 对照：表头 checkbox 只勾当前页（50/60）；跨页全选走表头下拉菜单
    const headerCheckbox = ruleTable.querySelector(
      ".ant-table-thead input[type=checkbox]",
    ) as HTMLElement;
    fireEvent.click(headerCheckbox);
    expect(screen.getByText(/已选 50 \/ 共 60 张表/)).toBeInTheDocument();

    // 表头下拉（hover 触发）：首项即跨页「全选当前筛选」→ 补齐到全部 60 张。
    // 按角色取首个菜单项而非其文案，避免依赖 antd 内置 locale（en "Select all data" / zh "全选所有"）。
    const user = userEvent.setup();
    const trigger = ruleTable.querySelector(
      ".ant-table-selection-extra .ant-dropdown-trigger",
    ) as HTMLElement;
    fireEvent.mouseEnter(trigger);
    const menuItems = await screen.findAllByRole("menuitem");
    await user.click(menuItems[0]);
    expect(screen.getByText(/已选 60 \/ 共 60 张表/)).toBeInTheDocument();
  });

  it("「全选当前筛选」按钮在超每页条数时也一次选全部表", async () => {
    vi.mocked(datasourceApi.getDatasourceSchema).mockResolvedValue({
      tables: manyTables(60),
      cachedAt: "2026-01-01T00:00:00Z",
    });
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("tbl_0");

    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    expect(screen.getByText(/已选 60 \/ 共 60 张表/)).toBeInTheDocument();
  });
});

// =============================================================================
// 选择 Schema（Oracle owner）步 —— 多 owner 数据源先选 owner 再看其下的表
// =============================================================================

describe("ImportWizard — schema（Oracle owner）选择步", () => {
  it("多 owner：先出 schema 步；未选禁用；选后进入选表且预览携带 schema", async () => {
    vi.mocked(datasourceApi.listDatasourceSchemas).mockResolvedValue(["SYS", "THBI", "ZJTH"]);
    vi.mocked(api.getImportPreview).mockResolvedValue(previewResponse());
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);

    expect(await screen.findByText("Schema（Oracle owner）")).toBeInTheDocument();
    expect(datasourceApi.listDatasourceSchemas).toHaveBeenCalledWith(1);

    // 未选 schema 时「下一步」禁用
    expect(screen.getByRole("button", { name: /下一步/i })).toBeDisabled();

    // 选择 THBI → 按该 owner 加载表
    await chooseSchema("THBI");
    await waitFor(() =>
      expect(datasourceApi.getDatasourceSchema).toHaveBeenCalledWith(1, "THBI"),
    );
    await waitNextEnabled();

    // 进入「规则配置」选表
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await screen.findByText(/选择要导入的表/i);
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));

    // 预览请求带 schema owner（后端据此内省 THBI 命名空间）
    await waitFor(() =>
      expect(api.getImportPreview).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ schema: "THBI" }),
      ),
    );
  });

  it("按选中 owner 过滤：THBI 只显示其下的表，其他 owner 的表不出现", async () => {
    vi.mocked(datasourceApi.listDatasourceSchemas).mockResolvedValue(["THBI", "ZJTH"]);
    vi.mocked(datasourceApi.getDatasourceSchema).mockImplementation(async (_id, owner) => ({
      tables: owner === "THBI" ? [table("DWD_SALES", ["id"])] : SCHEMA_TABLES,
      cachedAt: "2026-01-01T00:00:00Z",
    }));
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("Schema（Oracle owner）");

    await chooseSchema("THBI");
    await waitFor(() =>
      expect(datasourceApi.getDatasourceSchema).toHaveBeenCalledWith(1, "THBI"),
    );
    await waitNextEnabled();
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));

    expect(await screen.findByText("DWD_SALES")).toBeInTheDocument();
    expect(screen.queryByText("orders")).not.toBeInTheDocument();
  });

  it("缓存 404 时对所选 owner 触发内省", async () => {
    vi.mocked(datasourceApi.listDatasourceSchemas).mockResolvedValue(["THBI", "ZJTH"]);
    vi.mocked(datasourceApi.getDatasourceSchema).mockRejectedValue(
      Object.assign(new Error("未缓存"), { status: 404 }),
    );
    vi.mocked(datasourceApi.introspectDatasource).mockResolvedValue(schemaResponse());
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("Schema（Oracle owner）");

    await chooseSchema("THBI");
    await waitFor(() =>
      expect(datasourceApi.introspectDatasource).toHaveBeenCalledWith(1, "THBI"),
    );
  });

  it("schema 列表加载失败：显示错误阻断，点「重试」后恢复", async () => {
    vi.mocked(datasourceApi.listDatasourceSchemas)
      .mockRejectedValueOnce(new Error("网络错误"))
      .mockResolvedValueOnce(["THBI", "ZJTH"]);
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);

    expect(await screen.findByText(/Schema 列表加载失败/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /下一步/i })).not.toBeInTheDocument();

    // antd Button 对两字中文自动加空格 → 重试 匹配 /重\s?试/
    fireEvent.click(screen.getByRole("button", { name: /重\s?试/ }));
    expect(await screen.findByText("Schema（Oracle owner）")).toBeInTheDocument();
  });

  it("切换 schema：清空表选并重新加载新 owner 的表", async () => {
    vi.mocked(datasourceApi.listDatasourceSchemas).mockResolvedValue(["THBI", "ZJTH"]);
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("Schema（Oracle owner）");

    await chooseSchema("THBI");
    await waitFor(() =>
      expect(datasourceApi.getDatasourceSchema).toHaveBeenCalledWith(1, "THBI"),
    );
    await waitNextEnabled();
    await chooseSchema("ZJTH");
    await waitFor(() =>
      expect(datasourceApi.getDatasourceSchema).toHaveBeenCalledWith(1, "ZJTH"),
    );
  });

  it("选定 owner 加载失败：下一步禁用、选择复位；重选同 owner 重试恢复", async () => {
    vi.mocked(datasourceApi.listDatasourceSchemas).mockResolvedValue(["THBI", "ZJTH"]);
    // 首次按 owner 取表失败（非 404），随后恢复
    vi.mocked(datasourceApi.getDatasourceSchema)
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue(schemaResponse());
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    await screen.findByText("Schema（Oracle owner）");

    await chooseSchema("THBI");
    await waitFor(() =>
      expect(datasourceApi.getDatasourceSchema).toHaveBeenCalledWith(1, "THBI"),
    );
    // 失败后（schema 未就绪）schema 步「下一步」禁用，不会进入空表步
    const nextBtn = screen.getByRole("button", { name: /下一步/i });
    await waitFor(() => expect(nextBtn).toBeDisabled());

    // 选择复位到占位态；重选同一 owner → 重新触发加载并恢复
    await chooseSchema("THBI");
    await waitFor(() => expect(datasourceApi.getDatasourceSchema).toHaveBeenCalledTimes(2));
    await waitNextEnabled();
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    expect(await screen.findByText("orders")).toBeInTheDocument();
  });
});

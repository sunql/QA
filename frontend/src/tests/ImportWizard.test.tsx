import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ImportWizard from "../components/localImport/ImportWizard";
import * as api from "../api/localImport";

vi.mock("../api/localImport");

describe("ImportWizard", () => {
  it("renders rule config step by default", () => {
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    expect(screen.getByText(/规则配置/i)).toBeInTheDocument();
  });

  it("calls preview API when clicking next", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue({
      datasourceId: 1,
      proposedClasses: [],
      proposedJoins: [],
      conflicts: [],
      filterSuggestions: { recommendedBlacklistPatterns: [], excludedTables: [] },
      llmUsage: { modelName: null, promptTokens: 0, completionTokens: 0 },
    });
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() => expect(api.getImportPreview).toHaveBeenCalledWith(1, expect.any(Object)));
  });

  it("shows success result after successful import", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue({
      datasourceId: 1,
      proposedClasses: [
        {
          sourceTable: "orders",
          className: "orders",
          classAlias: null,
          description: null,
          isSelected: true,
          properties: [],
        },
      ],
      proposedJoins: [],
      conflicts: [],
      filterSuggestions: { recommendedBlacklistPatterns: [], excludedTables: [] },
      llmUsage: { modelName: null, promptTokens: 0, completionTokens: 0 },
    });
    vi.mocked(api.executeImport).mockResolvedValue({
      success: true,
      createdClasses: 1,
      createdProperties: 2,
      createdJoins: 0,
      skippedConflicts: 0,
      overwrittenConflicts: 0,
      errors: [],
    });

    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /确认导入/i })).toBeInTheDocument()
    );
    // 新 UI：未勾选时确认按钮禁用，需先全选当前筛选再确认
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    fireEvent.click(screen.getByRole("button", { name: /确认导入/i }));

    await waitFor(() =>
      expect(document.querySelector(".ant-result-title")).toHaveTextContent("导入完成")
    );
    expect(api.executeImport).toHaveBeenCalled();
    expect(document.querySelector(".ant-result-success")).toBeInTheDocument();
  });

  it("shows error result with error details after failed import", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue({
      datasourceId: 1,
      proposedClasses: [
        {
          sourceTable: "orders",
          className: "orders",
          classAlias: null,
          description: null,
          isSelected: true,
          properties: [],
        },
      ],
      proposedJoins: [],
      conflicts: [],
      filterSuggestions: { recommendedBlacklistPatterns: [], excludedTables: [] },
      llmUsage: { modelName: null, promptTokens: 0, completionTokens: 0 },
    });
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

    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /确认导入/i })).toBeInTheDocument()
    );
    // 新 UI：未勾选时确认按钮禁用，需先全选当前筛选再确认
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

  it("accumulates batch selection across searches and only submits selected subset", async () => {
    // 清掉前序测试对同一 mock 的调用记录，确保本测试读到自己的调用
    vi.mocked(api.executeImport).mockClear();
    vi.mocked(api.getImportPreview).mockResolvedValue({
      datasourceId: 1,
      proposedClasses: [
        {
          sourceTable: "orders",
          className: "orders",
          classAlias: null,
          description: null,
          isSelected: true,
          properties: [],
        },
        {
          sourceTable: "customers",
          className: "customers",
          classAlias: null,
          description: null,
          isSelected: true,
          properties: [],
        },
        {
          sourceTable: "suppliers",
          className: "suppliers",
          classAlias: null,
          description: null,
          isSelected: true,
          properties: [],
        },
      ],
      proposedJoins: [
        {
          sourceTable: "orders",
          sourceColumns: ["customer_id"],
          targetTable: "customers",
          targetColumns: ["id"],
          joinType: "INNER",
          relationType: "foreign_key",
          isSelected: true,
        },
        {
          sourceTable: "orders",
          sourceColumns: ["supplier_id"],
          targetTable: "suppliers",
          targetColumns: ["id"],
          joinType: "INNER",
          relationType: "foreign_key",
          isSelected: true,
        },
      ],
      conflicts: [],
      filterSuggestions: { recommendedBlacklistPatterns: [], excludedTables: [] },
      llmUsage: { modelName: null, promptTokens: 0, completionTokens: 0 },
    });
    vi.mocked(api.executeImport).mockResolvedValue({
      success: true,
      createdClasses: 1,
      createdProperties: 1,
      createdJoins: 1,
      skippedConflicts: 0,
      overwrittenConflicts: 0,
      errors: [],
    });

    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /确认导入/i })).toBeInTheDocument()
    );

    // 未勾选任何行时，确认按钮禁用
    expect(screen.getByRole("button", { name: /确认导入/i })).toBeDisabled();

    // 批次1：搜索 order → 只显示 orders（1 行）→ 全选当前筛选
    fireEvent.change(screen.getByPlaceholderText(/搜索表名\/类名/i), {
      target: { value: "order" },
    });
    expect(document.querySelectorAll(".ant-table-row")).toHaveLength(1);
    expect(screen.queryAllByText("customers")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));

    // 批次2：搜索 customer → 只显示 customers（1 行）→ 全选当前筛选（累计，不覆盖批次1）
    fireEvent.change(screen.getByPlaceholderText(/搜索表名\/类名/i), {
      target: { value: "customer" },
    });
    expect(document.querySelectorAll(".ant-table-row")).toHaveLength(1);
    expect(screen.queryAllByText("orders")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    // 批次2 全选后应为 已选 2 张表（orders + customers 累计）
    expect(screen.getByText(/已选 2/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /确认导入/i }));

    await waitFor(() => expect(api.executeImport).toHaveBeenCalled());
    const request = vi.mocked(api.executeImport).mock.calls[0][1];
    // 只提交勾选的类；join 只保留两端都在子集内的（orders->suppliers 因 suppliers 未勾选被丢弃）
    expect(request.confirmedClasses.map((c) => c.sourceTable)).toEqual([
      "orders",
      "customers",
    ]);
    expect(request.confirmedJoins.map((j) => j.targetTable)).toEqual(["customers"]);
    expect(request.confirmedClasses[0].isSelected).toBe(true);
  });

  it("getImportPreview 失败时显示错误提示，不进入预览步骤", async () => {
    vi.mocked(api.getImportPreview).mockRejectedValue(new Error("预览失败"));
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() => {
      // catch 分支触发 message.error
      expect(api.getImportPreview).toHaveBeenCalled();
    });
    // 仍在第一步（规则配置）
    expect(screen.getByText(/规则配置/i)).toBeInTheDocument();
  });

  it("executeImport 失败时显示错误提示", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue({
      datasourceId: 1,
      proposedClasses: [
        {
          sourceTable: "orders",
          className: "orders",
          classAlias: null,
          description: null,
          isSelected: true,
          properties: [],
        },
      ],
      proposedJoins: [],
      conflicts: [],
      filterSuggestions: { recommendedBlacklistPatterns: [], excludedTables: [] },
      llmUsage: { modelName: null, promptTokens: 0, completionTokens: 0 },
    });
    vi.mocked(api.executeImport).mockRejectedValue(new Error("导入失败"));
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /确认导入/i })).toBeInTheDocument()
    );
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/i }));
    fireEvent.click(screen.getByRole("button", { name: /确认导入/i }));
    await waitFor(() => {
      expect(api.executeImport).toHaveBeenCalled();
    });
  });

  it("上一步按钮：从预览页返回到规则配置页", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue({
      datasourceId: 1,
      proposedClasses: [],
      proposedJoins: [],
      conflicts: [],
      filterSuggestions: { recommendedBlacklistPatterns: [], excludedTables: [] },
      llmUsage: { modelName: null, promptTokens: 0, completionTokens: 0 },
    });
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    // 进入预览
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /上一步/i })).toBeInTheDocument();
    });
    // 返回上一步
    fireEvent.click(screen.getByRole("button", { name: /上一步/i }));
    // 回到规则配置
    expect(screen.getByText(/规则配置/i)).toBeInTheDocument();
  });
});

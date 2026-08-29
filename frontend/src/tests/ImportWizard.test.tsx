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
    fireEvent.click(screen.getByRole("button", { name: /确认导入/i }));

    await waitFor(() =>
      expect(screen.getByText("导入未完全成功")).toBeInTheDocument()
    );
    expect(document.querySelector(".ant-result-error")).toBeInTheDocument();
    expect(screen.getByText("property")).toBeInTheDocument();
    expect(screen.getByText("orders.customer_id")).toBeInTheDocument();
    expect(screen.getByText("NOT NULL violation")).toBeInTheDocument();
  });
});

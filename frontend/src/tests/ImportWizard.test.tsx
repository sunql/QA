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
});

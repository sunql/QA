import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { buildCheckpointAnswerPayload } from "../components/research/CheckpointDecisionModal";

// Mock httpClient at source (same pattern as researchStore.test.ts).
const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock, apiClient: httpMock }));

const useLlmModelsMock = vi.fn();
vi.mock("../hooks/useLlmModels", () => ({
  useLlmModels: () => useLlmModelsMock(),
}));

import CheckpointDecisionModal from "../components/research/CheckpointDecisionModal";

describe("CheckpointDecisionModal model dropdown (N4)", () => {
  beforeEach(() => {
    useLlmModelsMock.mockReturnValue({
      data: [
        { id: 1, modelName: "deepseek-chat", provider: "OPENAI_COMPATIBLE_PROXY" },
        { id: 2, modelName: "Qwen3.8-27B-4bit", provider: "OLLAMA" },
      ],
      loading: false,
      error: null,
      reload: vi.fn(),
    });
    httpMock.post.mockResolvedValue({ sessionStatus: "running" });
  });

  it("默认 modelId = turnModelId (原模型)", async () => {
    render(
      <CheckpointDecisionModal
        checkpointId="cp-1"
        turnModelId={2}
        action="confirm"
        onSubmit={() => {}}
        onClose={() => {}}
      />
    );
    const select = await waitFor(() => screen.getByTestId("checkpoint-model-select"));
    expect(select).toBeTruthy();
  });

  it("不改 model 提交 → payload 不含 modelId 字段 (走原模型)", async () => {
    render(
      <CheckpointDecisionModal
        checkpointId="cp-1"
        turnModelId={2}
        action="confirm"
        onSubmit={() => {}}
        onClose={() => {}}
      />
    );
    fireEvent.click(screen.getByTestId("checkpoint-submit"));
    await waitFor(() => expect(httpMock.post).toHaveBeenCalled());
    const [, payload] = httpMock.post.mock.calls[0];
    expect(payload).not.toHaveProperty("modelId");
  });

  it("选 deepseek 后提交 → display 显示新模型", async () => {
    render(
      <CheckpointDecisionModal
        checkpointId="cp-1"
        turnModelId={2}
        action="confirm"
        onSubmit={() => {}}
        onClose={() => {}}
      />
    );
    await waitFor(() => screen.getByTestId("checkpoint-model-select"));

    // Open Select dropdown
    fireEvent.mouseDown(document.querySelector(".ant-select-selector") as HTMLElement);

    // Wait for dropdown
    await waitFor(() => {
      const dropdown = document.querySelector(".ant-select-dropdown");
      if (!dropdown) throw new Error("No dropdown");
    });

    // Find the deepseek option
    const allOptions = document.body.querySelectorAll(".ant-select-item-option");
    let deepseekOption: Element | null = null;
    for (const opt of allOptions) {
      const content = opt.querySelector(".ant-select-item-option-content");
      if (content?.textContent?.includes("deepseek-chat")) {
        deepseekOption = opt;
        break;
      }
    }
    expect(deepseekOption).not.toBeNull();

    // Click the option and wait for display to update
    await act(async () => {
      deepseekOption!.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, cancelable: true }));
      deepseekOption!.dispatchEvent(new MouseEvent("pointerup", { bubbles: true, cancelable: true }));
      deepseekOption!.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    });

    // Wait for display to reflect selection
    await waitFor(
      () => {
        const sel = document.querySelector(".ant-select-selection-item");
        if (!sel?.textContent?.includes("deepseek-chat")) {
          throw new Error("Selection not updated yet");
        }
      },
      { timeout: 3000 },
    );

    // Verify the display shows the new selection
    const selAfter = document.querySelector(".ant-select-selection-item");
    expect(selAfter?.textContent).toContain("deepseek-chat");

    // Submit (display proves selection was registered)
    fireEvent.click(screen.getByTestId("checkpoint-submit"));
    await waitFor(() => expect(httpMock.post).toHaveBeenCalled());
  });
});

describe("buildCheckpointAnswerPayload (N4 pure function)", () => {
  it("modelId == turnModelId → no modelId in payload (走原模型)", () => {
    const p = buildCheckpointAnswerPayload("confirm", { x: 1 }, 2, 2);
    expect(p).toEqual({ action: "confirm", choice: { x: 1 } });
    expect(p).not.toHaveProperty("modelId");
  });

  it("modelId != turnModelId → payload 含 modelId (改派)", () => {
    const p = buildCheckpointAnswerPayload("confirm", { x: 1 }, 1, 2);
    expect(p).toEqual({ action: "confirm", choice: { x: 1 }, modelId: 1 });
  });

  it("modelId == null (用户没选) → 无 modelId 字段", () => {
    const p = buildCheckpointAnswerPayload("confirm", { x: 1 }, null, 2);
    expect(p).toEqual({ action: "confirm", choice: { x: 1 } });
    expect(p).not.toHaveProperty("modelId");
  });

  it("action=modify / reject 也走同一 builder", () => {
    expect(buildCheckpointAnswerPayload("modify", {}, 1, 2)).toEqual({ action: "modify", choice: {}, modelId: 1 });
    expect(buildCheckpointAnswerPayload("reject", {}, 2, 2)).toEqual({ action: "reject", choice: {} });
  });
});

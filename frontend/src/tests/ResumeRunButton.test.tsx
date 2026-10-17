import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";

const useLlmModelsMock = vi.fn();
vi.mock("../hooks/useLlmModels", () => ({
  useLlmModels: () => useLlmModelsMock(),
}));
vi.mock("../i18n", () => ({
  useTranslation: () => ({
    t: (k: string) => {
      const m: Record<string, string> = {
        "multiStep.resume": "续跑",
        "multiStep.resumeChooseModel": "选择续跑模型",
        "multiStep.resumeSameModelHint": "（该模型刚失败）",
        "multiStep.resumeWithSelected": "用所选模型续跑",
        "multiStep.resumeWithOriginal": "原模型续跑",
        "common.cancel": "取消",
      };
      return m[k] ?? k;
    },
  }),
}));

import ResumeRunButton from "../components/chat/ResumeRunButton";

describe("ResumeRunButton", () => {
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
  });

  it("点击续跑 → 弹 Popover 含 model select", async () => {
    const onResume = vi.fn();
    render(
      <ResumeRunButton
        runId="r-1" fromStepIndex={2} originalModelId={2}
        onResume={onResume}
      />
    );
    fireEvent.click(screen.getByTestId("resume-run"));
    await waitFor(() => screen.getByTestId("resume-model-select"));
  });

  it("selectedModelId === originalModelId → 显示 '该模型刚失败' 提示", async () => {
    const onResume = vi.fn();
    render(<ResumeRunButton runId="r-1" fromStepIndex={2} originalModelId={2} onResume={onResume} />);
    fireEvent.click(screen.getByTestId("resume-run"));
    await waitFor(() => screen.getByTestId("resume-same-model-hint"));
  });

  it("点 '用所选模型续跑' → onResume 收到 selectedModelId", async () => {
    const onResume = vi.fn();
    render(<ResumeRunButton runId="r-1" fromStepIndex={2} originalModelId={2} onResume={onResume} />);
    fireEvent.click(screen.getByTestId("resume-run"));
    await waitFor(() => screen.getByTestId("resume-model-select"));
    // antd Select：mouseDown 打开下拉，点选项文本
    fireEvent.mouseDown(document.querySelector(".ant-select-selector") as HTMLElement);
    fireEvent.click(await screen.findByText(/^deepseek-chat/));
    fireEvent.click(screen.getByTestId("resume-with-selected"));
    expect(onResume).toHaveBeenCalledWith("r-1", 2, 1);
  });

  it("点 '原模型续跑' → onResume 收到 null", async () => {
    const onResume = vi.fn();
    render(<ResumeRunButton runId="r-1" fromStepIndex={2} originalModelId={2} onResume={onResume} />);
    fireEvent.click(screen.getByTestId("resume-run"));
    await waitFor(() => screen.getByTestId("resume-with-original"));
    fireEvent.click(screen.getByTestId("resume-with-original"));
    expect(onResume).toHaveBeenCalledWith("r-1", 2, null);
  });
});

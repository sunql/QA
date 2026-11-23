/** I3 采购 Copilot 页测试（mock API，真实 i18n）。

判据要点：
1. 场景只列 **runnable** 的 Agent；模型必须显式选（缺一不可发送）；
2. 发送后按 SSE 事件渲染：轨迹步骤 → 最终答案 → tokens；
3. **PAUSED 渲染成追问**（ASK_USER 的 planSummary），而不是「失败」；
4. 发送失败（HTTP 4xx）把后端 detail 展示出来，不是空白。
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { describe, expect, it, vi } from "vitest";
import i18n from "../../i18n/i18n";
import ProcurementCopilotPage from "../ProcurementCopilotPage";

const api = vi.hoisted(() => ({
  getAgentCatalog: vi.fn(),
  listModels: vi.fn(),
  streamCopilotMessage: vi.fn(),
  getAgentRunSteps: vi.fn(),
}));

vi.mock("../../api/procurementKnowledge", () => ({
  getAgentCatalog: api.getAgentCatalog,
}));
vi.mock("../../api/modelConfig", () => ({ listModels: api.listModels }));
vi.mock("../../api/procurementCopilot", () => ({
  streamCopilotMessage: api.streamCopilotMessage,
}));
vi.mock("../../api/agentOrchestrationApi", () => ({
  getAgentRunSteps: api.getAgentRunSteps,
}));

function renderPage() {
  return render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <ProcurementCopilotPage />
      </ConfigProvider>
    </I18nextProvider>,
  );
}

function mockOptions() {
  api.getAgentCatalog.mockResolvedValue([
    { agentCode: "PROCUREMENT_SOURCING", agentName: "采购寻源 Agent", runnable: true, status: "active", description: null, tool: null },
    { agentCode: "PROCUREMENT_COPILOT", agentName: "采购 AI 副驾", runnable: false, status: "active", description: null, tool: null },
  ]);
  api.listModels.mockResolvedValue([
    { id: 3, modelName: "Qwen3.8-27B-4bit", provider: "local", isActive: true },
  ]);
}

describe("ProcurementCopilotPage", () => {
  it("只列出可运行场景，并渲染执行轨迹与答案", async () => {
    mockOptions();
    api.streamCopilotMessage.mockImplementation(async (_payload, handlers) => {
      handlers.onRun?.({ runId: "r-1", stepsUrl: "/api/v1/agent-runs/r-1/steps" });
      handlers.onStep?.({
        stepNo: 0,
        iterationNo: 1,
        kind: "PLAN",
        status: "SUCCEEDED",
        action: "CALL_TOOL",
        planSummary: "先定位物料",
        toolName: "material_search",
        answer: null,
        errorCode: null,
        errorMessage: null,
      });
      handlers.onDone?.({
        runId: "r-1",
        status: "SUCCEEDED",
        answer: "命中 16 个物料",
        pauseCode: null,
        errorCode: null,
        errorMessage: null,
        totalTokens: 4337,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/r-1/steps",
      });
    });

    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "找不锈钢板" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(screen.getByText("命中 16 个物料")).toBeTruthy());
    expect(screen.getByText(/material_search/)).toBeTruthy();
    expect(screen.getByText(/4337/)).toBeTruthy();
    // 不可运行的场景不在下拉里
    expect(screen.queryByText("采购 AI 副驾")).toBeNull();
  });

  it("PAUSED 渲染为追问（不是失败）", async () => {
    mockOptions();
    api.streamCopilotMessage.mockImplementation(async (_payload, handlers) => {
      handlers.onStep?.({
        stepNo: 2,
        iterationNo: 2,
        kind: "PLAN",
        status: "SKIPPED",
        action: "ASK_USER",
        planSummary: "请确认是哪一个规格的不锈钢板",
        toolName: null,
        answer: null,
        errorCode: null,
        errorMessage: null,
      });
      handlers.onDone?.({
        runId: "r-2",
        status: "PAUSED",
        answer: null,
        pauseCode: "NEEDS_INPUT",
        errorCode: null,
        errorMessage: null,
        totalTokens: 100,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/r-2/steps",
      });
    });

    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "找不锈钢板" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    // 追问会同时出现在轨迹（步骤）和高亮 Alert 里 —— 至少一处即可
    await waitFor(() =>
      expect(screen.getAllByText("请确认是哪一个规格的不锈钢板").length).toBeGreaterThan(0),
    );
    expect(screen.getByText("NEEDS_INPUT")).toBeTruthy();
  });

  it("发送失败展示后端 detail", async () => {
    mockOptions();
    api.streamCopilotMessage.mockRejectedValue(new Error("Copilot 只接受采购域 Agent"));

    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "找不锈钢板" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() =>
      expect(screen.getByText("Copilot 只接受采购域 Agent")).toBeTruthy(),
    );
  });
});


describe("ProcurementCopilotPage 执行轨迹", () => {
  function mockOneTurn() {
    mockOptions();
    api.streamCopilotMessage.mockImplementation(async (_payload, handlers) => {
      handlers.onRun?.({ runId: "r-9", stepsUrl: "/api/v1/agent-runs/r-9/steps" });
      handlers.onDone?.({
        runId: "r-9",
        status: "SUCCEEDED",
        answer: "答案",
        pauseCode: null,
        errorCode: null,
        errorMessage: null,
        totalTokens: 10,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/r-9/steps",
      });
    });
  }

  async function sendOneQuestion() {
    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "找不锈钢板" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(screen.getByText("答案")).toBeTruthy());
  }

  it("轨迹按钮不是裸链接（裸链接带不上 Authorization ⇒ 新窗口只会看到「请先登录」）", async () => {
    mockOneTurn();
    await sendOneQuestion();

    expect(
      screen.queryByRole("link", { name: /查看执行轨迹/ }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: /查看执行轨迹/ }),
    ).toBeTruthy();
  });

  it("点击后带鉴权拉取 /agent-runs/{runId}/steps 并在页内抽屉展示", async () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    mockOneTurn();
    api.getAgentRunSteps.mockResolvedValue([
      {
        stepNo: 0,
        iterationNo: 1,
        kind: "PLAN",
        status: "SUCCEEDED",
        action: "CALL_TOOL",
        planSummary: "先定位物料",
        toolName: "material_search",
        inputJson: null,
        outputJson: null,
        answer: null,
        errorCode: null,
        errorMessage: null,
        promptTokens: 12,
        completionTokens: 3,
        cost: 0,
        startedAt: "2026-11-23T10:00:00Z",
        finishedAt: "2026-11-23T10:00:01Z",
      },
    ]);
    await sendOneQuestion();

    fireEvent.click(screen.getByRole("button", { name: /查看执行轨迹/ }));

    await waitFor(() =>
      expect(api.getAgentRunSteps).toHaveBeenCalledWith("r-9"),
    );
    await waitFor(() =>
      expect(screen.getByText("先定位物料")).toBeTruthy(),
    );
    // 不新开窗口（否则必然回到「请先登录」）
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it("先收到超时事件、随后传输中断：保留超时文案（不被网络错误覆盖）", async () => {
    mockOptions();
    api.streamCopilotMessage.mockImplementation(async (_payload, handlers) => {
      handlers.onRun?.({ runId: "r-7", stepsUrl: "/api/v1/agent-runs/r-7/steps" });
      handlers.onError?.({
        error: "本次流式转发的等待时间已到；运行仍在后台继续，可用轨迹地址继续跟进",
        errorType: "timeout",
        runId: "r-7",
        stepsUrl: "/api/v1/agent-runs/r-7/steps",
      });
      // nginx 在上游正常收尾后仍可能重置连接 ⇒ fetch 的 body reader 抛 TypeError
      throw new TypeError("Failed to fetch");
    });

    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());
    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "找不锈钢板" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() =>
      expect(screen.getByText(/流式转发的等待时间已到/)).toBeTruthy(),
    );
    expect(screen.queryByText("Failed to fetch")).toBeNull();
  });

  it("拉取失败时在抽屉内提示错误，而不是空白", async () => {
    mockOneTurn();
    api.getAgentRunSteps.mockRejectedValue(new Error("请先登录"));
    await sendOneQuestion();

    fireEvent.click(screen.getByRole("button", { name: /查看执行轨迹/ }));

    await waitFor(() => expect(screen.getByText("请先登录")).toBeTruthy());
  });
});

describe("ProcurementCopilotPage 多轮会话", () => {
  it("第二轮带上后端回传的 conversationId；新会话清空会话 id", async () => {
    mockOptions();
    const payloads: Array<Record<string, unknown>> = [];
    api.streamCopilotMessage.mockImplementation(async (payload, handlers) => {
      payloads.push(payload);
      handlers.onRun?.({
        runId: `r-${payloads.length}`,
        stepsUrl: "/api/v1/agent-runs/x/steps",
        conversationId: "conv-123456",
        resumed: false,
      });
      handlers.onDone?.({
        runId: `r-${payloads.length}`,
        status: "SUCCEEDED",
        answer: "答案",
        pauseCode: null,
        errorCode: null,
        errorMessage: null,
        totalTokens: 1,
        cost: 0,
        stepsUrl: "/api/v1/agent-runs/x/steps",
      });
    });

    renderPage();
    await waitFor(() => expect(api.getAgentCatalog).toHaveBeenCalled());

    const box = screen.getByPlaceholderText(/不锈钢板/);
    fireEvent.change(box, { target: { value: "第一问" } });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(payloads).toHaveLength(1));
    expect(payloads[0].conversationId).toBeUndefined();

    fireEvent.change(box, { target: { value: "第二问" } });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(payloads).toHaveLength(2));
    expect(payloads[1].conversationId).toBe("conv-123456");

    fireEvent.click(screen.getByRole("button", { name: /新会话/ }));
    fireEvent.change(box, { target: { value: "第三问" } });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(payloads).toHaveLength(3));
    expect(payloads[2].conversationId).toBeUndefined();
  });
});

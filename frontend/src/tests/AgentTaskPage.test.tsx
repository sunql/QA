import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import AgentTaskPage, { RUN_POLL_MS } from "../pages/AgentTaskPage";
import type { AgentOrchestrationProfileRead } from "../types/agentOrchestration";
import type { AgentRunRead, AgentRunStepRead } from "../types/agentOrchestration";
import type { ModelConfig } from "../types/modelConfig";

const api = vi.hoisted(() => ({
  listOrchestrations: vi.fn(),
  createAgentRun: vi.fn(),
  getAgentRun: vi.fn(),
  getAgentRunSteps: vi.fn(),
  resumeAgentRun: vi.fn(),
  cancelAgentRun: vi.fn(),
  listModels: vi.fn(),
  runAgent: vi.fn(),
  sendMessage: vi.fn(),
}));

vi.mock("../api/agentOrchestrationApi", () => api);
vi.mock("../api/modelConfig", () => api);
vi.mock("../api/agentRegistry", () => api);
vi.mock("../api/chat", () => api);

const enabledProfile: AgentOrchestrationProfileRead = {
  agentCode: "SUPPLIER_RISK_AGENT",
  instructions: "评估供应商风险",
  enabled: true,
  defaultMaxIterations: 5,
  defaultMaxToolCalls: 10,
  defaultMaxCostUsd: 1,
  defaultTimeoutSeconds: 120,
  maxObservationChars: 5000,
  revision: 1,
  bindings: [{ toolName: "supplier_risk", enabled: true, sortOrder: 1 }],
};

const disabledProfile: AgentOrchestrationProfileRead = {
  ...enabledProfile,
  agentCode: "DRAFT_AGENT",
  enabled: false,
};

const activeModel: ModelConfig = {
  id: 7,
  modelName: "gpt-4o-mini",
  provider: "openai",
  apiEndpoint: "https://api.example.com",
  isActive: true,
  costPer1KInput: 1,
  costPer1KOutput: 2,
  maxInputTokens: 1000,
  weight: 1,
  costThreshold: 10,
  createdAt: "2026-10-20T00:00:00Z",
  updatedAt: "2026-10-20T00:00:00Z",
};

function makeRun(overrides: Partial<AgentRunRead>): AgentRunRead {
  return {
    runId: "run-1",
    agentCode: "SUPPLIER_RISK_AGENT",
    question: "评估供应商 10105 的风险",
    createdBy: "tester",
    modelConfigId: 7,
    status: "RUNNING",
    instructionsSnapshot: "",
    toolBindingsSnapshot: [],
    maxIterations: 5,
    maxToolCalls: 10,
    maxCostUsd: 1,
    timeoutSeconds: 120,
    iterationCount: 0,
    toolCallCount: 0,
    promptTokens: 0,
    completionTokens: 0,
    totalTokens: 0,
    cost: 0,
    costSource: "UNKNOWN",
    costComplete: false,
    finalAnswer: null,
    pauseCode: null,
    pauseDetail: null,
    errorCode: null,
    errorMessage: null,
    cancelRequested: false,
    createdTime: "2026-10-26T10:00:00Z",
    updatedTime: "2026-10-26T10:00:00Z",
    queuedAt: "2026-10-26T10:00:00Z",
    startedAt: "2026-10-26T10:00:01Z",
    finishedAt: null,
    revision: 1,
    ...overrides,
  };
}

function makeStep(overrides: Partial<AgentRunStepRead>): AgentRunStepRead {
  return {
    stepNo: 0,
    iterationNo: 1,
    kind: "PLAN",
    status: "SUCCEEDED",
    action: null,
    planSummary: null,
    toolName: null,
    inputJson: null,
    outputJson: null,
    answer: null,
    errorCode: null,
    errorMessage: null,
    promptTokens: 0,
    completionTokens: 0,
    cost: 0,
    startedAt: "2026-10-26T10:00:00Z",
    finishedAt: "2026-10-26T10:00:01Z",
    ...overrides,
  };
}

/** 刷新微任务队列（API mock 是真实 Promise，不是定时器）。 */
async function flush(): Promise<void> {
  for (let index = 0; index < 6; index += 1) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

/** 推进轮询定时器。 */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await flush();
}

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/agent-tasks"]}>
        <Routes>
          <Route path="/agent-tasks" element={<AgentTaskPage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>,
  );
}

/** 打开第 index 个 antd Select 并选中指定选项。 */
async function selectOption(index: number, label: string): Promise<void> {
  const selectors = document.querySelectorAll(".ant-select-selector");
  fireEvent.mouseDown(selectors[index] as HTMLElement);
  await flush();
  // antd 的 Select 同时渲染一份无障碍镜像与一份可视列表，同一文案会出现两次；
  // 取最后一个（可视列表里的选项）点击。
  const matches = screen.getAllByText(label);
  fireEvent.click(matches[matches.length - 1] as HTMLElement);
  await flush();
}

/** 走完「选配置 → 选模型 → 填输入 → 创建」的完整交互。 */
async function createRun(): Promise<void> {
  await selectOption(0, "SUPPLIER_RISK_AGENT");
  await selectOption(1, "gpt-4o-mini");
  fireEvent.change(
    screen.getByPlaceholderText("描述需要 Agent 执行的任务"),
    { target: { value: "评估供应商 10105 的风险" } },
  );
  fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*运\s*行/ }));
  await flush();
}

describe("AgentTaskPage", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    api.listOrchestrations.mockResolvedValue([enabledProfile, disabledProfile]);
    api.listModels.mockResolvedValue([activeModel]);
    api.createAgentRun.mockResolvedValue({ runId: "run-1", status: "QUEUED" });
    api.getAgentRun.mockResolvedValue(makeRun({ status: "RUNNING" }));
    api.getAgentRunSteps.mockResolvedValue([]);
    api.resumeAgentRun.mockResolvedValue(makeRun({ status: "RUNNING" }));
    api.cancelAgentRun.mockResolvedValue(makeRun({ status: "RUNNING" }));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it("只加载启用中的编排配置与启用中的模型", async () => {
    renderPage();
    await flush();
    expect(api.listModels).toHaveBeenCalledWith(true);
    await selectOption(0, "SUPPLIER_RISK_AGENT");
    // 未启用的配置不出现在下拉里
    expect(screen.queryByText("DRAFT_AGENT")).toBeNull();
  });

  it("创建运行：提交用户选中的 profile 与模型，并选中返回的运行", async () => {
    renderPage();
    await flush();
    await createRun();
    expect(api.createAgentRun).toHaveBeenCalledWith({
      agentCode: "SUPPLIER_RISK_AGENT",
      input: "评估供应商 10105 的风险",
      modelConfigId: 7,
    });
    expect(api.getAgentRun).toHaveBeenCalledWith("run-1");
    expect(screen.getByText("运行 ID：run-1")).toBeInTheDocument();
  });

  it("运行中按固定周期轮询，并按 lastStepNo+1 增量拉取、去重排序合并步骤", async () => {
    const step0 = makeStep({
      stepNo: 0,
      kind: "PLAN",
      action: "CALL_TOOL",
      planSummary: "先查风险",
      toolName: "supplier_risk",
    });
    const step1 = makeStep({
      stepNo: 1,
      kind: "TOOL",
      toolName: "supplier_risk",
      outputJson: { text: "{}", truncated: false, original_chars: 2 },
    });
    const step2 = makeStep({ stepNo: 2, kind: "TOOL", toolName: "supplier_otsd" });
    api.getAgentRunSteps.mockImplementation(
      async (_runId: string, query?: { afterStepNo?: number }) => {
        const pages: Record<number, AgentRunStepRead[]> = {
          0: [step1, step0],
          2: [step2],
        };
        return pages[query?.afterStepNo ?? 0] ?? [];
      },
    );

    renderPage();
    await flush();
    await createRun();

    await advance(RUN_POLL_MS);
    expect(api.getAgentRunSteps).toHaveBeenCalledWith("run-1", { afterStepNo: 0 });
    // 乱序返回也按 stepNo 升序渲染
    const rendered = screen.getAllByTestId(/agent-run-step-/);
    expect(rendered.map((node) => node.getAttribute("data-testid"))).toEqual([
      "agent-run-step-0",
      "agent-run-step-1",
    ]);

    await advance(RUN_POLL_MS);
    expect(api.getAgentRunSteps).toHaveBeenCalledWith("run-1", { afterStepNo: 2 });
    expect(screen.getAllByTestId(/agent-run-step-/)).toHaveLength(3);
  });

  it("暂停后停止轮询", async () => {
    api.getAgentRun.mockResolvedValue(
      makeRun({ status: "PAUSED", pauseCode: "NEEDS_INPUT" }),
    );
    renderPage();
    await flush();
    await createRun();
    const before = api.getAgentRun.mock.calls.length;
    await advance(RUN_POLL_MS * 2);
    expect(api.getAgentRun.mock.calls.length).toBe(before);
  });

  it("组件卸载后停止轮询", async () => {
    const { unmount } = renderPage();
    await flush();
    await createRun();
    const before = api.getAgentRun.mock.calls.length;
    unmount();
    await advance(RUN_POLL_MS * 2);
    expect(api.getAgentRun.mock.calls.length).toBe(before);
  });

  it("PAUSED + NEEDS_INPUT 必须补充输入才能恢复", async () => {
    api.getAgentRun.mockResolvedValue(
      makeRun({ status: "PAUSED", pauseCode: "NEEDS_INPUT" }),
    );
    renderPage();
    await flush();
    await createRun();
    await advance(RUN_POLL_MS);
    expect(screen.getByText("需要补充输入")).toBeInTheDocument();

    // 未填补充输入 → 友好提示，且不发起恢复
    fireEvent.click(screen.getByRole("button", { name: /恢\s*复\s*运\s*行/ }));
    await flush();
    expect(screen.getByText("请先填写补充输入")).toBeInTheDocument();
    expect(api.resumeAgentRun).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("补充输入"), {
      target: { value: "供应商 10105" },
    });
    fireEvent.click(screen.getByRole("button", { name: /恢\s*复\s*运\s*行/ }));
    await flush();
    expect(api.resumeAgentRun).toHaveBeenCalledWith("run-1", {
      input: "供应商 10105",
    });
  });

  it("预算暂停只暴露与 pauseDetail.budget 匹配的那一个增量", async () => {
    api.getAgentRun.mockResolvedValue(
      makeRun({
        status: "PAUSED",
        pauseCode: "BUDGET_EXHAUSTED",
        pauseDetail: { budget: "iteration" },
      }),
    );
    renderPage();
    await flush();
    await createRun();
    await advance(RUN_POLL_MS);

    expect(screen.getByLabelText("追加迭代次数")).toBeInTheDocument();
    expect(screen.queryByLabelText("追加工具调用次数")).toBeNull();
    expect(screen.queryByLabelText("追加成本上限（美元）")).toBeNull();
    expect(screen.queryByLabelText("追加超时时间（秒）")).toBeNull();

    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "3" } });
    await flush();
    fireEvent.click(screen.getByRole("button", { name: /恢\s*复\s*运\s*行/ }));
    await flush();
    expect(api.resumeAgentRun).toHaveBeenCalledWith("run-1", {
      additionalIterations: 3,
    });
  });

  it("运行中取消：先显示「已请求取消」，直到轮询报 CANCELLED 才显示已取消", async () => {
    renderPage();
    await flush();
    await createRun();
    await advance(RUN_POLL_MS);

    fireEvent.click(screen.getByRole("button", { name: /取\s*消\s*运\s*行/ }));
    await flush();
    expect(api.cancelAgentRun).toHaveBeenCalledWith("run-1");
    expect(
      screen.getByText("已请求取消，运行仍在进行中，等待下一次轮询确认。"),
    ).toBeInTheDocument();

    api.getAgentRun.mockResolvedValue(makeRun({ status: "CANCELLED" }));
    await advance(RUN_POLL_MS);
    expect(screen.getByText("已取消")).toBeInTheDocument();
    expect(
      screen.queryByText("已请求取消，运行仍在进行中，等待下一次轮询确认。"),
    ).toBeNull();
  });

  it("404 显示友好消息，且不伪造终态运行", async () => {
    api.getAgentRun.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404 }),
    );
    renderPage();
    await flush();
    await createRun();
    expect(
      screen.getByText("找不到该运行，可能已被删除或当前无权访问"),
    ).toBeInTheDocument();
    expect(screen.queryByText("已失败")).toBeNull();
    expect(screen.queryByText("运行 ID：run-1")).toBeNull();
  });

  it("运行级失败时展示后端的失败原因（没有 FAILED 步骤行时也不能吞掉诊断）", async () => {
    api.getAgentRun.mockResolvedValue(
      makeRun({
        status: "FAILED",
        errorCode: "PLANNER_OUTPUT_INVALID",
        errorMessage: "规划器输出不合法：缺少 action",
      }),
    );
    renderPage();
    await flush();
    await createRun();
    await advance(RUN_POLL_MS);
    expect(
      screen.getByText("运行失败：规划器输出不合法：缺少 action"),
    ).toBeInTheDocument();
    expect(screen.getByText("错误码：PLANNER_OUTPUT_INVALID")).toBeInTheDocument();
  });

  it("422 但后端没给诊断文案时给出通用校验提示", async () => {
    api.createAgentRun.mockRejectedValue(
      Object.assign(new Error(""), { status: 422 }),
    );
    renderPage();
    await flush();
    await createRun();
    expect(screen.getByText("请求参数校验未通过")).toBeInTheDocument();
  });

  it("服务端返回 500 时展示带后端文案的操作失败提示", async () => {
    api.createAgentRun.mockRejectedValue(
      Object.assign(new Error("服务器内部错误"), { status: 500 }),
    );
    renderPage();
    await flush();
    await createRun();
    expect(screen.getByText("操作失败：服务器内部错误")).toBeInTheDocument();
  });

  it("拒绝值不是 Error 对象时按网络异常处理，不抛未捕获异常", async () => {
    api.createAgentRun.mockRejectedValue("socket hang up");
    renderPage();
    await flush();
    await createRun();
    expect(screen.getByText("网络异常，请稍后重试")).toBeInTheDocument();

    api.createAgentRun.mockRejectedValue({ detail: "weird" });
    await createRun();
    expect(screen.getAllByText("网络异常，请稍后重试")).toHaveLength(1);
  });

  it("编排配置列表加载失败时展示错误提示", async () => {
    api.listOrchestrations.mockRejectedValue(new Error("boom"));
    renderPage();
    await flush();
    expect(screen.getByText("编排配置列表加载失败")).toBeInTheDocument();
  });

  it("模型列表加载失败时展示错误提示", async () => {
    api.listModels.mockRejectedValue(new Error("boom"));
    renderPage();
    await flush();
    expect(screen.getByText("模型列表加载失败")).toBeInTheDocument();
  });

  it("预算耗尽但后端没给预算明细时提示无法匹配预算增量", async () => {
    api.getAgentRun.mockResolvedValue(
      makeRun({ status: "PAUSED", pauseCode: "BUDGET_EXHAUSTED", pauseDetail: null }),
    );
    renderPage();
    await flush();
    await createRun();
    await advance(RUN_POLL_MS);
    expect(
      screen.getByText("后端未返回预算明细，无法自动匹配预算增量"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /恢\s*复\s*运\s*行/ }));
    await flush();
    expect(api.resumeAgentRun).not.toHaveBeenCalled();
  });

  it("预算耗尽在 tool 上时提交的是追加工具调用次数", async () => {
    api.getAgentRun.mockResolvedValue(
      makeRun({
        status: "PAUSED",
        pauseCode: "BUDGET_EXHAUSTED",
        pauseDetail: { budget: "tool" },
      }),
    );
    renderPage();
    await flush();
    await createRun();
    await advance(RUN_POLL_MS);
    expect(screen.getByLabelText("追加工具调用次数")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "5" } });
    await flush();
    fireEvent.click(screen.getByRole("button", { name: /恢\s*复\s*运\s*行/ }));
    await flush();
    expect(api.resumeAgentRun).toHaveBeenCalledWith("run-1", {
      additionalToolCalls: 5,
    });
  });

  it("终态运行不再轮询，但仍然展示完整轨迹与最终结论", async () => {
    const step1 = makeStep({ stepNo: 1, kind: "TOOL", toolName: "supplier_risk" });
    const step2 = makeStep({ stepNo: 2, kind: "TOOL", toolName: "supplier_otsd" });
    api.getAgentRun.mockResolvedValue(
      makeRun({
        status: "SUCCEEDED",
        finalAnswer: "供应商 SUP000001 风险等级 high。",
      }),
    );
    api.getAgentRunSteps.mockResolvedValue([step1, step2]);

    renderPage();
    await flush();
    await createRun();
    await advance(RUN_POLL_MS);

    expect(screen.getAllByTestId(/agent-run-step-/)).toHaveLength(2);
    expect(screen.getByText("供应商 SUP000001 风险等级 high。")).toBeInTheDocument();
    const before = api.getAgentRun.mock.calls.length;
    await advance(RUN_POLL_MS * 2);
    expect(api.getAgentRun.mock.calls.length).toBe(before);
  });

  it("不调用 Chat API，也不调用旧版 runAgent", async () => {
    renderPage();
    await flush();
    await createRun();
    await advance(RUN_POLL_MS);
    expect(api.sendMessage).not.toHaveBeenCalled();
    expect(api.runAgent).not.toHaveBeenCalled();
  });

  it("创建前逐项校验：配置、模型、输入都必须给出", async () => {
    renderPage();
    await flush();
    fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*运\s*行/ }));
    await flush();
    expect(screen.getByText("请选择已启用的编排配置")).toBeInTheDocument();
    expect(api.createAgentRun).not.toHaveBeenCalled();

    await selectOption(0, "SUPPLIER_RISK_AGENT");
    fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*运\s*行/ }));
    await flush();
    expect(screen.getByText("请选择模型")).toBeInTheDocument();

    await selectOption(1, "gpt-4o-mini");
    fireEvent.click(screen.getByRole("button", { name: /创\s*建\s*运\s*行/ }));
    await flush();
    expect(screen.getByText("请输入任务内容")).toBeInTheDocument();
    expect(api.createAgentRun).not.toHaveBeenCalled();
  });

  it("创建失败时按状态码给出友好提示（422 展示后端诊断原文）", async () => {
    api.createAgentRun.mockRejectedValue(
      Object.assign(new Error("工具不存在：supplier_risk"), { status: 422 }),
    );
    renderPage();
    await flush();
    await createRun();
    expect(
      screen.getByText("请求被拒绝：工具不存在：supplier_risk"),
    ).toBeInTheDocument();
  });

  it("网络异常与权限不足各有独立提示", async () => {
    api.createAgentRun.mockRejectedValue(
      Object.assign(new Error("boom"), { status: 403 }),
    );
    renderPage();
    await flush();
    await createRun();
    expect(screen.getByText("没有权限操作该运行")).toBeInTheDocument();

    api.createAgentRun.mockRejectedValue(new Error("socket hang up"));
    await createRun();
    expect(screen.getByText("网络异常，请稍后重试")).toBeInTheDocument();
  });

  it("恢复失败时展示后端给出的原因（如预算增量缺失）", async () => {
    api.getAgentRun.mockResolvedValue(
      makeRun({
        status: "PAUSED",
        pauseCode: "BUDGET_EXHAUSTED",
        pauseDetail: { budget: "iteration" },
      }),
    );
    api.resumeAgentRun.mockRejectedValue(
      Object.assign(new Error("恢复运行需要提供对应的预算增量"), { status: 422 }),
    );
    renderPage();
    await flush();
    await createRun();
    await advance(RUN_POLL_MS);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "3" } });
    await flush();
    fireEvent.click(screen.getByRole("button", { name: /恢\s*复\s*运\s*行/ }));
    await flush();
    expect(
      screen.getByText("请求被拒绝：恢复运行需要提供对应的预算增量"),
    ).toBeInTheDocument();
  });

  it("恢复时缺少预算增量则不发起请求", async () => {
    api.getAgentRun.mockResolvedValue(
      makeRun({
        status: "PAUSED",
        pauseCode: "BUDGET_EXHAUSTED",
        pauseDetail: { budget: "iteration" },
      }),
    );
    renderPage();
    await flush();
    await createRun();
    await advance(RUN_POLL_MS);
    fireEvent.click(screen.getByRole("button", { name: /恢\s*复\s*运\s*行/ }));
    await flush();
    expect(screen.getByText("请先填写预算增量")).toBeInTheDocument();
    expect(api.resumeAgentRun).not.toHaveBeenCalled();
  });
});

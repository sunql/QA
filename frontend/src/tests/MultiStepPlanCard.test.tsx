/** 多步计划卡（D2 改版）：折叠头一行摘要 + 展开步骤时间线，**卡内不出图**。
 *
 * 图只出现在最终报告的对应分节（`ReportChartBlock`），计划卡重复出图被移除；
 * 摘要行回答「几步、成功几、失败几、耗时多少」，不用展开。
 *
 * 注意 antd `Collapse` 默认**不挂载**面板内容：不展开就什么都查不到（不是组件坏了）。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";

// ResumeRunButton 展开 Popover 时才渲染 model Select，会真实打 useLlmModels；
// 这里替掉，避免测试去打后端。
const useLlmModelsMock = vi.fn();
vi.mock("../hooks/useLlmModels", () => ({
  useLlmModels: () => useLlmModelsMock(),
}));

vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: React.forwardRef(function EChartsMock(
    props: { option?: Record<string, unknown> },
    ref: React.ForwardedRef<unknown>
  ) {
    React.useImperativeHandle(ref, () => ({
      getEchartsInstance: () => ({ getDataURL: () => "data:image/png;base64,x" }),
    }));
    return <div data-testid="echarts-mock">{JSON.stringify(props.option)}</div>;
  }),
}));

import MultiStepPlanCard from "../components/chat/MultiStepPlanCard";
import type { MultiStepStep } from "../types/chat";

const SUMMARY_TESTID = "multi-step-summary";

function renderExpanded(
  steps: MultiStepStep[],
  onResume?: (runId: string, fromStepIndex: number, modelId: number | null) => void,
  disabled?: boolean,
  originalModelId = 1,
  elapsedMs?: number
) {
  render(
    <MultiStepPlanCard
      steps={steps}
      onResume={onResume}
      disabled={disabled}
      originalModelId={originalModelId}
      elapsedMs={elapsedMs}
    />
  );
  fireEvent.click(screen.getByTestId(SUMMARY_TESTID));
}

/**
 * 点开续跑 Popover 并走到真正触发回调的那一步。
 *
 * `resume-run` 只负责**打开** Popover（antd Popover 的 trigger="click"），
 * 回调发生在 Popover 内容里的 `resume-with-selected` / `resume-with-original`
 * 上。此前两条用例只点了 `resume-run` 就断言回调，于是断言的是一个永远到不了
 * 的调用 —— 它们在 main 上一直是红的（"Number of calls: 0"），
 * 标题写着「点击回调带 runId 与步号」而实际没验到任何回调。
 */
async function openResumeAndClick(which: "selected" | "original"): Promise<void> {
  fireEvent.click(screen.getByTestId("resume-run"));
  await waitFor(() => screen.getByTestId(`resume-with-${which}`));
  fireEvent.click(screen.getByTestId(`resume-with-${which}`));
}

function makeStep(overrides: Partial<MultiStepStep> = {}): MultiStepStep {
  return {
    stepIndex: 0,
    description: "各供应商收货量",
    subQuestion: "各供应商的收货量是多少",
    aggregationOnly: false,
    status: "done",
    sql: "SELECT 1",
    summary: null,
    error: null,
    ...overrides,
  };
}

describe("MultiStepPlanCard 摘要行与卡内无图", () => {
  // 反向守卫：把 ChartRenderer 放回卡内 ⇒ 本用例红。
  it("卡内不再渲染每步图表（图只在最终报告对应分节出现）", () => {
    renderExpanded([
      makeStep({ stepIndex: 0, chartType: "hbar", chartOption: { series: [{ type: "bar", data: [9812] }] } }),
      makeStep({
        stepIndex: 1,
        chartType: "kpi",
        chartOption: { kpi: { label: "总收货量", value: 9812, unit: "件", delta: null } },
      }),
    ]);

    expect(screen.queryByTestId("echarts-mock")).toBeNull();
    expect(screen.queryByText("总收货量")).toBeNull();
  });

  it("只删了图，步骤明细仍在（SQL / 摘要 / 压缩行数）", () => {
    renderExpanded([
      makeStep({ stepIndex: 0, status: "done", summary: "共 3 家供应商" }),
      makeStep({ stepIndex: 1, status: "compressed", originalRows: 1000, compressedRows: 30 }),
    ]);

    // SQL 预览仍是折叠面板（内容不挂载），按入口数量断言两步各有一份
    expect(screen.getAllByText("查看 SQL")).toHaveLength(2);
    expect(screen.getByText("共 3 家供应商")).toBeInTheDocument();
    expect(screen.getByText("数据已压缩（1000 → 30 行）")).toBeInTheDocument();
  });

  it("结束态摘要：步数 · 成功 · 失败 · 耗时", () => {
    render(
      <MultiStepPlanCard
        steps={[
          makeStep({ stepIndex: 0, status: "done" }),
          makeStep({ stepIndex: 1, status: "compressed", originalRows: 100, compressedRows: 10 }),
          makeStep({ stepIndex: 2, status: "error", error: "ORA-00942" }),
        ]}
        originalModelId={1}
        elapsedMs={12345}
      />
    );

    // 压缩步算成功（数据被裁过，但那一步本身成功）；12345ms → 12.3秒
    expect(screen.getByTestId(SUMMARY_TESTID).textContent).toBe("3 步 · 成功 2 · 失败 1 · 12.3秒");
  });

  it("全部成功且无耗时：省略失败与耗时段（历史回放没有时长，不伪造）", () => {
    render(
      <MultiStepPlanCard
        steps={[makeStep({ stepIndex: 0, status: "done" }), makeStep({ stepIndex: 1, status: "compressed" })]}
        originalModelId={1}
      />
    );

    expect(screen.getByTestId(SUMMARY_TESTID).textContent).toBe("2 步 · 成功 2");
  });

  it("运行中摘要显示当前步进度", () => {
    render(
      <MultiStepPlanCard
        steps={[
          makeStep({ stepIndex: 0, status: "done" }),
          makeStep({ stepIndex: 1, status: "running" }),
          makeStep({ stepIndex: 2, status: "pending" }),
        ]}
        originalModelId={1}
        currentStepIndex={1}
      />
    );

    expect(screen.getByTestId(SUMMARY_TESTID).textContent).toBe("执行中 (2/3)");
  });

  it("尚未开始（计划刚下发、全部待执行）显示待执行，而不是「成功 0」", () => {
    render(
      <MultiStepPlanCard
        steps={[
          makeStep({ stepIndex: 0, status: "pending" }),
          makeStep({ stepIndex: 1, status: "pending" }),
          makeStep({ stepIndex: 2, status: "pending" }),
        ]}
        originalModelId={1}
      />
    );

    expect(screen.getByTestId(SUMMARY_TESTID).textContent).toBe("3 步 · 待执行");
  });
});

describe("MultiStepPlanCard 续跑与压缩徽章", () => {
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

  it("失败步骤带 runId 时渲染续跑按钮，回调带 runId 与步号", async () => {
    // Arrange
    const onResume = vi.fn();
    renderExpanded(
      [makeStep({ stepIndex: 1, status: "error", error: "oMLX timeout", runId: "r-1" })],
      onResume,
      undefined,
      2
    );

    // Act：点开 Popover → 「原模型续跑」
    await openResumeAndClick("original");

    // Assert
    expect(onResume).toHaveBeenCalledWith("r-1", 1, null);
  });

  // 锁住本卡片的透传职责：ResumeRunButton 选中的 modelId 必须原样交给 onResume。
  //
  // 注意这**不是** N4 失效的守卫点，真正断在 MessageItem（它的 onResume 回调只声明
  // 2 参形参，静默吞掉第 3 参）。本卡片在 :140 是把 prop 直传，运行时第 3 参本来就
  // 流得过去 —— 已实测：回退 MessageItem 的修复后本用例依旧全绿。守卫在
  // MessageItem.test.tsx「换模型续跑（N4 接缝）」那组。本用例的作用是钉住透传契约，
  // 防止将来有人在中间加一层包装时再丢一次。
  it("换模型续跑：Popover 选中的 modelId 必须透传到 onResume", async () => {
    const onResume = vi.fn();
    renderExpanded(
      [makeStep({ stepIndex: 2, status: "error", error: "oMLX timeout", runId: "r-1" })],
      onResume,
      undefined,
      2 // 原模型 = Qwen(2)；本用例要换成 deepseek(1)
    );

    fireEvent.click(screen.getByTestId("resume-run"));
    await waitFor(() => screen.getByTestId("resume-model-select"));
    // antd Select：mouseDown 打开下拉，点选项文本
    fireEvent.mouseDown(document.querySelector(".ant-select-selector") as HTMLElement);
    fireEvent.click(await screen.findByText(/^deepseek-chat/));
    fireEvent.click(screen.getByTestId("resume-with-selected"));

    expect(onResume).toHaveBeenCalledWith("r-1", 2, 1);
  });

  // 反向断言：取消不得触发回调（挡「默认就续跑」的过宽修法）。
  it("点「取消」不触发 onResume", async () => {
    const onResume = vi.fn();
    renderExpanded(
      [makeStep({ stepIndex: 1, status: "error", error: "boom", runId: "r-1" })],
      onResume
    );

    fireEvent.click(screen.getByTestId("resume-run"));
    await waitFor(() => screen.getByTestId("resume-cancel"));
    fireEvent.click(screen.getByTestId("resume-cancel"));

    expect(onResume).not.toHaveBeenCalled();
  });

  it("没有 runId 的失败步骤不渲染续跑按钮（单步路径不落库）", () => {
    const onResume = vi.fn();
    renderExpanded([makeStep({ status: "error", error: "boom" })], onResume);

    expect(screen.queryByTestId("resume-run")).toBeNull();
  });

  it("汇总步即使失败也不渲染续跑按钮（其步号恒越界，后端必拒）", () => {
    // 汇总步不落 multi_step_step 行，但它的 stepIndex = len(data_steps)，
    // 恰好等于 steps 行数 ⇒ 续跑请求必然落在 `start >= len(steps)` 的范围闸上，
    // 用户点下去只会拿到 409。故前端根本不渲染这个按钮（后端越界闸见
    // test_multi_step_resume_api.py::testResumeOutOfRangeFromStepIndexRejected）。
    // 反向自检：删掉 MultiStepPlanCard 里的 `!s.aggregationOnly` ⇒ 本用例红。
    const onResume = vi.fn();
    renderExpanded(
      [
        makeStep({ stepIndex: 1, status: "error", error: "boom", runId: "r-1", aggregationOnly: true }),
      ],
      onResume
    );

    expect(screen.queryByTestId("resume-run")).toBeNull();
  });

  it("普通失败步与汇总步并存时，只有普通步拿到续跑按钮", async () => {
    // 挡「把整个按钮删掉」这类过宽修法：普通步必须仍有入口。
    const onResume = vi.fn();
    renderExpanded(
      [
        makeStep({ stepIndex: 0, status: "error", error: "boom", runId: "r-1" }),
        makeStep({
          stepIndex: 1,
          status: "error",
          error: "总结失败",
          runId: "r-1",
          aggregationOnly: true,
        }),
      ],
      onResume
    );

    const buttons = screen.getAllByTestId("resume-run");
    expect(buttons).toHaveLength(1);
    await openResumeAndClick("original");
    expect(onResume).toHaveBeenCalledWith("r-1", 0, null);
  });

  it("压缩步骤显示压缩徽章与行数", () => {
    renderExpanded([
      makeStep({ status: "compressed", originalRows: 1000, compressedRows: 30 }),
    ]);

    expect(screen.getByText("已压缩")).toBeInTheDocument();
    expect(screen.getByText("数据已压缩（1000 → 30 行）")).toBeInTheDocument();
  });

  it("流式进行中（disabled）时续跑按钮禁用，防重复点击", () => {
    renderExpanded(
      [makeStep({ stepIndex: 1, status: "error", error: "boom", runId: "r-1" })],
      vi.fn(),
      true
    );

    expect(screen.getByTestId("resume-run")).toBeDisabled();
  });
});

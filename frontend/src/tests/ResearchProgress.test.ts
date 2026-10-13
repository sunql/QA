// ResearchProgress 的进度推导（`deriveProgressItems`）。
//
// 承重契约：`research.error` **只**在 `uiHint === "degraded"` 时进时间线 —— 终态
// （terminal）与冲突（conflict）各自已有自己的面（主区 Alert / toast），进时间线会
// 重复且误导（时间线语义是「还在推进」）。分支按 uiHint 的**类**，不按 code
// （仓库规则 ③：后端新增终态 code 前端零控制流改动）。
import { describe, it, expect } from "vitest";

import { zhCN } from "../i18n/zh-CN";
import { enUS } from "../i18n/en-US";
import { deriveProgressItems } from "../components/research/ResearchProgress";
import type { ResearchSseEvent } from "../types/research";

function errorEvent(payload: Record<string, unknown>): ResearchSseEvent {
  return { name: "research.error", payload };
}

describe("deriveProgressItems：中间帧映射", () => {
  it("白名单内的中间帧逐条映射，id 带下标（同事件多次出现不撞 key）", () => {
    const events: ResearchSseEvent[] = [
      { name: "research.intent", payload: {} },
      { name: "research.step.done", payload: { index: 0 } },
      { name: "research.step.done", payload: { index: 1 } },
    ];

    expect(deriveProgressItems(events)).toEqual([
      { id: "research.intent-0", labelKey: "research.progress.intent" },
      { id: "research.step.done-1", labelKey: "research.progress.stepDone" },
      { id: "research.step.done-2", labelKey: "research.progress.stepDone" },
    ]);
  });

  it("不修改入参（派生是纯函数）", () => {
    const events: ResearchSseEvent[] = [{ name: "research.intent", payload: {} }];
    deriveProgressItems(events);
    expect(events).toHaveLength(1);
  });
});

describe("deriveProgressItems：research.error 按 uiHint 分类", () => {
  it("uiHint=degraded 进时间线（用户唯一能看见预算闸门降级的地方）", () => {
    const events: ResearchSseEvent[] = [
      errorEvent({ code: "budget_metering_degraded", message: "计量降级", uiHint: "degraded" }),
    ];

    expect(deriveProgressItems(events)).toEqual([
      { id: "research.error-0", labelKey: "research.progress.degraded" },
    ]);
  });

  it("uiHint=terminal 不进时间线（主区 error Alert 已有面）", () => {
    const events: ResearchSseEvent[] = [
      errorEvent({ code: "turn_failed", message: "本轮失败", uiHint: "terminal" }),
    ];

    expect(deriveProgressItems(events)).toEqual([]);
  });

  it("uiHint=conflict 不进时间线（toast 已有面）", () => {
    const events: ResearchSseEvent[] = [
      errorEvent({ code: "checkpoint_conflict", message: "决策冲突", uiHint: "conflict" }),
    ];

    expect(deriveProgressItems(events)).toEqual([]);
  });

  it("uiHint 缺失/未知 ⇒ 不进时间线（fail-safe：认不出就不画）", () => {
    expect(deriveProgressItems([errorEvent({ code: "mystery" })])).toEqual([]);
    expect(deriveProgressItems([errorEvent({})])).toEqual([]);
  });

  it("degraded 与中间帧混排时保持原顺序", () => {
    const events: ResearchSseEvent[] = [
      { name: "research.intent", payload: {} },
      errorEvent({ code: "budget_metering_degraded", uiHint: "degraded" }),
      { name: "research.plan", payload: {} },
    ];

    expect(deriveProgressItems(events).map((item) => item.labelKey)).toEqual([
      "research.progress.intent",
      "research.progress.degraded",
      "research.progress.plan",
    ]);
  });
});

describe("research.progress.degraded 文案（双语齐备）", () => {
  it.each([
    ["zh-CN", zhCN],
    ["en-US", enUS],
  ])("%s 有 research.progress.degraded 且非空", (_locale, bundle) => {
    const value = (bundle as Record<string, unknown>).research as {
      progress?: Record<string, string>;
    };
    expect(value.progress?.degraded).toBeTruthy();
  });
});

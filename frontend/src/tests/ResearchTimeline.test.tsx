/** ResearchTimeline 单元测试（feat-research-entry Task 10，TDD RED→GREEN）。
 *
 * 契约：
 * - turns 按序渲染；user 轮取 content.question，checkpoint_awaiting 轮取
 *   content.prompt，agent 轮取 content.text/summary
 * - checkpoint_awaiting 轮按 checkpointId 关联 checkpoints 数组补状态 Tag
 * - checkpoints 中未被任何 turn 引用的「孤儿检查点」追加到末尾
 * - 每项锚点 turn-<id> / checkpoint-<id>，点跳转按钮回调 onJump(anchor)
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ConfigProvider } from "antd";
import { ResearchTimeline } from "../components/research/ResearchTimeline";
import type { ResearchTurn, ResearchCheckpoint } from "../types/research";

function userTurn(id: string, question: string): ResearchTurn {
  return {
    id,
    turnIndex: 0,
    role: "user",
    content: { question },
    createdAt: "2026-10-04T00:00:00Z",
  };
}

function checkpointTurn(id: string, checkpointId: string, prompt: string): ResearchTurn {
  return {
    id,
    turnIndex: 1,
    role: "checkpoint_awaiting",
    content: { checkpointId, phase: "intent", prompt, options: {} },
    createdAt: "2026-10-04T00:00:01Z",
  };
}

function agentTurn(id: string, text: string): ResearchTurn {
  return {
    id,
    turnIndex: 2,
    role: "agent",
    content: { text },
    createdAt: "2026-10-04T00:00:02Z",
  };
}

function checkpoint(id: string, prompt: string, status: ResearchCheckpoint["status"]): ResearchCheckpoint {
  return {
    id,
    phase: "intent",
    status,
    options: {},
    prompt,
    userChoice: null,
    decidedAt: null,
  };
}

describe("ResearchTimeline", () => {
  it("渲染 user 轮的问题文本", () => {
    render(
      <ConfigProvider>
        <ResearchTimeline turns={[userTurn("t1", "本月收货量趋势")]} checkpoints={[]} onJump={() => {}} />
      </ConfigProvider>
    );
    expect(screen.getByText("本月收货量趋势")).toBeInTheDocument();
  });

  it("渲染 agent 轮内容", () => {
    render(
      <ConfigProvider>
        <ResearchTimeline turns={[agentTurn("t1", "结论：整体上升")]} checkpoints={[]} onJump={() => {}} />
      </ConfigProvider>
    );
    expect(screen.getByText("结论：整体上升")).toBeInTheDocument();
  });

  it("checkpoint_awaiting 轮渲染 prompt 并按 checkpoint 状态补 Tag", () => {
    render(
      <ConfigProvider>
        <ResearchTimeline
          turns={[checkpointTurn("t1", "cp-1", "三臂是否齐全？")]}
          checkpoints={[checkpoint("cp-1", "三臂是否齐全？", "pending")]}
          onJump={() => {}}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("三臂是否齐全？")).toBeInTheDocument();
    expect(screen.getByText("待决策")).toBeInTheDocument();
  });

  it("孤儿检查点（无 turn 引用）追加到末尾", () => {
    render(
      <ConfigProvider>
        <ResearchTimeline
          turns={[userTurn("t1", "原始问题")]}
          checkpoints={[checkpoint("cp-9", "孤立的待决策点", "pending")]}
          onJump={() => {}}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("孤立的待决策点")).toBeInTheDocument();
  });

  it("点跳转按钮回调 onJump(anchor)", () => {
    const onJump = vi.fn();
    render(
      <ConfigProvider>
        <ResearchTimeline
          turns={[userTurn("t1", "原始问题")]}
          checkpoints={[]}
          onJump={onJump}
        />
      </ConfigProvider>
    );
    fireEvent.click(screen.getByTestId("jump-turn-t1"));
    expect(onJump).toHaveBeenCalledWith("turn-t1");
  });

  it("孤儿检查点跳转锚点为 checkpoint-<id>", () => {
    const onJump = vi.fn();
    render(
      <ConfigProvider>
        <ResearchTimeline
          turns={[]}
          checkpoints={[checkpoint("cp-9", "孤立的待决策点", "confirmed")]}
          onJump={onJump}
        />
      </ConfigProvider>
    );
    fireEvent.click(screen.getByTestId("jump-checkpoint-cp-9"));
    expect(onJump).toHaveBeenCalledWith("checkpoint-cp-9");
  });
});

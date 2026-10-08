/** ResearchTimeline 单元测试（feat-research-entry Task 10，详情按钮替换轮）。
 *
 * 契约（2026-10-08 重构）：
 * - turns 按序渲染；user 轮取 content.question，checkpoint_awaiting 轮取
 *   content.prompt，agent 轮取 content.text/summary
 * - checkpoint_awaiting 轮按 checkpointId 关联 checkpoints 数组补状态 Tag
 * - checkpoints 中未被任何 turn 引用的「孤儿检查点」追加到末尾
 * - 每项带「查看」按钮，点击回调 onDetail(payload)：
 *     { kind: "user", turn } | { kind: "checkpoint", turn, checkpoint } | { kind: "orphan", checkpoint }
 *   原 onJump(anchor) 行为（按钮 anchor 自指 = 永远 noop）已删除。
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
        <ResearchTimeline turns={[userTurn("t1", "本月收货量趋势")]} checkpoints={[]} onDetail={() => {}} />
      </ConfigProvider>
    );
    expect(screen.getByText("本月收货量趋势")).toBeInTheDocument();
  });

  it("渲染 agent 轮内容", () => {
    render(
      <ConfigProvider>
        <ResearchTimeline turns={[agentTurn("t1", "结论：整体上升")]} checkpoints={[]} onDetail={() => {}} />
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
          onDetail={() => {}}
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
          onDetail={() => {}}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("孤立的待决策点")).toBeInTheDocument();
  });

  it("点 user 轮查看按钮回调 onDetail({ kind: 'user', turn })", () => {
    const onDetail = vi.fn();
    const turn = userTurn("t1", "原始问题");
    render(
      <ConfigProvider>
        <ResearchTimeline turns={[turn]} checkpoints={[]} onDetail={onDetail} />
      </ConfigProvider>
    );
    fireEvent.click(screen.getByTestId("detail-turn-t1"));
    expect(onDetail).toHaveBeenCalledWith({ kind: "user", turn });
  });

  it("点 checkpoint_awaiting 轮查看按钮回调 onDetail({ kind: 'checkpoint', turn, checkpoint })", () => {
    const onDetail = vi.fn();
    const turn = checkpointTurn("t1", "cp-1", "三臂是否齐全？");
    const cp = checkpoint("cp-1", "三臂是否齐全？", "pending");
    render(
      <ConfigProvider>
        <ResearchTimeline turns={[turn]} checkpoints={[cp]} onDetail={onDetail} />
      </ConfigProvider>
    );
    fireEvent.click(screen.getByTestId("detail-turn-t1"));
    expect(onDetail).toHaveBeenCalledWith({ kind: "checkpoint", turn, checkpoint: cp });
  });

  it("历史 checkpoint_awaiting 轮（未在 checkpoints 列表）也必须能触发 onDetail：从 turn.content 合成 checkpoint", () => {
    // 2026-10-08 补：页面只把 pendingCheckpoint 传给 timeline，历史已决策的 checkpoint
    // 不会出现在 checkpoints 列表里。但用户点「查看」必须有反应 —— 用 turn.content
    // 合成一个 ResearchCheckpoint（status=confirmed 标记已决策）。
    const onDetail = vi.fn();
    const turn: ResearchTurn = {
      id: "t1",
      turnIndex: 1,
      role: "checkpoint_awaiting",
      content: {
        checkpointId: "cp-1",
        phase: "hypothesis",
        prompt: "共 3 条候选假设",
        options: { candidates: [{ statement: "假设 A" }] },
      },
      createdAt: "2026-10-04T00:00:01Z",
    };
    // checkpoints 列表为空 —— 模拟「历史已决策 checkpoint 不在 page state」
    render(
      <ConfigProvider>
        <ResearchTimeline turns={[turn]} checkpoints={[]} onDetail={onDetail} />
      </ConfigProvider>
    );
    fireEvent.click(screen.getByTestId("detail-turn-t1"));
    expect(onDetail).toHaveBeenCalledTimes(1);
    const [payload] = onDetail.mock.calls[0];
    expect(payload.kind).toBe("checkpoint");
    // 合成出来的 checkpoint 必须能从 turn.content 拿到 prompt + options
    expect(payload.checkpoint.prompt).toBe("共 3 条候选假设");
    expect(payload.checkpoint.options).toEqual({ candidates: [{ statement: "假设 A" }] });
    expect(payload.checkpoint.phase).toBe("hypothesis");
  });

  it("点孤儿检查点查看按钮回调 onDetail({ kind: 'orphan', checkpoint })", () => {
    const onDetail = vi.fn();
    const cp = checkpoint("cp-9", "孤立的待决策点", "confirmed");
    render(
      <ConfigProvider>
        <ResearchTimeline turns={[]} checkpoints={[cp]} onDetail={onDetail} />
      </ConfigProvider>
    );
    fireEvent.click(screen.getByTestId("detail-checkpoint-cp-9"));
    expect(onDetail).toHaveBeenCalledWith({ kind: "orphan", checkpoint: cp });
  });
});

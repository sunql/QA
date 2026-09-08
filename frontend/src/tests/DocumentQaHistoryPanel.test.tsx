import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DocumentQaHistoryPanel } from "../components/documents/DocumentQaHistoryPanel";

interface SessionSummary {
  sessionId: string;
  title?: string;
  updatedAt?: string;
}

function makeSession(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    sessionId: "sess-default-1234567890",
    ...overrides,
  };
}

describe("DocumentQaHistoryPanel", () => {
  it("renders sessions with title fallback to sessionId slice", () => {
    const sessions = [
      makeSession({ sessionId: "abc123def4567890", title: "质量协议问答" }),
      makeSession({ sessionId: "long-session-id-without-title" }),
    ];
    render(
      <DocumentQaHistoryPanel
        sessions={sessions}
        currentSessionId={null}
        loading={false}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />
    );
    expect(screen.getByText("质量协议问答")).toBeInTheDocument();
    // 无 title 时显示 sessionId.slice(0, 16)
    expect(screen.getByText("long-session-id-")).toBeInTheDocument();
  });

  it("highlights current session with primary tint background", () => {
    const sessions = [
      makeSession({ sessionId: "active-session-id", title: "当前会话" }),
      makeSession({ sessionId: "other-session-id", title: "其他会话" }),
    ];
    const { container } = render(
      <DocumentQaHistoryPanel
        sessions={sessions}
        currentSessionId="active-session-id"
        loading={false}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />
    );
    const items = container.querySelectorAll(".ant-list-item");
    expect(items.length).toBe(2);
    const activeStyle = (items[0] as HTMLElement).getAttribute("style") ?? "";
    const otherStyle = (items[1] as HTMLElement).getAttribute("style") ?? "";
    // antd primary tint 背景色 #e6f7ff 在 React 渲染时会被转为 rgb(230, 247, 255)
    expect(activeStyle).toContain("rgb(230, 247, 255)");
    // 未选中项不应包含 primary tint 背景
    expect(otherStyle).not.toContain("rgb(230, 247, 255)");
  });

  it("clicking a session triggers onSelect with the sessionId", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <DocumentQaHistoryPanel
        sessions={[makeSession({ sessionId: "sess-target", title: "目标会话" })]}
        currentSessionId={null}
        loading={false}
        onSelect={onSelect}
        onNew={vi.fn()}
      />
    );
    await user.click(screen.getByText("目标会话"));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("sess-target");
  });

  it("clicking 新对话 button triggers onNew", async () => {
    const user = userEvent.setup();
    const onNew = vi.fn();
    render(
      <DocumentQaHistoryPanel
        sessions={[]}
        currentSessionId={null}
        loading={false}
        onSelect={vi.fn()}
        onNew={onNew}
      />
    );
    await user.click(screen.getByRole("button", { name: /新对话/ }));
    expect(onNew).toHaveBeenCalledTimes(1);
  });
});
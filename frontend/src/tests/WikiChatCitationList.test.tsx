// WikiChatCitationList 组件测试（feat-wiki-chat UI 优化）：
// 引用默认收起只显示标题，点击展开正文、再点收起。

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WikiChatCitationList } from "../components/wikiChat/WikiChatCitationList";
import type { WikiCitation } from "../types/wikiChat";

const CITE: WikiCitation = {
  id: 1,
  pageId: "PAGE-A",
  title: "5.1采购与供应商控制流程",
  dimension: "RULE",
  chunkText: "注册资本一千万以上方可准入。",
  score: 0.55,
};

describe("WikiChatCitationList", () => {
  it("renders header (id + title + score) with chunk text collapsed by default", async () => {
    const user = userEvent.setup();
    const { container } = render(<WikiChatCitationList citations={[CITE]} />);

    // 标题与分数可见
    expect(screen.getByText("5.1采购与供应商控制流程")).toBeInTheDocument();
    expect(screen.getByText("55.0%")).toBeInTheDocument();

    const activePanels = () =>
      container.querySelectorAll(".ant-collapse-item-active").length;
    // 默认全部收起
    expect(activePanels()).toBe(0);

    // 点击展开 → 激活面板出现；再点收起 → 回到收起
    await user.click(screen.getByTestId("wiki-chat-citation-header-1"));
    expect(activePanels()).toBe(1);
    expect(screen.getByText(/注册资本一千万以上方可准入/)).toBeInTheDocument();

    await user.click(screen.getByTestId("wiki-chat-citation-header-1"));
    expect(activePanels()).toBe(0);
  });

  it("renders nothing for empty citations", () => {
    const { container } = render(<WikiChatCitationList citations={[]} />);
    expect(container.querySelector(".wiki-chat-citation-list")).toBeNull();
  });
});

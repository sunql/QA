/**
 * WikiLinksPage 骨架测试。
 *
 * 验证：渲染标题 + 空状态提示（未选中页面时）。
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { WikiLinksPage } from "../WikiLinksPage";

describe("WikiLinksPage", () => {
  it("renders title and empty state", () => {
    render(<WikiLinksPage />);
    expect(
      screen.getByText("Wiki ↔ Ontology 链接管理"),
    ).toBeInTheDocument();
    expect(screen.getByText("请先选择左侧 Wiki 页面")).toBeInTheDocument();
  });

  it("renders class and property tabs", () => {
    render(<WikiLinksPage />);
    expect(screen.getByText("Class")).toBeInTheDocument();
    expect(screen.getByText("Property")).toBeInTheDocument();
  });
});

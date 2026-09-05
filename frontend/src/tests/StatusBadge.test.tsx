/** StatusBadge 单元测试（Phase C RED）。
 *
 * 覆盖：
 * - 渲染默认（offline）样式
 * - status=success → 绿色徽章（带对勾 icon）
 * - status=warning → 橙色徽章（带 pause icon）
 * - status=error → 红色徽章
 * - status=offline → 灰色徽章
 * - 未知 status → 回退到 offline（健壮性）
 * - children 透传
 * - icon 缺省时不渲染
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import StatusBadge from "../components/common/StatusBadge";

describe("StatusBadge", () => {
  it("渲染默认 children 文本", () => {
    render(<StatusBadge status="success">生产中</StatusBadge>);
    expect(screen.getByText("生产中")).toBeInTheDocument();
  });

  it("success 状态使用 .statusBadgeSuccess 类（CSS Modules 驼峰化）", () => {
    const { container } = render(<StatusBadge status="success">OK</StatusBadge>);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain("statusBadge");
    expect(badge.className).toContain("statusBadgeSuccess");
  });

  it("warning 状态使用 .statusBadgeWarning 类", () => {
    const { container } = render(<StatusBadge status="warning">停机</StatusBadge>);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain("statusBadgeWarning");
  });

  it("error 状态使用 .statusBadgeError 类", () => {
    const { container } = render(<StatusBadge status="error">断网</StatusBadge>);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain("statusBadgeError");
  });

  it("offline 状态使用 .statusBadgeOffline 类", () => {
    const { container } = render(<StatusBadge status="offline">关机</StatusBadge>);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain("statusBadgeOffline");
  });

  it("未知 status 回退到 offline 样式", () => {
    const { container } = render(
      // @ts-expect-error 故意测试未知 status
      <StatusBadge status="unknown">未知</StatusBadge>,
    );
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain("statusBadgeOffline");
  });

  it("success 状态默认渲染对勾图标", () => {
    const { container } = render(<StatusBadge status="success">生产中</StatusBadge>);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
  });

  it("icon=false 时不渲染图标", () => {
    const { container } = render(
      <StatusBadge status="success" icon={false}>生产中</StatusBadge>,
    );
    const svg = container.querySelector("svg");
    expect(svg).toBeNull();
  });

  it("支持自定义 className 追加", () => {
    const { container } = render(
      <StatusBadge status="success" className="custom-class">OK</StatusBadge>,
    );
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain("custom-class");
    expect(badge.className).toContain("statusBadgeSuccess");
  });
});
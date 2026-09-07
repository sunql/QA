/** MetricCard 单元测试（Phase C RED）。
 *
 * 覆盖：
 * - 渲染 value（数字）+ label + unit（可选）
 * - status=success 时顶部渐变光带类为 metricCardSuccess
 * - 默认 status=success（中性青色）
 * - 数字格式化：toLocaleString 启用时按千分位
 * - icon 缺省时不渲染图标区
 * - className 追加
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import MetricCard from "../components/common/MetricCard";

describe("MetricCard", () => {
  it("渲染 value / label / unit", () => {
    render(<MetricCard value={4506} label="总冲次数" unit="万冲" />);
    expect(screen.getByText("总冲次数")).toBeInTheDocument();
    expect(screen.getByText("4,506")).toBeInTheDocument();
    expect(screen.getByText("万冲")).toBeInTheDocument();
  });

  it("默认 status=success，顶部光带使用 metricCardSuccess 类", () => {
    const { container } = render(<MetricCard value={100} label="在线设备" />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain("metricCard");
    expect(card.className).toContain("metricCardSuccess");
  });

  it("status=warning 时使用 metricCardWarning 类", () => {
    const { container } = render(
      <MetricCard value={3} label="停机" status="warning" />,
    );
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain("metricCardWarning");
  });

  it("status=error 时使用 metricCardError 类", () => {
    const { container } = render(
      <MetricCard value={1} label="断网" status="error" />,
    );
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain("metricCardError");
  });

  it("status=offline 时使用 metricCardOffline 类", () => {
    const { container } = render(
      <MetricCard value={0} label="关机" status="offline" />,
    );
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain("metricCardOffline");
  });

  it("format=false 时不格式化数字（按原始字符串渲染）", () => {
    render(<MetricCard value={4506} label="测试" format={false} />);
    // 没有千分位分隔符
    expect(screen.getByText("4506")).toBeInTheDocument();
  });

  it("icon prop 提供时渲染图标节点", () => {
    const { container } = render(
      <MetricCard value={1} label="x" icon={<span data-testid="my-icon">★</span>} />,
    );
    expect(container.querySelector("[data-testid='my-icon']")).not.toBeNull();
  });

  it("未传 icon 时不渲染图标区", () => {
    const { container } = render(<MetricCard value={1} label="x" />);
    // 图标区应不存在
    expect(container.querySelector("[data-testid='metric-card-icon']")).toBeNull();
  });

  it("className 追加到根节点", () => {
    const { container } = render(
      <MetricCard value={1} label="x" className="extra" />,
    );
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain("extra");
    expect(card.className).toContain("metricCard");
  });
});
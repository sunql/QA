/** 通用 echarts 包装的主题注入（DQ 报表图表走这条路）。
 *
 * 契约：**主题只补色，业务显式色优先** —— 调用方写在 series 上的 `itemStyle.color`
 * （如「通过/失败」语义色）不能被主题的系列色板覆盖，否则红绿会变随机色。
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: React.forwardRef(function EChartsMock(
    props: { option?: Record<string, unknown> },
    ref: React.ForwardedRef<unknown>
  ) {
    React.useImperativeHandle(ref, () => ({ getEchartsInstance: () => ({}) }));
    return <div data-testid="echarts-mock">{JSON.stringify(props.option)}</div>;
  }),
}));

import EChart from "../components/EChart";
import { LIGHT_TOKEN } from "../theme/tokens";

describe("EChart 主题注入", () => {
  it("注入主题调色板与文字色", () => {
    render(
      <EChart
        testId="dq-chart"
        option={{ xAxis: { type: "category" }, yAxis: { type: "value" }, series: [{ type: "bar", data: [1] }] }}
      />
    );

    const passed = JSON.parse(screen.getByTestId("echarts-mock").textContent ?? "{}") as {
      color: string[];
      textStyle: { color: string };
    };
    expect(passed.color).toEqual(LIGHT_TOKEN.chartPalette);
    expect(passed.textStyle.color).toBe(LIGHT_TOKEN.colorText);
  });

  it("调用方显式写的 itemStyle.color 不被主题覆盖（语义色优先）", () => {
    render(
      <EChart
        option={{ series: [{ type: "pie", itemStyle: { color: "#52c41a" }, data: [{ name: "通过", value: 1 }] }] }}
      />
    );

    const passed = JSON.parse(screen.getByTestId("echarts-mock").textContent ?? "{}") as {
      series: { itemStyle: { color: string } }[];
    };
    expect(passed.series[0].itemStyle.color).toBe("#52c41a");
  });

  it("数据与结构不被改动", () => {
    render(<EChart option={{ series: [{ type: "bar", data: [1, 2, 3] }] }} />);

    const passed = JSON.parse(screen.getByTestId("echarts-mock").textContent ?? "{}") as {
      series: unknown;
    };
    expect(passed.series).toEqual([{ type: "bar", data: [1, 2, 3] }]);
  });
});

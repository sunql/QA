/** 推荐问题 chips 组件测试（**与场景无关**）。
 *
 * chips 来自 copilot_scenario.chips（DB 配置）：任何配了 chips 的场景都要
 * 在工作区显示推荐问题 ⇒ 组件是通用件，不认识 scenarioCode。
 * 2026-11-26：原先 chips 只在「待到货」欢迎态里渲染，到货跟踪等场景的
 * chips 成了死配置（配置在、UI 不显示）。
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { describe, expect, it, vi } from "vitest";
import i18n from "../i18n/i18n";
import CopilotSuggestedQuestions from "../components/copilot/CopilotSuggestedQuestions";
import type { CopilotScenarioChip } from "../api/copilotScenario";

const CHIPS: CopilotScenarioChip[] = [
  { label: "近 12 个月到货概览", monthsBack: 12 },
  { label: "本期到货概览（24 日周期）", anchorDay: 24, periodIndex: 0 },
];

interface RenderOptions {
  chips?: CopilotScenarioChip[];
  disabled?: boolean;
  onChipClick?: (chip: CopilotScenarioChip) => void;
}

function renderSuggestedQuestions(options: RenderOptions = {}) {
  const onChipClick = options.onChipClick ?? vi.fn();
  render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <CopilotSuggestedQuestions
          chips={options.chips ?? CHIPS}
          disabled={options.disabled ?? false}
          onChipClick={onChipClick}
        />
      </ConfigProvider>
    </I18nextProvider>,
  );
  return { onChipClick };
}

describe("CopilotSuggestedQuestions", () => {
  it("渲染全部推荐问题条目", () => {
    renderSuggestedQuestions();
    expect(screen.getByText("近 12 个月到货概览")).toBeTruthy();
    expect(screen.getByText("本期到货概览（24 日周期）")).toBeTruthy();
  });

  it("点击 chip 回调带上该 chip（含槽位预设）", () => {
    const onChipClick = vi.fn();
    renderSuggestedQuestions({ onChipClick });
    fireEvent.click(screen.getByText("本期到货概览（24 日周期）"));
    expect(onChipClick).toHaveBeenCalledTimes(1);
    expect(onChipClick).toHaveBeenCalledWith(CHIPS[1]);
  });

  it("disabled 时点击不触发回调", () => {
    const onChipClick = vi.fn();
    renderSuggestedQuestions({ onChipClick, disabled: true });
    const button = screen.getByText("近 12 个月到货概览").closest("button");
    expect(button).toBeDisabled();
    fireEvent.click(button as HTMLElement);
    expect(onChipClick).not.toHaveBeenCalled();
  });

  it("chips 为空时不渲染推荐问题", () => {
    renderSuggestedQuestions({ chips: [] });
    expect(screen.queryByText("试试这样问我")).toBeNull();
  });
});

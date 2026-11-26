/** 推荐问题 chips（场景配置驱动，**与场景无关**）。
 *
 * chips 来自 copilot_scenario.chips（加场景 = 插一行配置，前端零改动）：
 * 任何配了 chips 的场景在工作区（还没有轮次时）都渲染本组件。
 *
 * 2026-11-26：从 PendingDeliveryWelcome 拆出 —— 原先 chips 只在待到货
 * 欢迎态里渲染 ⇒ 到货跟踪等场景的 chips 是死配置（配置在、UI 不显示）。
 */

import { Button, Card, Space } from "antd";
import { ArrowRightOutlined } from "@ant-design/icons";
import { useTranslation } from "../../i18n";
import type { CopilotScenarioChip } from "../../api/copilotScenario";

interface CopilotSuggestedQuestionsProps {
  chips: CopilotScenarioChip[];
  disabled: boolean;
  onChipClick: (chip: CopilotScenarioChip) => void;
}

export default function CopilotSuggestedQuestions({
  chips,
  disabled,
  onChipClick,
}: CopilotSuggestedQuestionsProps) {
  const { t } = useTranslation();

  if (chips.length === 0) return null;

  return (
    <Card size="small" title={t("procurement.copilot.tryAsk")} style={{ marginTop: 16 }}>
      <Space direction="vertical" size={6} style={{ width: "100%" }}>
        {chips.map((chip) => (
          <Button
            key={chip.label}
            block
            style={{ textAlign: "left", display: "flex", justifyContent: "space-between", alignItems: "center" }}
            disabled={disabled}
            onClick={() => onChipClick(chip)}
          >
            <span>{chip.label}</span>
            <ArrowRightOutlined />
          </Button>
        ))}
      </Space>
    </Card>
  );
}

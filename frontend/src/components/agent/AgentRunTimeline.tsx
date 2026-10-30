import { useState } from "react";
import { Space, Tag, Typography } from "antd";
import { useTranslation } from "../../i18n";
import type {
  AgentRunStepRead,
  CostSource,
} from "../../types/agentOrchestration";
import AgentRunStepRow from "./AgentRunStepRow";

export interface AgentRunTimelineProps {
  /** 已按 stepNo 去重排序的运行轨迹步骤。 */
  steps: readonly AgentRunStepRead[];
  /** 运行级最终结论（PLAN 的 FINISH 动作也会写在自己的 answer 字段里）。 */
  finalAnswer?: string | null;
  /** 成本来源口径；UNKNOWN 且 costComplete=false 表示成本从未被测量。 */
  costSource: CostSource;
  /** 成本是否完整可信。 */
  costComplete: boolean;
}

/** 成本来源对应的标签颜色。 */
const COST_COLOR: Record<CostSource, string> = {
  MEASURED: "green",
  ESTIMATED: "blue",
  UNKNOWN: "default",
  MIXED: "gold",
};

/** 成本徽标：从未测量时必须显式说明不完整，不能当成已测总量。 */
function CostBadge({
  costSource,
  costComplete,
}: {
  costSource: CostSource;
  costComplete: boolean;
}) {
  const { t } = useTranslation();
  const text =
    !costComplete && costSource === "UNKNOWN"
      ? t("agentTask.timeline.costIncomplete")
      : t("agentTask.timeline.cost." + costSource);
  return (
    <div>
      <Tag color={COST_COLOR[costSource]}>{text}</Tag>
    </div>
  );
}

/** 最终结论。 */
function FinalAnswerBlock({ finalAnswer }: { finalAnswer?: string | null }) {
  const { t } = useTranslation();
  if (!finalAnswer) return null;
  return (
    <div>
      <strong>{t("agentTask.timeline.finalAnswer")}</strong>
      <Typography.Paragraph style={{ whiteSpace: "pre-wrap" }}>
        {finalAnswer}
      </Typography.Paragraph>
    </div>
  );
}

/**
 * 运行轨迹：把一条运行的步骤按时间顺序渲染出来。
 *
 * 关键约定：
 * - PLAN 与 TOOL 是两条独立步骤行（一次 CALL_TOOL 规划会产出两行），都要渲染。
 * - 工具输出默认折叠，点击才展开；截断标记来自 outputJson.truncated。
 * - 失败步骤的错误文案来自 errorMessage，而不是解析 outputJson.text。
 * - 成本徽标在「从未测量」时必须显式说明不完整，不能当成已测总量。
 */
export default function AgentRunTimeline({
  steps,
  finalAnswer,
  costSource,
  costComplete,
}: AgentRunTimelineProps) {
  const { t } = useTranslation();
  const [expandedStepNos, setExpandedStepNos] = useState<number[]>([]);

  const toggleStep = (stepNo: number): void => {
    setExpandedStepNos((previous) =>
      previous.includes(stepNo)
        ? previous.filter((value) => value !== stepNo)
        : [...previous, stepNo],
    );
  };

  const ordered = [...steps].sort((left, right) => left.stepNo - right.stepNo);

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <CostBadge costSource={costSource} costComplete={costComplete} />
      {ordered.length === 0 ? (
        <Typography.Text type="secondary">
          {t("agentTask.empty.steps")}
        </Typography.Text>
      ) : (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          {ordered.map((step) => (
            <AgentRunStepRow
              key={step.stepNo}
              step={step}
              isExpanded={expandedStepNos.includes(step.stepNo)}
              onToggle={toggleStep}
            />
          ))}
        </Space>
      )}
      <FinalAnswerBlock finalAnswer={finalAnswer} />
    </Space>
  );
}

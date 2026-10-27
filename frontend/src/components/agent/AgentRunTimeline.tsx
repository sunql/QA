import { useState } from "react";
import { Alert, Button, Space, Tag, Typography } from "antd";
import { useTranslation } from "../../i18n";
import type {
  AgentRunStepRead,
  CostSource,
  JsonObject,
  StepStatus,
} from "../../types/agentOrchestration";

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

/** 工具观测：后端 serializeObservation 只写 {text, truncated, original_chars}。 */
interface Observation {
  text: string;
  truncated: boolean;
  originalChars: number;
}

/** 从 outputJson 里读出观测内容；截断标记也在这里，不在单独的列上。 */
function readObservation(
  output: JsonObject | null | undefined,
): Observation | null {
  if (!output) return null;
  const text = typeof output.text === "string" ? output.text : "";
  const truncated = output.truncated === true;
  const originalChars =
    typeof output.original_chars === "number" ? output.original_chars : 0;
  if (!text && !truncated) return null;
  return { text, truncated, originalChars };
}

/** JSON 以转义文本渲染（React 自动转义），绝不使用 dangerouslySetInnerHTML。 */
function renderJson(value: JsonObject): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

const STEP_STATUS_COLOR: Record<StepStatus, string> = {
  RUNNING: "processing",
  SUCCEEDED: "success",
  FAILED: "error",
  SKIPPED: "default",
};

const COST_COLOR: Record<CostSource, string> = {
  MEASURED: "green",
  ESTIMATED: "blue",
  UNKNOWN: "default",
  MIXED: "gold",
};

interface RunStepRowProps {
  step: AgentRunStepRead;
  isExpanded: boolean;
  onToggle: (stepNo: number) => void;
}

function RunStepRow({ step, isExpanded, onToggle }: RunStepRowProps) {
  const { t } = useTranslation();
  const observation = readObservation(step.outputJson);
  const isPlan = step.kind === "PLAN";
  const action = step.action ?? null;

  return (
    <div
      data-testid={`agent-run-step-${step.stepNo}`}
      style={{ borderLeft: "2px solid #f0f0f0", paddingLeft: 12 }}
    >
      <Space wrap size="small">
        <Tag>{isPlan ? t("agentTask.timeline.planLabel") : t("agentTask.timeline.toolLabel")}</Tag>
        <Tag color={STEP_STATUS_COLOR[step.status]}>
          {t("agentTask.timeline.status." + step.status)}
        </Tag>
        <span>{t("agentTask.timeline.stepNo", { no: step.stepNo })}</span>
        <span>{t("agentTask.timeline.iterationNo", { no: step.iterationNo })}</span>
      </Space>

      {action ? (
        <div style={{ marginTop: 4 }}>
          {t("agentTask.timeline.action." + action)}
        </div>
      ) : null}

      {step.planSummary ? (
        <div style={{ marginTop: 4 }}>
          <strong>{t("agentTask.timeline.planSummary")}</strong>
          <div>{step.planSummary}</div>
        </div>
      ) : null}

      {step.toolName ? (
        <div style={{ marginTop: 4 }}>
          <strong>{t("agentTask.timeline.toolName")}</strong>
          <div>{step.toolName}</div>
        </div>
      ) : null}

      {step.inputJson ? (
        <div style={{ marginTop: 4 }}>
          <strong>{t("agentTask.timeline.toolArgs")}</strong>
          <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
            {renderJson(step.inputJson)}
          </pre>
        </div>
      ) : null}

      {step.answer ? (
        <div style={{ marginTop: 4 }}>
          <strong>
            {action === "ASK_USER"
              ? t("agentTask.timeline.question")
              : t("agentTask.timeline.planAnswer")}
          </strong>
          <div>{step.answer}</div>
        </div>
      ) : null}

      {observation ? (
        <div style={{ marginTop: 4 }}>
          <Space direction="vertical" size="small">
            <Button
              size="small"
              onClick={() => onToggle(step.stepNo)}
              aria-label={
                isExpanded
                  ? t("agentTask.timeline.hideOutput")
                  : t("agentTask.timeline.showOutput")
              }
            >
              {isExpanded
                ? t("agentTask.timeline.hideOutput")
                : t("agentTask.timeline.showOutput")}
            </Button>
            {observation.truncated ? (
              <Tag color="orange">
                {t("agentTask.timeline.truncated", {
                  chars: observation.originalChars,
                })}
              </Tag>
            ) : null}
          </Space>
          {isExpanded ? (
            <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
              {observation.text}
            </pre>
          ) : null}
        </div>
      ) : null}

      {step.status === "FAILED" && step.errorMessage ? (
        <div style={{ marginTop: 4 }}>
          <Alert
            type="error"
            showIcon
            message={t("agentTask.timeline.stepError", {
              message: step.errorMessage,
            })}
          />
        </div>
      ) : null}
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
  const costText =
    !costComplete && costSource === "UNKNOWN"
      ? t("agentTask.timeline.costIncomplete")
      : t("agentTask.timeline.cost." + costSource);

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Tag color={COST_COLOR[costSource]}>{costText}</Tag>
      </div>

      {ordered.length === 0 ? (
        <Typography.Text type="secondary">
          {t("agentTask.empty.steps")}
        </Typography.Text>
      ) : (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          {ordered.map((step) => (
            <RunStepRow
              key={step.stepNo}
              step={step}
              isExpanded={expandedStepNos.includes(step.stepNo)}
              onToggle={toggleStep}
            />
          ))}
        </Space>
      )}

      {finalAnswer ? (
        <div>
          <strong>{t("agentTask.timeline.finalAnswer")}</strong>
          <Typography.Paragraph style={{ whiteSpace: "pre-wrap" }}>
            {finalAnswer}
          </Typography.Paragraph>
        </div>
      ) : null}
    </Space>
  );
}

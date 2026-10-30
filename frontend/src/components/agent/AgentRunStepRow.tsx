import { Alert, Space, Tag } from "antd";
import { useTranslation } from "../../i18n";
import type {
  AgentRunStepRead,
  StepStatus,
} from "../../types/agentOrchestration";
import AgentStepObservation, { readObservation, renderJson } from "./AgentStepObservation";

export interface AgentRunStepRowProps {
  step: AgentRunStepRead;
  isExpanded: boolean;
  onToggle: (stepNo: number) => void;
}

/** 步骤状态对应的标签颜色。 */
const STEP_STATUS_COLOR: Record<StepStatus, string> = {
  RUNNING: "processing",
  SUCCEEDED: "success",
  FAILED: "error",
  SKIPPED: "default",
};

/** 步骤头：类型、状态、步骤号、轮次。 */
function StepHeader({ step }: { step: AgentRunStepRead }) {
  const { t } = useTranslation();
  const isPlan = step.kind === "PLAN";
  return (
    <Space wrap size="small">
      <Tag>
        {isPlan
          ? t("agentTask.timeline.planLabel")
          : t("agentTask.timeline.toolLabel")}
      </Tag>
      <Tag color={STEP_STATUS_COLOR[step.status]}>
        {t("agentTask.timeline.status." + step.status)}
      </Tag>
      <span>{t("agentTask.timeline.stepNo", { no: step.stepNo })}</span>
      <span>{t("agentTask.timeline.iterationNo", { no: step.iterationNo })}</span>
    </Space>
  );
}

/** 规划动作、规划说明、工具名称与调用参数。 */
function StepDetails({ step }: { step: AgentRunStepRead }) {
  const { t } = useTranslation();
  const action = step.action ?? null;
  return (
    <>
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
    </>
  );
}

/** 规划结论 / Agent 提问。 */
function StepAnswer({ step }: { step: AgentRunStepRead }) {
  const { t } = useTranslation();
  if (!step.answer) return null;
  const label =
    step.action === "ASK_USER"
      ? t("agentTask.timeline.question")
      : t("agentTask.timeline.planAnswer");
  return (
    <div style={{ marginTop: 4 }}>
      <strong>{label}</strong>
      <div>{step.answer}</div>
    </div>
  );
}

/** 步骤级失败原因：来自后端的 errorMessage，而不是解析 outputJson.text。 */
function StepError({ step }: { step: AgentRunStepRead }) {
  const { t } = useTranslation();
  if (step.status !== "FAILED" || !step.errorMessage) return null;
  return (
    <div style={{ marginTop: 4 }}>
      <Alert
        type="error"
        showIcon
        message={t("agentTask.timeline.stepError", {
          message: step.errorMessage,
        })}
      />
    </div>
  );
}

/** 单条步骤：头 + 详情 + 结论 + 观测 + 失败原因。 */
export default function AgentRunStepRow(props: AgentRunStepRowProps) {
  const { step, isExpanded, onToggle } = props;
  const observation = readObservation(step.outputJson);
  return (
    <div
      data-testid={`agent-run-step-${step.stepNo}`}
      style={{ borderLeft: "2px solid #f0f0f0", paddingLeft: 12 }}
    >
      <StepHeader step={step} />
      <StepDetails step={step} />
      <StepAnswer step={step} />
      {observation ? (
        <AgentStepObservation
          observation={observation}
          isExpanded={isExpanded}
          stepNo={step.stepNo}
          onToggle={onToggle}
        />
      ) : null}
      <StepError step={step} />
    </div>
  );
}

import { Button, Space, Tag } from "antd";
import { useTranslation } from "../../i18n";
import type { JsonObject } from "../../types/agentOrchestration";

/** 工具观测：后端 serializeObservation 只写 {text, truncated, original_chars}。 */
export interface StepObservation {
  text: string;
  truncated: boolean;
  originalChars: number;
}

/** 从 outputJson 里读出观测内容；截断标记也在这里，不在单独的列上。 */
export function readObservation(
  output: JsonObject | null | undefined,
): StepObservation | null {
  if (!output) return null;
  const text = typeof output.text === "string" ? output.text : "";
  const truncated = output.truncated === true;
  const originalChars =
    typeof output.original_chars === "number" ? output.original_chars : 0;
  if (!text && !truncated) return null;
  return { text, truncated, originalChars };
}

/** JSON 以转义文本渲染（React 自动转义），绝不使用 dangerouslySetInnerHTML。 */
export function renderJson(value: JsonObject): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export interface AgentStepObservationProps {
  observation: StepObservation;
  isExpanded: boolean;
  stepNo: number;
  onToggle: (stepNo: number) => void;
}

/** 工具观测区块：默认折叠，点击才展开；截断标记来自 outputJson.truncated。 */
export default function AgentStepObservation({
  observation,
  isExpanded,
  stepNo,
  onToggle,
}: AgentStepObservationProps) {
  const { t } = useTranslation();
  const toggleLabel = isExpanded
    ? t("agentTask.timeline.hideOutput")
    : t("agentTask.timeline.showOutput");
  return (
    <div style={{ marginTop: 4 }}>
      <Space direction="vertical" size="small">
        <Button
          size="small"
          onClick={() => onToggle(stepNo)}
          aria-label={toggleLabel}
        >
          {toggleLabel}
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
  );
}

import { Collapse, Space, Tag, Typography } from "antd";

import { useTranslation } from "../../i18n";
import type { AgentTrace, AgentTraceStep } from "../../types/chat";
import { formatDuration, groupStepsByIteration } from "../../utils/agentTrace";

const { Text } = Typography;

interface AgentTraceCardProps {
  trace: AgentTrace;
}

/** 摘要对象的紧凑渲染：`tables: 3 · truncated: true`；空对象返回空串。 */
function formatRecord(record: Record<string, unknown>): string {
  return Object.entries(record)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(", ") : String(value)}`)
    .join(" · ");
}

function StepRow({ step }: { step: AgentTraceStep }) {
  const { t } = useTranslation();
  const isError = step.status === "error";
  // sqlPreview 单独以 code 块渲染（可能较长），其余参数拼一行
  const { sqlPreview, ...otherArgs } = step.args;
  const argsLine = formatRecord(otherArgs);
  const resultLine = formatRecord(step.resultSummary);

  return (
    <div data-testid="agent-trace-step" style={{ marginBottom: 8 }}>
      <Space size={6} wrap>
        <Text strong>{step.tool}</Text>
        {isError ? (
          <Tag color="error" style={{ marginInlineEnd: 0 }}>
            {t("agentTrace.stepFailed")}
          </Tag>
        ) : null}
        <Text type="secondary" style={{ fontSize: 12 }}>
          {formatDuration(step.elapsedMs)}
        </Text>
      </Space>
      {typeof sqlPreview === "string" && sqlPreview ? (
        <div style={{ marginTop: 2 }}>
          <Text code style={{ fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
            {sqlPreview}
          </Text>
        </div>
      ) : null}
      {argsLine ? (
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {argsLine}
          </Text>
        </div>
      ) : null}
      {isError && step.error ? (
        <div style={{ marginTop: 2 }}>
          <Text type="danger" style={{ fontSize: 12 }}>
            {step.error}
          </Text>
        </div>
      ) : null}
      {!isError && resultLine ? (
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {resultLine}
          </Text>
        </div>
      ) : null}
    </div>
  );
}

/**
 * L4 执行轨迹卡（事后快照，2026-10-31）。
 *
 * - 默认折叠，标题行即摘要：步数 / 轮次 / 总耗时（另带状态与截断提示）；
 * - 展开后按迭代轮次分组，逐个工具调用展示状态、耗时、参数与结果摘要；
 * - 只渲染后端已脱敏的摘要字段（args / resultSummary 是白名单计数），
 *   工具结果原文既不在快照里、也不在这里展示。
 */
export default function AgentTraceCard({ trace }: AgentTraceCardProps) {
  const { t } = useTranslation();
  if (!trace.steps.length) return null;

  const isCapped = trace.status === "max_iterations";
  const summary = t("agentTrace.summary", {
    steps: trace.stepCount,
    iterations: trace.iterations,
    duration: formatDuration(trace.totalMs),
  });

  return (
    <Collapse
      size="small"
      items={[
        {
          key: "agent-trace",
          label: (
            <Space size={6} wrap>
              <Text strong>{t("agentTrace.panelLabel")}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {summary}
              </Text>
              <Tag color={isCapped ? "warning" : "success"} style={{ marginInlineEnd: 0 }}>
                {isCapped
                  ? t("agentTrace.statusMaxIterations")
                  : t("agentTrace.statusAnswered")}
              </Tag>
              {trace.truncated ? (
                <Text type="warning" style={{ fontSize: 12 }}>
                  {t("agentTrace.truncated")}
                </Text>
              ) : null}
            </Space>
          ),
          children: (
            <div>
              {groupStepsByIteration(trace.steps).map((group) => (
                <div key={group.iteration} style={{ marginBottom: 8 }}>
                  <Text
                    type="secondary"
                    style={{ fontSize: 12, display: "block", marginBottom: 4 }}
                  >
                    {t("agentTrace.iterationLabel", { n: group.iteration + 1 })}
                  </Text>
                  {group.steps.map((step) => (
                    <StepRow key={step.index} step={step} />
                  ))}
                </div>
              ))}
            </div>
          ),
        },
      ]}
    />
  );
}

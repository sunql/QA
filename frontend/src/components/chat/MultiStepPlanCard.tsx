import { Collapse, Spin, Steps, Tag, Typography } from "antd";
import type { MultiStepStep, StepStatus } from "../../types/chat";
import { useTranslation } from "../../i18n";
import SqlPreview from "./SqlPreview";

const { Text } = Typography;

// 前端步骤状态 → antd Steps 状态（pending 用 wait 表达「未开始」）
const STATUS_TO_ANTD: Record<StepStatus, "wait" | "process" | "finish" | "error"> = {
  pending: "wait",
  running: "process",
  done: "finish",
  error: "error",
};

const STATUS_TAG_COLOR: Record<StepStatus, string> = {
  pending: "default",
  running: "processing",
  done: "success",
  error: "error",
};

interface MultiStepPlanCardProps {
  steps: MultiStepStep[];
  currentStepIndex?: number;
}

function StatusBadge({ status }: { status: StepStatus }) {
  const { t } = useTranslation();
  const labels: Record<StepStatus, string> = {
    pending: t("multiStep.statusPending"),
    running: t("multiStep.statusRunning"),
    done: t("multiStep.statusDone"),
    error: t("multiStep.statusError"),
  };
  return (
    <Tag color={STATUS_TAG_COLOR[status]} style={{ marginInlineEnd: 0 }}>
      {status === "running" ? <Spin size="small" /> : null}
      {labels[status]}
    </Tag>
  );
}

/**
 * 多步查询计划卡：流式期间实时展示拆解出的各步骤与当前执行进度。
 *
 * - 每步显示 description + subQuestion；aggregationOnly 步骤标注「汇总」。
 * - 状态徽标：待执行 / 执行中（spinner）/ 已完成 / 失败；当前步骤高亮由 antd Steps 状态表达。
 * - 已完成/失败步骤折叠展示 SQL（复用 SqlPreview）与 summary/error（不铺全量 data）。
 */
export default function MultiStepPlanCard({ steps, currentStepIndex }: MultiStepPlanCardProps) {
  const { t } = useTranslation();
  if (!steps.length) return null;

  return (
    <Collapse
      size="small"
      items={[
        {
          key: "multi-step",
          label: t("multiStep.panelLabel"),
          children: (
            <Steps
              size="small"
              direction="vertical"
              current={currentStepIndex}
              items={steps.map((s) => ({
                status: STATUS_TO_ANTD[s.status],
                title: (
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <Text strong>{s.description}</Text>
                    {s.aggregationOnly ? <Tag color="purple">{t("multiStep.summaryStep")}</Tag> : null}
                    <StatusBadge status={s.status} />
                  </span>
                ),
                description: (
                  <div style={{ marginTop: 2 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {s.subQuestion}
                    </Text>
                    {s.status === "done" || s.status === "error" ? (
                      <div style={{ marginTop: 6 }}>
                        {s.sql ? <SqlPreview sql={s.sql} /> : null}
                        {s.summary ? (
                          <Text type="secondary" style={{ display: "block", marginTop: 4 }}>
                            {s.summary}
                          </Text>
                        ) : null}
                        {s.error ? (
                          <Text type="danger" style={{ display: "block", marginTop: 4 }}>
                            {s.error}
                          </Text>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                ),
              }))}
            />
          ),
        },
      ]}
    />
  );
}

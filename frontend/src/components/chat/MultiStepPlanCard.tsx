import { Collapse, Spin, Steps, Tag, Typography } from "antd";
import type { MultiStepStep, StepStatus } from "../../types/chat";
import { useTranslation } from "../../i18n";
import QueryPlanCard from "./QueryPlanCard";
import ResumeRunButton from "./ResumeRunButton";
import SqlPreview from "./SqlPreview";

const { Text } = Typography;

// 前端步骤状态 → antd Steps 状态（pending 用 wait 表达「未开始」）
const STATUS_TO_ANTD: Record<StepStatus, "wait" | "process" | "finish" | "error"> = {
  pending: "wait",
  running: "process",
  done: "finish",
  // 压缩发生在步骤成功之后 ⇒ 阶段上仍是「完成」，只是数据被裁过
  compressed: "finish",
  error: "error",
};

const STATUS_TAG_COLOR: Record<StepStatus, string> = {
  pending: "default",
  running: "processing",
  done: "success",
  compressed: "warning",
  error: "error",
};

/**
 * 折叠头摘要：几步、成功/失败各几、耗时多少 —— 不展开也能读。
 *
 * 三种状态互斥：有 running ⇒ 执行中进度；还有 pending（计划刚下发）⇒ 待执行；
 * 否则即「结束态」，给出成功/失败计数。压缩发生在步骤成功之后 ⇒ 计入成功。
 */
function summaryText(
  t: (key: string, vars?: Record<string, string | number>) => string,
  steps: MultiStepStep[],
  elapsedMs?: number
): string {
  const total = steps.length;
  const runningIndex = steps.findIndex((s) => s.status === "running");
  if (runningIndex >= 0) {
    return t("multiStep.summaryRunning", { current: runningIndex + 1, total });
  }
  if (steps.some((s) => s.status === "pending")) {
    return t("multiStep.summaryPending", { total });
  }
  const success = steps.filter((s) => s.status === "done" || s.status === "compressed").length;
  const failed = steps.filter((s) => s.status === "error").length;
  const parts = [
    t("multiStep.summaryTotal", { total }),
    t("multiStep.summarySuccess", { success }),
  ];
  if (failed > 0) parts.push(t("multiStep.summaryFailed", { failed }));
  if (elapsedMs != null && Number.isFinite(elapsedMs) && elapsedMs >= 0) {
    // 保留一位小数：12.3秒比 12秒 更贴近真实等待
    parts.push(t("multiStep.elapsed", { seconds: Math.round(elapsedMs / 100) / 10 }));
  }
  return parts.join(" · ");
}

interface MultiStepPlanCardProps {
  steps: MultiStepStep[];
  currentStepIndex?: number;
  /**
   * 本轮端到端耗时（毫秒），来自流式 `done` 帧的 `latency_ms`。
   * 历史回放没有时长 ⇒ 缺省，摘要行不显示耗时段（不伪造）。
   */
  elapsedMs?: number;
  /** N4 qwen 续跑：该消息首发时使用的模型 id（透传给 ResumeRunButton） */
  originalModelId: number;
  /**
   * 失败步的续跑回调；不传则不渲染续跑按钮（如历史回放、单步路径）。
   *
   * 第三参 modelId 必须在这里声明：ResumeRunButton 会带它回调（选中的模型 id，
   * 或 null = 用原模型）。此前本类型只声明 2 参，JS 静默丢弃第三参 ⇒ 用户在
   * Popover 里选的模型到不了 MessageItem ⇒ 续跑永远按 run 的原模型跑，
   * 「换模型续跑」（N4）在 Chat 侧整个失效。
   */
  onResume?: (runId: string, fromStepIndex: number, modelId: number | null) => void;
  /** 流式进行中：禁用续跑按钮，防重复点击（由调用方从 store 的 loading 提供） */
  disabled?: boolean;
}

function StatusBadge({ status }: { status: StepStatus }) {
  const { t } = useTranslation();
  const labels: Record<StepStatus, string> = {
    pending: t("multiStep.statusPending"),
    running: t("multiStep.statusRunning"),
    done: t("multiStep.statusDone"),
    compressed: t("multiStep.statusCompressed"),
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
export default function MultiStepPlanCard({
  steps,
  currentStepIndex,
  elapsedMs,
  originalModelId,
  onResume,
  disabled,
}: MultiStepPlanCardProps) {
  const { t } = useTranslation();
  if (!steps.length) return null;

  return (
    <Collapse
      size="small"
      items={[
        {
          key: "multi-step",
          label: (
            <span data-testid="multi-step-summary" style={{ fontWeight: 500 }}>
              {summaryText(t, steps, elapsedMs)}
            </span>
          ),
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
                    {s.status === "done" || s.status === "compressed" || s.status === "error" ? (
                      <div style={{ marginTop: 6 }}>
                        {/* 每步自己的查询计划（仅成功步骤有值） */}
                        {s.queryPlan ? (
                          <div style={{ marginBottom: 6 }}>
                            <QueryPlanCard plan={s.queryPlan} dataQuality={null} />
                          </div>
                        ) : null}
                        {s.sql ? <SqlPreview sql={s.sql} /> : null}
                        {s.summary ? (
                          <Text type="secondary" style={{ display: "block", marginTop: 4 }}>
                            {s.summary}
                          </Text>
                        ) : null}
                        {/* 压缩步：数据被裁过，展示原始行数 → 保留行数（step_compressed 回填） */}
                        {s.status === "compressed" && s.originalRows != null && s.compressedRows != null ? (
                          <Text type="warning" style={{ display: "block", marginTop: 4 }}>
                            {t("multiStep.compressedRows", {
                              from: s.originalRows,
                              to: s.compressedRows,
                            })}
                          </Text>
                        ) : null}
                        {s.error ? (
                          <Text type="danger" style={{ display: "block", marginTop: 4 }}>
                            {s.error}
                          </Text>
                        ) : null}
                        {/* 失败步的续跑入口：仅当后端盖了 runId（多步路径）且调用方给了回调。
                            汇总步（aggregationOnly）**排除**：它不落 multi_step_step 行，
                            其 stepIndex = len(data_steps) 恰好等于步骤行数 ⇒ 续跑请求
                            必然落在后端 `start >= len(steps)` 的范围闸上（409），
                            渲染出来只会给用户一个点了必失败的按钮。 */}
                        {s.status === "error" && s.runId && onResume && !s.aggregationOnly ? (
                          <div style={{ marginTop: 6 }}>
                            <ResumeRunButton
                              runId={s.runId}
                              fromStepIndex={s.stepIndex}
                              originalModelId={originalModelId}
                              disabled={disabled}
                              onResume={onResume}
                            />
                          </div>
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

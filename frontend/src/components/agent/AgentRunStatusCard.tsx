import { Alert, Button, Card, Space, Tag } from "antd";
import { useTranslation } from "../../i18n";
import type { AgentRunRead } from "../../types/agentOrchestration";

export interface AgentRunStatusCardProps {
  run: AgentRunRead;
  /** 已请求取消但服务端尚未确认。 */
  isCancelPending: boolean;
  /** 恢复 / 取消请求进行中。 */
  busy: boolean;
  onCancel: () => void;
}

/**
 * 运行级失败原因。
 *
 * 有些失败发生在规划器 / Runner 层，此时没有任何 FAILED 步骤行，
 * 只有运行快照上的 errorCode / errorMessage——不展示就等于把诊断吞掉。
 */
function RunFailureReason({ run }: { run: AgentRunRead }) {
  const { t } = useTranslation();
  if (run.status !== "FAILED") return null;
  const detail = run.errorMessage ?? "";
  if (!detail) return null;
  return (
    <Alert
      type="error"
      showIcon
      message={t("agentTask.run.runError", { message: detail })}
      description={
        run.errorCode
          ? t("agentTask.run.runErrorCode", { code: run.errorCode })
          : undefined
      }
    />
  );
}

/** 运行状态卡片：状态标签、暂停原因、失败原因与取消按钮。 */
export default function AgentRunStatusCard({
  run,
  isCancelPending,
  busy,
  onCancel,
}: AgentRunStatusCardProps) {
  const { t } = useTranslation();
  const pauseCode = run.pauseCode ?? null;
  const isActive = run.status === "RUNNING" || run.status === "QUEUED";
  return (
    <Card size="small">
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <Space wrap>
          <span>{t("agentTask.run.id", { runId: run.runId })}</span>
          <Tag>{t("agentTask.status." + run.status)}</Tag>
          {pauseCode ? (
            <Space size="small">
              <span>{t("agentTask.run.pauseReason")}</span>
              <Tag color="orange">{t("agentTask.pause." + pauseCode)}</Tag>
            </Space>
          ) : null}
        </Space>
        <RunFailureReason run={run} />
        {isCancelPending ? (
          <Alert
            type="warning"
            showIcon
            message={t("agentTask.run.cancelRequested")}
          />
        ) : null}
        {isActive ? (
          <Button danger disabled={busy} onClick={onCancel}>
            {t("agentTask.run.cancelButton")}
          </Button>
        ) : null}
      </Space>
    </Card>
  );
}

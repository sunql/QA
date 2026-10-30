import { Alert, Button, Card, Input, InputNumber, Space } from "antd";
import { useTranslation } from "../../i18n";
import type { AgentRunRead } from "../../types/agentOrchestration";
import type { ResumeControls } from "../../types/agentTask";

export interface AgentResumePanelProps {
  run: AgentRunRead;
  controls: ResumeControls;
}

/** 补充输入区：NEEDS_INPUT / AMBIGUOUS 时出现。 */
function SupplementaryInputArea({ controls }: { controls: ResumeControls }) {
  const { t } = useTranslation();
  return (
    <Input.TextArea
      value={controls.supplementaryInput}
      onChange={(event) =>
        controls.onSupplementaryInputChange(event.target.value)
      }
      placeholder={t("agentTask.run.supplementaryInputPlaceholder")}
      aria-label={t("agentTask.run.supplementaryInputLabel")}
      autoSize={{ minRows: 2, maxRows: 6 }}
    />
  );
}

/**
 * 预算增量区：BUDGET_EXHAUSTED 时出现。
 *
 * 只给出与 pauseDetail.budget 匹配的那一项；后端没返回预算明细时无法匹配，
 * 直接说明原因而不是渲染一个错的输入框。
 */
function BudgetIncrementArea({ controls }: { controls: ResumeControls }) {
  const { t } = useTranslation();
  if (!controls.budgetKey) {
    return (
      <Alert
        type="warning"
        showIcon
        message={t("agentTask.messages.budgetDetailMissing")}
      />
    );
  }
  const label = t("agentTask.run.increment." + controls.budgetKey);
  return (
    <Space direction="vertical" size="small">
      <span>{t("agentTask.run.budgetTitle")}</span>
      <InputNumber
        value={controls.incrementValue ?? undefined}
        onChange={(value) => controls.onIncrementChange(value ?? null)}
        aria-label={label}
        placeholder={label}
      />
    </Space>
  );
}

/** 恢复面板：按暂停原因给出需要的字段，再提交恢复。 */
export default function AgentResumePanel({
  run,
  controls,
}: AgentResumePanelProps) {
  const { t } = useTranslation();
  const pauseCode = run.pauseCode ?? null;
  const needsInput = pauseCode === "NEEDS_INPUT" || pauseCode === "AMBIGUOUS";
  const needsBudget = pauseCode === "BUDGET_EXHAUSTED";
  return (
    <Card size="small" title={t("agentTask.run.resumeTitle")}>
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        {needsInput ? <SupplementaryInputArea controls={controls} /> : null}
        {needsBudget ? <BudgetIncrementArea controls={controls} /> : null}
        <Button
          type="primary"
          loading={controls.acting}
          disabled={controls.acting}
          onClick={() => void controls.handleResume()}
        >
          {t("agentTask.run.resumeButton")}
        </Button>
      </Space>
    </Card>
  );
}

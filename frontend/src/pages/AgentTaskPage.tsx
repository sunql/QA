import { Alert, Card, Space, Typography } from "antd";
import { useTranslation } from "../i18n";
import AgentCreateRunCard from "../components/agent/AgentCreateRunCard";
import AgentResumePanel from "../components/agent/AgentResumePanel";
import AgentRunStatusCard from "../components/agent/AgentRunStatusCard";
import AgentRunTimeline from "../components/agent/AgentRunTimeline";
import { useAgentRunTracker } from "../components/agent/task/useAgentRunTracker";
import { useCreateRunForm } from "../components/agent/task/useCreateRunForm";
import { useResumeControls } from "../components/agent/task/useResumeControls";
import type { ResumeControls } from "../types/agentTask";
import type {
  AgentRunRead,
  AgentRunStepRead,
} from "../types/agentOrchestration";

export { RUN_POLL_MS, mergeStepLists } from "../components/agent/task/useAgentRunTracker";
export type { BudgetKey } from "../types/agentTask";

const { Title, Paragraph } = Typography;

/** 页面标题与说明。 */
function PageHeader() {
  const { t } = useTranslation();
  return (
    <>
      <Title level={4} style={{ marginTop: 0 }}>
        {t("agentTask.title")}
      </Title>
      <Paragraph type="secondary" style={{ marginBottom: 12 }}>
        {t("agentTask.subtitle")}
      </Paragraph>
    </>
  );
}

/** 页面级错误提示：可选项加载失败与动作失败各一条。 */
function ErrorAlerts({
  loadError,
  actionError,
}: {
  loadError: string | null;
  actionError: string | null;
}) {
  if (!loadError && !actionError) return null;
  return (
    <>
      {loadError ? (
        <Alert type="error" showIcon message={loadError} />
      ) : null}
      {actionError ? (
        <Alert type="error" showIcon message={actionError} />
      ) : null}
    </>
  );
}

/** 运行区域：状态卡片 + 恢复面板 + 执行轨迹。 */
function RunSection({
  run,
  steps,
  isCancelPending,
  resume,
}: {
  run: AgentRunRead;
  steps: AgentRunStepRead[];
  isCancelPending: boolean;
  resume: ResumeControls;
}) {
  return (
    <>
      <AgentRunStatusCard
        run={run}
        isCancelPending={isCancelPending}
        busy={resume.acting}
        onCancel={() => void resume.handleCancel()}
      />
      {run.status === "PAUSED" ? (
        <AgentResumePanel run={run} controls={resume} />
      ) : null}
      <Card size="small">
        <AgentRunTimeline
          steps={steps}
          finalAnswer={run.finalAnswer ?? null}
          costSource={run.costSource}
          costComplete={run.costComplete}
        />
      </Card>
    </>
  );
}

/**
 * Agent 任务页：创建一次编排运行、逐步观察执行轨迹、暂停后恢复、运行中取消。
 *
 * 与旧 Chat/AgentRuntimePage 完全解耦：不复用 Chat 意图识别，也不做模型自动改派——
 * 用户选中的 modelConfigId 原样提交。
 */
export default function AgentTaskPage() {
  const { t } = useTranslation();

  const tracker = useAgentRunTracker(t);
  const {
    run,
    steps,
    actionError,
    isCancelPending,
    openRun,
    cancelRun,
    resumeRun,
    reportError,
    clearError,
  } = tracker;

  const form = useCreateRunForm(t, openRun, reportError, clearError);
  const resume = useResumeControls(t, run, resumeRun, cancelRun, reportError);

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card size="small">
        <PageHeader />
        <AgentCreateRunCard form={form} />
      </Card>
      <ErrorAlerts loadError={form.loadError} actionError={actionError} />
      {run ? (
        <RunSection
          run={run}
          steps={steps}
          isCancelPending={isCancelPending}
          resume={resume}
        />
      ) : null}
    </Space>
  );
}

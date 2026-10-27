import { Alert, App, Card, Space } from "antd";
import { useTranslation } from "../i18n";
import OrchestrationProfileHeader from "../components/agent/OrchestrationProfileHeader";
import OrchestrationProfileModal from "../components/agent/OrchestrationProfileModal";
import OrchestrationProfileTable from "../components/agent/OrchestrationProfileTable";
import { useOrchestrationProfiles } from "../components/agent/useOrchestrationProfiles";

export default function AgentOrchestrationPage() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const controller = useOrchestrationProfiles(t, message);
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <OrchestrationProfileHeader
        onCreate={controller.openCreate}
        onRefresh={() => void controller.refreshProfiles()}
      />

      {controller.pageError ? (
        <Alert type="error" showIcon message={controller.pageError} />
      ) : null}

      <Card size="small">
        <OrchestrationProfileTable
          profiles={controller.profiles}
          loading={controller.loading}
          onEdit={controller.openEdit}
          onToggle={(profile) => void controller.handleToggle(profile)}
        />
      </Card>

      <OrchestrationProfileModal
        open={controller.modalOpen}
        editing={controller.editing}
        saving={controller.saving}
        saveError={controller.saveError}
        form={controller.form}
        agentChoices={controller.agentChoices}
        toolChoices={controller.toolChoices}
        onOk={() => void controller.handleSubmit()}
        onCancel={controller.closeModal}
      />
    </Space>
  );
}

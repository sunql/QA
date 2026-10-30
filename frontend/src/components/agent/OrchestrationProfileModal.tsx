import { Alert, Modal } from "antd";
import type { FormInstance } from "antd";
import { useTranslation } from "../../i18n";
import OrchestrationProfileForm from "./OrchestrationProfileForm";
import type { ProfileFormValues, SelectChoice } from "./OrchestrationProfileForm";
import type { AgentOrchestrationProfileRead } from "../../types/agentOrchestration";

export interface OrchestrationProfileModalProps {
  open: boolean;
  /** 正在编辑的配置；null 表示新建。 */
  editing: AgentOrchestrationProfileRead | null;
  saving: boolean;
  saveError: string | null;
  form: FormInstance<ProfileFormValues>;
  agentChoices: SelectChoice[];
  toolChoices: SelectChoice[];
  onOk: () => void;
  onCancel: () => void;
}

/** 新建 / 编辑编排配置的弹窗（纯展示：提交逻辑在页面层）。 */
export default function OrchestrationProfileModal({
  open,
  editing,
  saving,
  saveError,
  form,
  agentChoices,
  toolChoices,
  onOk,
  onCancel,
}: OrchestrationProfileModalProps) {
  const { t } = useTranslation();
  return (
    <Modal
      open={open}
      forceRender
      width={720}
      title={
        editing
          ? t("agentOrchestration.editTitle")
          : t("agentOrchestration.createTitle")
      }
      okText={
        editing
          ? t("agentOrchestration.editSubmit")
          : t("agentOrchestration.createSubmit")
      }
      cancelText={t("agentOrchestration.cancelButton")}
      confirmLoading={saving}
      onOk={onOk}
      onCancel={onCancel}
    >
      {saveError ? <Alert type="error" showIcon message={saveError} /> : null}
      <OrchestrationProfileForm
        form={form}
        agentChoices={agentChoices}
        toolChoices={toolChoices}
        isEditing={editing !== null}
      />
    </Modal>
  );
}

import { Form, Select } from "antd";
import { useTranslation } from "../../i18n";
import type { SelectChoice } from "./OrchestrationProfileForm";

export interface OrchestrationProfileIdentityFieldProps {
  agentChoices: SelectChoice[];
  /** 编辑态下 Agent 编码不可改（agentCode 在更新请求里不在 body 内）。 */
  isEditing: boolean;
}

/** Agent 编码字段。 */
export default function OrchestrationProfileIdentityField({
  agentChoices,
  isEditing,
}: OrchestrationProfileIdentityFieldProps) {
  const { t } = useTranslation();
  return (
    <Form.Item
      name="agentCode"
      label={t("agentOrchestration.form.agentCode")}
      rules={[
        {
          required: true,
          message: t("agentOrchestration.messages.agentRequired"),
        },
      ]}
    >
      <Select
        showSearch
        optionFilterProp="label"
        disabled={isEditing}
        placeholder={t("agentOrchestration.form.agentCodePlaceholder")}
        options={agentChoices}
      />
    </Form.Item>
  );
}

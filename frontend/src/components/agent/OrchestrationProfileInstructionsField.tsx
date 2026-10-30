import { Form, Input } from "antd";
import { useTranslation } from "../../i18n";

/** 编排指令字段。 */
export default function OrchestrationProfileInstructionsField() {
  const { t } = useTranslation();
  return (
    <Form.Item
      name="instructions"
      label={t("agentOrchestration.form.instructions")}
      rules={[
        {
          required: true,
          message: t("agentOrchestration.messages.instructionsRequired"),
        },
      ]}
    >
      <Input.TextArea
        rows={4}
        placeholder={t("agentOrchestration.form.instructionsPlaceholder")}
      />
    </Form.Item>
  );
}

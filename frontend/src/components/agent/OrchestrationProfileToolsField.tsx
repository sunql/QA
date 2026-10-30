import { Form, Select, Typography } from "antd";
import { useTranslation } from "../../i18n";
import type { SelectChoice } from "./OrchestrationProfileForm";

export interface OrchestrationProfileToolsFieldProps {
  toolChoices: SelectChoice[];
}

/** 绑定工具字段（可多选），以及「暂无可用工具」的提示。 */
export default function OrchestrationProfileToolsField({
  toolChoices,
}: OrchestrationProfileToolsFieldProps) {
  const { t } = useTranslation();
  return (
    <>
      <Form.Item name="toolNames" label={t("agentOrchestration.form.toolNames")}>
        <Select
          mode="multiple"
          optionFilterProp="label"
          placeholder={t("agentOrchestration.form.toolNamesPlaceholder")}
          options={toolChoices}
        />
      </Form.Item>
      {toolChoices.length === 0 ? (
        <Typography.Text type="secondary">
          {t("agentOrchestration.empty.tools")}
        </Typography.Text>
      ) : null}
    </>
  );
}

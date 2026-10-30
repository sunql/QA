import { Button, Card, Input, Select, Space } from "antd";
import { useTranslation } from "../../i18n";
import type { CreateRunForm } from "../../types/agentTask";

export interface AgentCreateRunCardProps {
  form: CreateRunForm;
}

/** 编排配置下拉：只列出启用中的配置。 */
function ProfileSelect({ form }: { form: CreateRunForm }) {
  const { t } = useTranslation();
  const options = form.profiles.map((profile) => ({
    value: profile.agentCode,
    label: profile.agentCode,
  }));
  return (
    <Select<string>
      value={form.agentCode}
      onChange={form.onAgentCodeChange}
      placeholder={t("agentTask.form.profilePlaceholder")}
      aria-label={t("agentTask.form.profileLabel")}
      style={{ width: 260 }}
      options={options}
    />
  );
}

/** 模型下拉：只列出启用中的模型。 */
function ModelSelect({ form }: { form: CreateRunForm }) {
  const { t } = useTranslation();
  const options = form.models.map((model) => ({
    value: model.id,
    label: model.modelName,
  }));
  return (
    <Select<number>
      value={form.modelConfigId}
      onChange={form.onModelChange}
      placeholder={t("agentTask.form.modelPlaceholder")}
      aria-label={t("agentTask.form.modelLabel")}
      style={{ width: 260 }}
      options={options}
    />
  );
}

/** 任务输入框。 */
function TaskInput({ form }: { form: CreateRunForm }) {
  const { t } = useTranslation();
  return (
    <Input.TextArea
      value={form.input}
      onChange={(event) => form.onInputChange(event.target.value)}
      placeholder={t("agentTask.form.inputPlaceholder")}
      aria-label={t("agentTask.form.inputLabel")}
      autoSize={{ minRows: 2, maxRows: 6 }}
    />
  );
}

/** 创建运行卡片：选配置 + 选模型 + 填输入 + 提交。 */
export default function AgentCreateRunCard({
  form,
}: AgentCreateRunCardProps) {
  const { t } = useTranslation();
  return (
    <Card size="small">
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <Space wrap>
          <ProfileSelect form={form} />
          <ModelSelect form={form} />
        </Space>
        <TaskInput form={form} />
        <Button
          type="primary"
          loading={form.creating}
          disabled={form.creating}
          onClick={() => void form.handleCreate()}
        >
          {t("agentTask.form.createButton")}
        </Button>
      </Space>
    </Card>
  );
}

import { Form } from "antd";
import type { FormInstance } from "antd";
import OrchestrationProfileBudgetFields from "./OrchestrationProfileBudgetFields";
import OrchestrationProfileIdentityField from "./OrchestrationProfileIdentityField";
import OrchestrationProfileInstructionsField from "./OrchestrationProfileInstructionsField";
import OrchestrationProfileToolsField from "./OrchestrationProfileToolsField";

/** 编排配置表单的取值。 */
export interface ProfileFormValues {
  agentCode: string;
  instructions: string;
  toolNames: string[];
  defaultMaxIterations: number;
  defaultMaxToolCalls: number;
  defaultMaxCostUsd: number;
  defaultTimeoutSeconds: number;
  maxObservationChars: number;
}

/** Select 选项（Agent 编码 / 工具名）。 */
export interface SelectChoice {
  value: string;
  label: string;
}

/** 预算默认值：与后端 DTO 默认值一致。 */
export const DEFAULT_PROFILE_VALUES = {
  defaultMaxIterations: 20,
  defaultMaxToolCalls: 30,
  defaultMaxCostUsd: 5,
  defaultTimeoutSeconds: 900,
  maxObservationChars: 50000,
} as const;

export interface OrchestrationProfileFormProps {
  form: FormInstance<ProfileFormValues>;
  agentChoices: SelectChoice[];
  toolChoices: SelectChoice[];
  /** 编辑态下 Agent 编码不可改（agentCode 在更新请求里不在 body 内）。 */
  isEditing: boolean;
}

/**
 * 编排配置表单（纯展示）：字段、校验规则与默认值都在这里，
 * 提交逻辑留在页面层，保证组件可独立复用与测试。
 */
export default function OrchestrationProfileForm({
  form,
  agentChoices,
  toolChoices,
  isEditing,
}: OrchestrationProfileFormProps) {
  return (
    <Form form={form} layout="vertical" requiredMark={false}>
      <OrchestrationProfileIdentityField
        agentChoices={agentChoices}
        isEditing={isEditing}
      />
      <OrchestrationProfileInstructionsField />
      <OrchestrationProfileToolsField toolChoices={toolChoices} />
      <OrchestrationProfileBudgetFields />
    </Form>
  );
}

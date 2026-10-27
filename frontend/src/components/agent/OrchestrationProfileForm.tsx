import { Col, Form, Input, InputNumber, Row, Select, Typography } from "antd";
import type { FormInstance } from "antd";
import { useTranslation } from "../../i18n";

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

/**
 * 预算上限：与后端系统天花板一致（backend/app/config.py 的
 * AGENT_ORCHESTRATION_MAX_*）。超过上限后端一律 422，前端先行拦一道。
 */
const MAX_ITERATIONS = 20;
const MAX_TOOL_CALLS = 30;
const MAX_COST_USD = 5;
const MAX_TIMEOUT_SECONDS = 900;
/** 观测字符数上下限：与后端 OBSERVATION_CHARS_MIN/MAX 一致。 */
const MIN_OBSERVATION_CHARS = 1000;
const MAX_OBSERVATION_CHARS = 50000;

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
  const { t } = useTranslation();

  return (
    <Form form={form} layout="vertical" requiredMark={false}>
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

      <Row gutter={16}>
        <Col span={12}>
          <Form.Item
            name="defaultMaxIterations"
            label={t("agentOrchestration.form.defaultMaxIterations")}
          >
            <InputNumber
              min={1}
              max={MAX_ITERATIONS}
              style={{ width: "100%" }}
            />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item
            name="defaultMaxToolCalls"
            label={t("agentOrchestration.form.defaultMaxToolCalls")}
          >
            <InputNumber
              min={1}
              max={MAX_TOOL_CALLS}
              style={{ width: "100%" }}
            />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item
            name="defaultMaxCostUsd"
            label={t("agentOrchestration.form.defaultMaxCostUsd")}
          >
            <InputNumber
              min={0}
              max={MAX_COST_USD}
              step={0.1}
              style={{ width: "100%" }}
            />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item
            name="defaultTimeoutSeconds"
            label={t("agentOrchestration.form.defaultTimeoutSeconds")}
          >
            <InputNumber
              min={1}
              max={MAX_TIMEOUT_SECONDS}
              style={{ width: "100%" }}
            />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item
            name="maxObservationChars"
            label={t("agentOrchestration.form.maxObservationChars")}
          >
            <InputNumber
              min={MIN_OBSERVATION_CHARS}
              max={MAX_OBSERVATION_CHARS}
              step={1000}
              style={{ width: "100%" }}
            />
          </Form.Item>
        </Col>
      </Row>
    </Form>
  );
}

import { Col, Form, InputNumber } from "antd";

/** 预算数字字段可用的表单项名（与 ProfileFormValues 对应）。 */
export type BudgetFieldName =
  | "defaultMaxIterations"
  | "defaultMaxToolCalls"
  | "defaultMaxCostUsd"
  | "defaultTimeoutSeconds"
  | "maxObservationChars";

export interface OrchestrationProfileNumberFieldProps {
  name: BudgetFieldName;
  label: string;
  min: number;
  max: number;
  step?: number;
}

/** 单个预算数字字段（占半行）。 */
export default function OrchestrationProfileNumberField({
  name,
  label,
  min,
  max,
  step,
}: OrchestrationProfileNumberFieldProps) {
  return (
    <Col span={12}>
      <Form.Item name={name} label={label}>
        <InputNumber min={min} max={max} step={step} style={{ width: "100%" }} />
      </Form.Item>
    </Col>
  );
}

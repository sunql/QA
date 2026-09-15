/** DataQualityRuleParamsPage — 规则参数结构化配置页（feat-dq-rule-params Task 11） */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
} from "antd";
import type { RuleParamsReadDto } from "../types/dataQualityRuleParams";
import { listRules, createRule } from "../api/dataQualityRuleParams";
import { RuleParamsForm } from "../components/dq/RuleParamsForm";
import { summarizeRuleParams } from "../utils/ruleParamsSummary";

interface CreateRuleModalProps {
  open: boolean;
  onCancel: () => void;
  onCreated: () => void;
}

/**
 * 把 Form.useForm 放在子组件里，避免页面级 useForm 早于 Form 元素挂载
 * 触发的 "Instance created by useForm is not connected to any Form element" 警告。
 */
function CreateRuleModal({ open, onCancel, onCreated }: CreateRuleModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const [mode, setMode] = useState<"structured" | "custom">("structured");

  async function onCreate() {
    const v = await form.validateFields();
    await createRule({
      ruleCode: v.ruleCode,
      ruleName: v.ruleName,
      ruleType: v.ruleType,
      targetTable: v.targetTable,
      targetColumn: v.targetColumn ?? null,
      datasourceId: v.datasourceId,
      threshold: String(v.threshold),
      severity: v.severity,
      ruleParams: mode === "structured" ? (v.ruleParams ?? null) : null,
      ruleExpression: mode === "custom" ? (v.ruleExpression ?? null) : null,
    });
    form.resetFields();
    onCreated();
  }

  return (
    <Modal
      open={open}
      onCancel={onCancel}
      onOk={onCreate}
      title={t("dqRuleParams.create")}
      width={720}
      destroyOnClose
    >
      <Space style={{ marginBottom: 16 }}>
        <span>{t("dqRuleParams.mode.structured")}</span>
        <Switch
          checked={mode === "custom"}
          onChange={(c) => setMode(c ? "custom" : "structured")}
        />
        <span>{t("dqRuleParams.mode.custom")}</span>
      </Space>
      <Form form={form} layout="vertical">
        <Form.Item name="ruleCode" label="Code" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Form.Item name="ruleName" label="Name" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Form.Item name="ruleType" label="Type" rules={[{ required: true }]}>
          <Select
            options={[
              "COMPLETENESS",
              "VALIDITY",
              "UNIQUENESS",
              "REFERENTIAL",
              "CONSISTENCY",
            ].map((v) => ({ value: v, label: v }))}
          />
        </Form.Item>
        <Form.Item name="targetTable" label="Target Table" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Form.Item name="targetColumn" label="Target Column">
          <Input />
        </Form.Item>
        <Form.Item name="datasourceId" label="Datasource ID" rules={[{ required: true }]}>
          <InputNumber />
        </Form.Item>
        <Form.Item name="threshold" label="Threshold" rules={[{ required: true }]}>
          <InputNumber min={0} max={100} step={0.01} />
        </Form.Item>
        <Form.Item name="severity" label="Severity" rules={[{ required: true }]}>
          <Select
            options={["LOW", "MEDIUM", "HIGH"].map((v) => ({ value: v, label: v }))}
          />
        </Form.Item>
        {mode === "structured" ? (
          <Form.Item name="ruleParams" label="Rule Params">
            <RuleParamsForm
              ruleType={form.getFieldValue("ruleType") ?? "VALIDITY"}
              columns={[
                { name: "ORDER_QTY" },
                { name: "RECEIVED_QTY" },
                { name: "PO_LINE_KEY" },
              ]}
              value={null}
              onChange={(v) => form.setFieldsValue({ ruleParams: v })}
            />
          </Form.Item>
        ) : (
          <Form.Item
            name="ruleExpression"
            label="Rule Expression"
            rules={[{ required: true }]}
          >
            <Input.TextArea rows={3} />
          </Form.Item>
        )}
      </Form>
    </Modal>
  );
}

export function DataQualityRuleParamsPage() {
  const { t, i18n } = useTranslation();
  const [rows, setRows] = useState<RuleParamsReadDto[]>([]);
  const [open, setOpen] = useState(false);

  async function refresh() {
    const data = await listRules();
    setRows(data);
  }

  useEffect(() => {
    void refresh();
  }, []);

  return (
    <div style={{ padding: 24 }}>
      <h2>{t("dqRuleParams.title")}</h2>
      <Button
        type="primary"
        onClick={() => setOpen(true)}
        style={{ marginBottom: 16 }}
      >
        {t("dqRuleParams.create")}
      </Button>
      <Table
        rowKey="id"
        dataSource={rows}
        columns={[
          { title: "Code", dataIndex: "ruleCode" },
          { title: "Name", dataIndex: "ruleName" },
          { title: "Type", dataIndex: "ruleType" },
          {
            title: "Mode",
            dataIndex: "configMode",
            render: (v: string) => (
              <Tag color={v === "structured" ? "geekblue" : "default"}>{v}</Tag>
            ),
          },
          {
            title: "Params",
            dataIndex: "ruleParams",
            render: (params: unknown, row: RuleParamsReadDto) =>
              params
                ? summarizeRuleParams(
                    params as Parameters<typeof summarizeRuleParams>[0],
                    row.targetColumn ?? row.targetTable,
                    i18n.language as "zh-CN" | "en-US",
                  )
                : row.ruleExpression,
          },
        ]}
      />
      {open && (
        <CreateRuleModal
          open={open}
          onCancel={() => setOpen(false)}
          onCreated={() => {
            setOpen(false);
            void refresh();
          }}
        />
      )}
    </div>
  );
}
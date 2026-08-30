import { useCallback, useEffect, useState } from "react";
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
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import {
  createRule,
  disableRule,
  listRules,
  updateRule,
} from "../api/dataQuality";
import { listDataSources } from "../api/datasource";
import type {
  DataQualityRule,
  DataQualityRuleCreate,
  RuleType,
  Severity,
} from "../types/dataQuality";
import type { DataSource } from "../types/datasource";

const RULE_TYPES: RuleType[] = [
  "COMPLETENESS",
  "VALIDITY",
  "UNIQUENESS",
  "CONSISTENCY",
  "REFERENTIAL",
  "TIMELINESS",
];

const SEVERITIES: Severity[] = ["HIGH", "MEDIUM", "LOW", "INFO"];

function severityColor(s: Severity): string {
  switch (s) {
    case "HIGH":
      return "red";
    case "MEDIUM":
      return "orange";
    case "LOW":
      return "blue";
    default:
      return "default";
  }
}

export default function DataQualityPage() {
  const { t } = useTranslation();
  const [rules, setRules] = useState<DataQualityRule[]>([]);
  const [loading, setLoading] = useState(false);
  const [filterType, setFilterType] = useState<RuleType | undefined>();
  const [editing, setEditing] = useState<DataQualityRule | null>(null);
  const [creating, setCreating] = useState(false);
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [form] = Form.useForm<DataQualityRuleCreate>();

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listRules(
        filterType ? { ruleType: filterType } : undefined,
      );
      setRules(data);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      message.error(msg);
    } finally {
      setLoading(false);
    }
  }, [filterType]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 加载数据源列表（用于表单下拉）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const dsList = await listDataSources(true);
        if (!cancelled) setDatasources(dsList);
      } catch {
        // 静默：数据源列表为空时表单仍可用，只是没有下拉选项
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleCreate = async () => {
    const values = await form.validateFields();
    try {
      await createRule(values);
      message.success(t("dataQuality.createSuccess"));
      setCreating(false);
      form.resetFields();
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      message.error(msg);
    }
  };

  const handleUpdate = async () => {
    if (!editing) return;
    const values = await form.validateFields();
    try {
      await updateRule(editing.id, values);
      message.success(t("dataQuality.updateSuccess"));
      setEditing(null);
      form.resetFields();
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      message.error(msg);
    }
  };

  const handleDisable = async (rule: DataQualityRule) => {
    try {
      await disableRule(rule.id);
      message.success(t("dataQuality.disableSuccess"));
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      message.error(msg);
    }
  };

  const openEdit = (rule: DataQualityRule) => {
    setEditing(rule);
    form.setFieldsValue({
      ruleName: rule.ruleName,
      ruleCode: rule.ruleCode,
      datasourceId: rule.datasourceId,
      targetTable: rule.targetTable,
      targetColumn: rule.targetColumn ?? undefined,
      ruleType: rule.ruleType,
      ruleExpression: rule.ruleExpression ?? undefined,
      threshold: rule.threshold,
      severity: rule.severity,
      isEnabled: rule.isEnabled,
      version: rule.version,
      owner: rule.owner ?? undefined,
      description: rule.description ?? undefined,
    });
  };

  const columns: ColumnsType<DataQualityRule> = [
    {
      title: t("dataQuality.ruleCode"),
      dataIndex: "ruleCode",
      key: "ruleCode",
      width: 200,
    },
    {
      title: t("dataQuality.ruleName"),
      dataIndex: "ruleName",
      key: "ruleName",
    },
    {
      title: t("dataQuality.datasource"),
      dataIndex: "datasourceId",
      key: "datasourceId",
      width: 100,
      render: (id: number) => {
        const ds = datasources.find((d) => d.id === id);
        return ds ? ds.name : `#${id}`;
      },
    },
    {
      title: t("dataQuality.targetTable"),
      dataIndex: "targetTable",
      key: "targetTable",
      width: 140,
    },
    {
      title: t("dataQuality.ruleType"),
      dataIndex: "ruleType",
      key: "ruleType",
      width: 130,
    },
    {
      title: t("dataQuality.severity"),
      dataIndex: "severity",
      key: "severity",
      width: 100,
      render: (s: Severity) => <Tag color={severityColor(s)}>{s}</Tag>,
    },
    {
      title: t("dataQuality.threshold"),
      dataIndex: "threshold",
      key: "threshold",
      width: 100,
    },
    {
      title: t("dataQuality.enabled"),
      dataIndex: "isEnabled",
      key: "isEnabled",
      width: 90,
      render: (e: boolean) => (e ? t("common.yes") : t("common.no")),
    },
    {
      title: t("dataQuality.owner"),
      dataIndex: "owner",
      key: "owner",
      width: 120,
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 160,
      render: (_: unknown, record: DataQualityRule) => (
        <Space>
          <Button size="small" onClick={() => openEdit(record)}>
            {t("common.edit")}
          </Button>
          <Button
            size="small"
            danger
            disabled={!record.isEnabled}
            onClick={() => void handleDisable(record)}
          >
            {t("common.disable")}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Select
          allowClear
          placeholder={t("dataQuality.filterType")}
          style={{ width: 200 }}
          value={filterType}
          onChange={(v) => setFilterType(v as RuleType | undefined)}
          options={RULE_TYPES.map((rt) => ({ label: rt, value: rt }))}
        />
        <Button type="primary" onClick={() => setCreating(true)}>
          {t("dataQuality.createRule")}
        </Button>
        <Button onClick={() => void refresh()}>
          {t("common.refresh")}
        </Button>
      </Space>

      <Table
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={rules}
        pagination={{ pageSize: 20 }}
      />

      <Modal
        title={editing ? t("dataQuality.editRule") : t("dataQuality.createRule")}
        open={creating || editing !== null}
        onCancel={() => {
          setCreating(false);
          setEditing(null);
          form.resetFields();
        }}
        onOk={editing ? handleUpdate : handleCreate}
        okText={t("common.save")}
        cancelText={t("common.cancel")}
        width={640}
        destroyOnClose
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item
            name="ruleCode"
            label={t("dataQuality.ruleCode")}
            rules={[
              { required: true },
              {
                pattern: /^[A-Z][A-Z0-9_]*$/,
                message: t("dataQuality.ruleCodePattern"),
              },
            ]}
          >
            <Input disabled={!!editing} />
          </Form.Item>
          <Form.Item
            name="ruleName"
            label={t("dataQuality.ruleName")}
            rules={[{ required: true }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="datasourceId"
            label={t("dataQuality.datasource")}
            rules={[{ required: true }]}
          >
            <Select
              disabled={!!editing}
              placeholder={t("dataQuality.datasourcePlaceholder")}
              options={datasources.map((ds) => ({
                label: `${ds.name} (${ds.type})`,
                value: ds.id,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="targetTable"
            label={t("dataQuality.targetTable")}
            rules={[{ required: true }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="targetColumn" label={t("dataQuality.targetColumn")}>
            <Input />
          </Form.Item>
          <Form.Item
            name="ruleType"
            label={t("dataQuality.ruleType")}
            rules={[{ required: true }]}
          >
            <Select options={RULE_TYPES.map((rt) => ({ label: rt, value: rt }))} />
          </Form.Item>
          <Form.Item name="ruleExpression" label={t("dataQuality.ruleExpression")}>
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item name="threshold" label={t("dataQuality.threshold")}>
            <InputNumber min={0} max={100} step={0.01} />
          </Form.Item>
          <Form.Item name="severity" label={t("dataQuality.severity")}>
            <Select
              options={SEVERITIES.map((s) => ({ label: s, value: s }))}
            />
          </Form.Item>
          <Form.Item
            name="isEnabled"
            label={t("dataQuality.enabled")}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item name="owner" label={t("dataQuality.owner")}>
            <Input />
          </Form.Item>
          <Form.Item name="description" label={t("common.description")}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
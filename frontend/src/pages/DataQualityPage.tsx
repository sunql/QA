import { useCallback, useEffect, useMemo, useState } from "react";
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
  DataQualityRuleListParams,
  RuleType,
  Severity,
} from "../types/dataQuality";
import type { DataSource } from "../types/datasource";
import { useDataQualityFilterOptions } from "../hooks/useDataQualityFilterOptions";

type EnabledFilter = "all" | "enabled" | "disabled";

interface FilterValues {
  ruleName: string | undefined;
  datasourceId: number | undefined;
  targetTable: string | undefined;
  ruleType: RuleType | undefined;
  severity: Severity | undefined;
  enabled: EnabledFilter;
}

const EMPTY_FILTERS: FilterValues = {
  ruleName: undefined,
  datasourceId: undefined,
  targetTable: undefined,
  ruleType: undefined,
  severity: undefined,
  enabled: "all",
};

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
  const [filters, setFilters] = useState<FilterValues>(EMPTY_FILTERS);
  const [editing, setEditing] = useState<DataQualityRule | null>(null);
  const [creating, setCreating] = useState(false);
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [form] = Form.useForm<DataQualityRuleCreate>();
  const { options: filterOptions } = useDataQualityFilterOptions();

  // 加载数据源列表（用于表单下拉 + 筛选下拉回填）
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

  const updateFilter = useCallback(
    <K extends keyof FilterValues>(key: K, value: FilterValues[K]) => {
      setFilters((prev) => {
        const next = { ...prev, [key]: value };
        // 切换数据源时清空目标表（级联）
        if (key === "datasourceId") {
          next.targetTable = undefined;
        }
        return next;
      });
    },
    [],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const params: DataQualityRuleListParams = {};
      if (filters.ruleName) params.ruleName = filters.ruleName;
      if (filters.datasourceId !== undefined) params.datasourceId = filters.datasourceId;
      if (filters.targetTable) params.targetTable = filters.targetTable;
      if (filters.ruleType) params.ruleType = filters.ruleType;
      if (filters.severity) params.severity = filters.severity;
      if (filters.enabled !== "all") params.enabled = filters.enabled;
      const data = await listRules(params);
      setRules(data);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      message.error(msg);
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 数据源下的目标表选项（DISTINCT 全量 + 前端按 datasourceId 过滤）
  const availableTargetTables = useMemo(() => {
    if (!filterOptions.targetTables) return [];
    return filterOptions.targetTables;
  }, [filterOptions.targetTables]);

  const resetFilters = useCallback(() => {
    setFilters(EMPTY_FILTERS);
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
      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          allowClear
          showSearch
          data-testid="filter-rule-name"
          placeholder={t("dataQuality.filterRuleName")}
          style={{ width: 200 }}
          value={filters.ruleName}
          onChange={(v) => updateFilter("ruleName", v)}
          options={filterOptions.ruleNames.map((n) => ({ label: n, value: n }))}
        />
        <Select
          allowClear
          showSearch
          data-testid="filter-datasource"
          placeholder={t("dataQuality.filterDatasource")}
          style={{ width: 200 }}
          value={filters.datasourceId}
          onChange={(v) =>
            updateFilter("datasourceId", v === undefined ? undefined : Number(v))
          }
          options={filterOptions.datasourceIds.map((d) => ({
            label: d.name,
            value: d.id,
          }))}
        />
        <Select
          allowClear
          showSearch
          data-testid="filter-target-table"
          disabled={filters.datasourceId === undefined}
          placeholder={
            filters.datasourceId === undefined
              ? t("dataQuality.filterTargetTableDisabled")
              : t("dataQuality.filterTargetTable")
          }
          style={{ width: 200 }}
          value={filters.targetTable}
          onChange={(v) => updateFilter("targetTable", v)}
          options={availableTargetTables.map((t) => ({
            label: t,
            value: t,
          }))}
        />
        <Select
          allowClear
          data-testid="filter-rule-type"
          placeholder={t("dataQuality.filterType")}
          style={{ width: 160 }}
          value={filters.ruleType}
          onChange={(v) => updateFilter("ruleType", v as RuleType | undefined)}
          options={RULE_TYPES.map((rt) => ({ label: rt, value: rt }))}
        />
        <Select
          allowClear
          data-testid="filter-severity"
          placeholder={t("dataQuality.filterSeverity")}
          style={{ width: 140 }}
          value={filters.severity}
          onChange={(v) => updateFilter("severity", v as Severity | undefined)}
          options={filterOptions.severities.map((s) => ({
            label: s,
            value: s,
          }))}
        />
        <Select
          data-testid="filter-enabled"
          placeholder={t("dataQuality.filterEnabled")}
          style={{ width: 130 }}
          value={filters.enabled}
          onChange={(v) => updateFilter("enabled", (v ?? "all") as EnabledFilter)}
          options={[
            { label: t("dataQuality.filterEnabledOptions.all"), value: "all" },
            { label: t("dataQuality.filterEnabledOptions.enabled"), value: "enabled" },
            { label: t("dataQuality.filterEnabledOptions.disabled"), value: "disabled" },
          ]}
        />
        <Button onClick={resetFilters}>{t("common.reset")}</Button>
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
/** DataQualityPage — 数据质量（规则维护 + 质量评分）。
 *
 * Tab 0「规则」：规则列表（**六字段级联筛选** + 模糊查询）、新建/编辑/停用、
 *   单条评估与批量评估（feat-dq-rule-list-filters + 评估结果弹窗）。
 * Tab 1「质量评分」：评分列表 + 一键计算。
 *
 * 筛选栏那六个下拉是本页的既有能力，改动本文件时不要顺手简化掉：`listRules` 的
 * 入参是 `DataQualityRuleListParams` 的全部可选字段，后端 `data_quality.py` 逐个支持。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
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
import {
  computeScore,
  evaluateBatch,
  evaluateRule,
  listScores,
} from "../api/dataQualityScore";
import { listDataSources } from "../api/datasource";
import type {
  DataQualityRule,
  DataQualityRuleCreate,
  DataQualityRuleListParams,
  RuleType,
  Severity,
} from "../types/dataQuality";
import type {
  DataQualityScore,
  EvaluationResult,
  ScoreListParams,
  ScoreType,
} from "../types/dataQualityScore";
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

const SCORE_TYPES: ScoreType[] = ["TABLE", "DATABASE", "COLUMN"];

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

/** 综合评分配色：>=90 绿 / >=70 橙 / 其余红。 */
function scoreColor(score: string): string {
  const n = parseFloat(score);
  if (n >= 90) return "green";
  if (n >= 70) return "orange";
  return "red";
}

// ---------------------------------------------------------------------------
// Tab 0：规则
// ---------------------------------------------------------------------------

function RulesTab() {
  const { t } = useTranslation();
  const [rules, setRules] = useState<DataQualityRule[]>([]);
  const [loading, setLoading] = useState(false);
  const [filters, setFilters] = useState<FilterValues>(EMPTY_FILTERS);
  const [editing, setEditing] = useState<DataQualityRule | null>(null);
  const [creating, setCreating] = useState(false);
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [form] = Form.useForm<DataQualityRuleCreate>();
  const { options: filterOptions } = useDataQualityFilterOptions();
  const [evaluating, setEvaluating] = useState<number | null>(null);
  const [evalModal, setEvalModal] = useState<EvaluationResult | null>(null);
  const [batchLoading, setBatchLoading] = useState(false);

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
      void message.error(msg);
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
      void message.success(t("dataQuality.createSuccess"));
      setCreating(false);
      form.resetFields();
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    }
  };

  const handleUpdate = async () => {
    if (!editing) return;
    const values = await form.validateFields();
    try {
      await updateRule(editing.id, values);
      void message.success(t("dataQuality.updateSuccess"));
      setEditing(null);
      form.resetFields();
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    }
  };

  const handleDisable = async (rule: DataQualityRule) => {
    try {
      await disableRule(rule.id);
      void message.success(t("dataQuality.disableSuccess"));
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    }
  };

  const handleEvaluate = async (rule: DataQualityRule) => {
    setEvaluating(rule.id);
    try {
      const result = await evaluateRule(rule.id);
      setEvalModal(result);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    } finally {
      setEvaluating(null);
    }
  };

  /** 批量评估**当前列表里已启用的规则** —— 后端会自己再过滤一次，这里先挡是为了
   *  在没有任何可评估对象时给出人话提示，而不是发一个空 ruleIds 的请求。 */
  const handleBatchEvaluate = async () => {
    const enabledRules = rules.filter((r) => r.isEnabled);
    if (enabledRules.length === 0) {
      void message.warning(t("dataQuality.evaluateBatchNoEnabled"));
      return;
    }
    setBatchLoading(true);
    try {
      const result = await evaluateBatch({
        ruleIds: enabledRules.map((r) => r.id),
      });
      void message.success(
        t("dataQuality.evaluateBatchSuccess", {
          total: result.summaryTotal,
          passed: result.summaryPassed,
        }),
      );
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    } finally {
      setBatchLoading(false);
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
      width: 220,
      render: (_: unknown, record: DataQualityRule) => (
        <Space>
          <Button
            size="small"
            loading={evaluating === record.id}
            onClick={() => void handleEvaluate(record)}
          >
            {t("dataQuality.evaluate")}
          </Button>
          <Button size="small" onClick={() => openEdit(record)}>
            {t("common.edit")}
          </Button>
          <Button
            size="small"
            danger
            disabled={!record.isEnabled}
            onClick={() => void handleDisable(record)}
          >
            {t("common.disabled")}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <>
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
          options={availableTargetTables.map((tbl) => ({
            label: tbl,
            value: tbl,
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
        <Button loading={batchLoading} onClick={() => void handleBatchEvaluate()}>
          {t("dataQuality.evaluateBatch")}
        </Button>
      </Space>

      <Table
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={rules}
        pagination={{ pageSize: 20 }}
      />

      {/* 评估结果 */}
      <Modal
        title={t("dataQuality.evalResult.title")}
        open={evalModal !== null}
        onCancel={() => setEvalModal(null)}
        footer={
          <Button onClick={() => setEvalModal(null)}>{t("common.close")}</Button>
        }
        width={560}
      >
        {evalModal && (
          <Descriptions column={2} bordered size="small">
            <Descriptions.Item label={t("dataQuality.evalResult.ruleCode")}>
              {evalModal.ruleCode}
            </Descriptions.Item>
            <Descriptions.Item label={t("dataQuality.evalResult.ruleType")}>
              {evalModal.ruleType}
            </Descriptions.Item>
            <Descriptions.Item label={t("dataQuality.evalResult.totalCount")}>
              {evalModal.totalCount.toLocaleString()}
            </Descriptions.Item>
            <Descriptions.Item label={t("dataQuality.evalResult.passedCount")}>
              {evalModal.passedCount.toLocaleString()}
            </Descriptions.Item>
            <Descriptions.Item label={t("dataQuality.evalResult.passRate")}>
              <Badge
                status={evalModal.status === "PASS" ? "success" : "error"}
                text={`${evalModal.passRate.toFixed(2)}%`}
              />
            </Descriptions.Item>
            <Descriptions.Item label={t("dataQuality.evalResult.status")}>
              <Tag color={evalModal.status === "PASS" ? "green" : "red"}>
                {evalModal.status === "PASS"
                  ? t("dataQuality.evalResult.pass")
                  : t("dataQuality.evalResult.fail")}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t("dataQuality.evalResult.duration")}>
              {evalModal.durationMs} ms
            </Descriptions.Item>
            {evalModal.message && (
              <Descriptions.Item
                label={t("dataQuality.evalResult.message")}
                span={2}
              >
                {evalModal.message}
              </Descriptions.Item>
            )}
          </Descriptions>
        )}
      </Modal>

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
        destroyOnHidden
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
            <Select options={SEVERITIES.map((s) => ({ label: s, value: s }))} />
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
    </>
  );
}

// ---------------------------------------------------------------------------
// Tab 1：质量评分
// ---------------------------------------------------------------------------

function ScoresTab() {
  const { t } = useTranslation();
  const [scores, setScores] = useState<DataQualityScore[]>([]);
  const [loading, setLoading] = useState(false);
  const [computeLoading, setComputeLoading] = useState(false);
  const [filterTable, setFilterTable] = useState<string | undefined>();
  const [filterType, setFilterType] = useState<ScoreType | undefined>();
  const [latestOnly, setLatestOnly] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const filters: ScoreListParams = {
        table: filterTable,
        scoreType: filterType,
        latest: latestOnly,
        limit: 100,
      };
      const data = await listScores(filters);
      setScores(data);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    } finally {
      setLoading(false);
    }
  }, [filterTable, filterType, latestOnly]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleCompute = async () => {
    setComputeLoading(true);
    try {
      const result = await computeScore();
      void message.success(
        t("dataQuality.computeScoreSuccess", { count: result.savedScores }),
      );
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    } finally {
      setComputeLoading(false);
    }
  };

  const dimKeys = [
    "completenessScore",
    "validityScore",
    "uniquenessScore",
    "consistencyScore",
    "timelinessScore",
    "referentialScore",
  ] as const;

  const scoreColumns: ColumnsType<DataQualityScore> = [
    {
      title: t("dataQuality.scores.targetTable"),
      dataIndex: "targetTable",
      key: "targetTable",
      width: 160,
    },
    {
      title: t("dataQuality.scores.scoreType"),
      dataIndex: "scoreType",
      key: "scoreType",
      width: 100,
      render: (v: ScoreType) => t(`dataQuality.scores.${v.toLowerCase()}`),
    },
    {
      title: t("dataQuality.scores.overallScore"),
      dataIndex: "overallScore",
      key: "overallScore",
      width: 100,
      render: (v: string) => (
        <Tag color={scoreColor(v)}>{parseFloat(v).toFixed(1)}</Tag>
      ),
    },
    ...dimKeys.map((k) => ({
      title: t(`dataQuality.scores.dimensions.${k.replace("Score", "")}`),
      dataIndex: k,
      key: k,
      width: 90,
      render: (v: string | null) =>
        v !== null ? parseFloat(v).toFixed(1) : t("common.dash"),
    })),
    {
      title: t("dataQuality.scores.rulesCount"),
      dataIndex: "rulesCount",
      key: "rulesCount",
      width: 90,
    },
    {
      title: t("dataQuality.scores.evaluatedAt"),
      dataIndex: "evaluatedAt",
      key: "evaluatedAt",
      width: 180,
      render: (v: string) => (v ? new Date(v).toLocaleString() : t("common.dash")),
    },
    {
      title: t("dataQuality.scores.duration"),
      dataIndex: "evaluationDurationMs",
      key: "evaluationDurationMs",
      width: 100,
      render: (v: number) => `${v} ms`,
    },
  ];

  return (
    <>
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          allowClear
          placeholder={t("dataQuality.scoresFilter.table")}
          style={{ width: 200 }}
          value={filterTable}
          onChange={(e) => setFilterTable(e.target.value || undefined)}
        />
        <Select
          allowClear
          placeholder={t("dataQuality.scoresFilter.scoreType")}
          style={{ width: 160 }}
          value={filterType}
          onChange={(v) => setFilterType(v as ScoreType | undefined)}
          options={SCORE_TYPES.map((st) => ({
            label: t(`dataQuality.scores.${st.toLowerCase()}`),
            value: st,
          }))}
        />
        <Button
          type="primary"
          loading={computeLoading}
          onClick={() => void handleCompute()}
        >
          {t("dataQuality.computeScore")}
        </Button>
        <Button onClick={() => void refresh()}>{t("common.refresh")}</Button>
        <Select
          allowClear
          placeholder={t("dataQuality.scoresFilter.latestOnly")}
          style={{ width: 140 }}
          value={latestOnly ? "true" : undefined}
          onChange={(v) => setLatestOnly(v === "true")}
          options={[
            { label: t("dataQuality.scoresFilter.latestOnly"), value: "true" },
          ]}
        />
      </Space>

      <Table
        rowKey="id"
        loading={loading}
        columns={scoreColumns}
        dataSource={scores}
        pagination={{ pageSize: 20 }}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// 页面
// ---------------------------------------------------------------------------

export default function DataQualityPage() {
  const { t } = useTranslation();

  const items = [
    {
      key: "rules",
      label: t("dataQuality.tabs.rules"),
      children: <RulesTab />,
    },
    {
      key: "scores",
      label: t("dataQuality.tabs.scores"),
      children: <ScoresTab />,
    },
  ];

  return <Tabs defaultActiveKey="rules" items={items} style={{ padding: "0 4px" }} />;
}

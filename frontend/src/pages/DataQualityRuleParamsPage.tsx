/** DataQualityRuleParamsPage — 规则参数结构化配置页（feat-dq-rule-params Task 11）
 *
 * 2026-09-15 弹窗改造：
 * - 全中文标签（i18n dqRuleParams.*）
 * - Code 自动生成：DQ-Rule-{YYYYMMDD}-{10位流水}（后端 /next-code，只读预览）
 * - Name 自动生成：数据源名称-类名-规则名（英文，不随 UI 语言切换）
 * - 级联选择：直接选类名 → 反查数据源（默认数据源）+ 目标表；
 *   先选数据源 → 逐一选表 → 类名下拉按 source_table 关联过滤
 * - 阈值手工输入、Severity 手工选择
 *
 * 2026-09-15 列对齐 data-quality 规则 tab：
 * - 规则 tab 有的列全部补齐：编码/名称/数据源/目标表/规则类型/严重级别/阈值/
 *   启用/负责人/操作（评估·编辑·停用），另保留本页特有 配置模式/规则参数
 * - 评估复用 /data-quality/rules/{id}/evaluate，停用复用 DELETE（软删），
 *   编辑走本命名空间 PUT（阈值/严重级别）
 */
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
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
  Spin,
  Switch,
  Table,
  Tag,
  message,
} from "antd";
import type { RuleParamsReadDto } from "../types/dataQualityRuleParams";
import {
  listRules,
  createRule,
  updateRule,
  fetchNextRuleCode,
} from "../api/dataQualityRuleParams";
import { disableRule } from "../api/dataQuality";
import { evaluateRule } from "../api/dataQualityScore";
import type { EvaluationResult } from "../types/dataQualityScore";
import { listClasses } from "../api/ontology";
import {
  getDatasourceSchema,
  introspectDatasource,
  listDataSources,
} from "../api/datasource";
import { RuleParamsForm } from "../components/dq/RuleParamsForm";
import { summarizeRuleParams } from "../utils/ruleParamsSummary";
import { RULE_TYPE_EN } from "../utils/ruleCodeGenerator";
import type { DataSource } from "../types/datasource";
import type { OntologyClass } from "../types/ontology";
import type { RuleTypeLiteral } from "../utils/ruleExpressionTemplates";

/** 与 DataQualityPage.severityColor 保持一致（避免跨页面耦合直接复制） */
function severityColor(s: string): string {
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

const PAGE_SIZE_OPTIONS = [20, 50, 100, 200, 500];

interface TableColumn {
  columnName: string;
  dataType: string;
  isPrimaryKey?: boolean;
}

interface TableSchema {
  tableName: string;
  columns: TableColumn[];
}

/** 规则名自动拼装：数据源名称-类名-规则名（英文，不随 UI 语言切换）。 */
export function buildAutoRuleName(args: {
  datasourceName: string;
  className: string;
  ruleType: string;
}): string {
  const en = RULE_TYPE_EN[args.ruleType] ?? args.ruleType;
  return `${args.datasourceName}-${args.className}-${en}`;
}

const RULE_TYPE_OPTIONS: RuleTypeLiteral[] = [
  "COMPLETENESS",
  "VALIDITY",
  "UNIQUENESS",
  "REFERENTIAL",
  "CONSISTENCY",
];

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

  // ===== 基础数据 =====
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [classes, setClasses] = useState<OntologyClass[]>([]);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [ds, cls] = await Promise.all([
          listDataSources(false),
          listClasses({ includeExpired: false }),
        ]);
        if (cancelled) return;
        setDatasources(ds);
        setClasses(cls);
      } catch (e: unknown) {
        if (!cancelled) {
          message.error(e instanceof Error ? e.message : String(e));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ===== 建议编码（打开弹窗即取，只读预览）=====
  const [codeSuggestion, setCodeSuggestion] = useState("");
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetchNextRuleCode();
        if (!cancelled) setCodeSuggestion(r.code);
      } catch {
        if (!cancelled) setCodeSuggestion("");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ===== 级联选择状态 =====
  const [datasourceId, setDatasourceId] = useState<number | null>(null);
  const [classId, setClassId] = useState<number | null>(null);
  const [targetTable, setTargetTable] = useState("");
  const [tableQuery, setTableQuery] = useState("");
  const [schema, setSchema] = useState<TableSchema[]>([]);
  const [loadingSchema, setLoadingSchema] = useState(false);

  const selectedClass = useMemo(
    () => classes.find((c) => c.id === classId) ?? null,
    [classes, classId],
  );
  const selectedDatasource = useMemo(
    () => datasources.find((d) => d.id === datasourceId) ?? null,
    [datasources, datasourceId],
  );
  const currentTable = useMemo(
    () => schema.find((tb) => tb.tableName === targetTable) ?? null,
    [schema, targetTable],
  );

  // 选类名 → 定位目标表 + 数据源（默认数据源兜底反查）
  function onChangeClass(id: number | null) {
    setClassId(id);
    const cls = classes.find((c) => c.id === id);
    setTargetTable(cls?.sourceTable ?? "");
    if (cls && datasourceId == null) {
      const defaultDs = datasources.find((d) => d.isDefault);
      if (defaultDs) setDatasourceId(defaultDs.id);
    }
  }

  // 选数据源 → 加载 schema，逐一选表
  useEffect(() => {
    if (datasourceId == null) {
      setSchema([]);
      return;
    }
    let cancelled = false;
    (async () => {
      setLoadingSchema(true);
      try {
        let resp;
        try {
          resp = await getDatasourceSchema(datasourceId);
        } catch {
          resp = await introspectDatasource(datasourceId);
        }
        if (cancelled) return;
        setSchema(resp.tables);
      } catch (e: unknown) {
        if (!cancelled) {
          message.error(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!cancelled) setLoadingSchema(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [datasourceId]);

  // 确定表以后 → 类名下拉按 source_table 关联过滤；与所选类冲突则清空类
  function onChangeTable(table: string) {
    setTargetTable(table);
    if (selectedClass && selectedClass.sourceTable !== table) {
      setClassId(null);
    }
  }

  const classOptions = useMemo(() => {
    const list = targetTable
      ? classes.filter((c) => c.sourceTable === targetTable)
      : classes;
    return list.map((c) => ({
      value: c.id,
      label: c.sourceTable ? `${c.className} (${c.sourceTable})` : c.className,
    }));
  }, [classes, targetTable]);

  const filteredTables = useMemo(() => {
    const q = tableQuery.trim().toLowerCase();
    if (!q) return schema;
    return schema.filter((tb) => tb.tableName.toLowerCase().includes(q));
  }, [schema, tableQuery]);

  // ===== 规则类型 / 自动名称 =====
  const ruleType: RuleTypeLiteral =
    (Form.useWatch("ruleType", form) as RuleTypeLiteral) ?? "VALIDITY";

  const autoRuleName = useMemo(() => {
    if (!selectedDatasource || !selectedClass) return "";
    return buildAutoRuleName({
      datasourceName: selectedDatasource.name,
      className: selectedClass.className,
      ruleType,
    });
  }, [selectedDatasource, selectedClass, ruleType]);

  // 编码 / 名称写入表单（只读展示，提交时随表单值发出）
  useEffect(() => {
    form.setFieldsValue({ ruleCode: codeSuggestion });
  }, [codeSuggestion, form]);
  useEffect(() => {
    form.setFieldsValue({ ruleName: autoRuleName });
  }, [autoRuleName, form]);

  async function onCreate() {
    const v = await form.validateFields();
    if (datasourceId == null) {
      message.warning(t("dqRuleParams.form.datasourcePlaceholder"));
      return;
    }
    // next-code 未返回时兜底现取（避免空 ruleCode 打到后端 422）
    const ruleCode = v.ruleCode || (await fetchNextRuleCode()).code;
    await createRule({
      ruleCode,
      ruleName: v.ruleName,
      ruleType: v.ruleType,
      targetTable,
      targetColumn: v.targetColumn ?? null,
      datasourceId,
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
      destroyOnHidden
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
        <Form.Item
          name="ruleCode"
          label={t("dqRuleParams.form.code")}
          extra={t("dqRuleParams.form.codeAutoHint")}
        >
          <Input readOnly />
        </Form.Item>
        <Form.Item
          name="ruleName"
          label={t("dqRuleParams.form.name")}
          extra={t("dqRuleParams.form.nameAutoHint")}
          rules={[{ required: true }]}
        >
          <Input readOnly />
        </Form.Item>
        <Form.Item
          name="ruleType"
          label={t("dqRuleParams.form.ruleType")}
          rules={[{ required: true }]}
        >
          <Select
            options={RULE_TYPE_OPTIONS.map((v) => ({
              value: v,
              label: `${t(`dataQuality.ruleTypeLabels.${v}`)} (${v})`,
            }))}
          />
        </Form.Item>

        <Form.Item
          label={t("dqRuleParams.form.datasource")}
          required
          validateStatus={datasourceId == null ? undefined : "success"}
        >
          <Select
            placeholder={t("dqRuleParams.form.datasourcePlaceholder")}
            value={datasourceId ?? undefined}
            onChange={(id?: number) => setDatasourceId(id ?? null)}
            options={datasources.map((d) => ({
              value: d.id,
              label: `${d.name}${d.type ? ` (${d.type})` : ""}`,
            }))}
            showSearch
            optionFilterProp="label"
            allowClear
          />
        </Form.Item>

        <Form.Item label={t("dqRuleParams.form.className")} required>
          <Select
            placeholder={t("dqRuleParams.form.classPlaceholder")}
            value={classId ?? undefined}
            onChange={onChangeClass}
            options={classOptions}
            showSearch
            optionFilterProp="label"
            allowClear
          />
        </Form.Item>

        <Form.Item
          label={t("dqRuleParams.form.targetTable")}
          required
          extra={
            loadingSchema
              ? t("dqRuleParams.form.loadingSchema")
              : undefined
          }
        >
          <Spin spinning={loadingSchema}>
            <Select
              placeholder={t("dqRuleParams.form.tablePlaceholder")}
              value={targetTable || undefined}
              onChange={onChangeTable}
              showSearch
              searchValue={tableQuery}
              onSearch={setTableQuery}
              filterOption={false}
              options={filteredTables.map((tb) => ({
                value: tb.tableName,
                label: tb.tableName,
              }))}
              disabled={datasourceId == null}
              notFoundContent={null}
              allowClear
            />
          </Spin>
        </Form.Item>

        <Form.Item name="targetColumn" label={t("dqRuleParams.form.targetColumn")}>
          <Select
            allowClear
            options={(currentTable?.columns ?? []).map((c) => ({
              value: c.columnName,
              label: `${c.columnName}${c.dataType ? ` (${c.dataType})` : ""}`,
            }))}
          />
        </Form.Item>

        <Form.Item name="threshold" label={t("dqRuleParams.form.threshold")} rules={[{ required: true }]}>
          <InputNumber min={0} max={100} step={0.01} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="severity" label={t("dqRuleParams.form.severity")} rules={[{ required: true }]}>
          <Select
            options={["LOW", "MEDIUM", "HIGH"].map((v) => ({
              value: v,
              label: t(`dataQuality.severityLabels.${v}`, { defaultValue: v }),
            }))}
          />
        </Form.Item>

        {mode === "structured" ? (
          <Form.Item name="ruleParams" label={t("dqRuleParams.table.params")}>
            <RuleParamsForm
              ruleType={ruleType}
              columns={(currentTable?.columns ?? []).map((c) => ({
                name: c.columnName,
                dataType: c.dataType,
              }))}
              value={null}
              onChange={(v) => form.setFieldsValue({ ruleParams: v })}
            />
          </Form.Item>
        ) : (
          <Form.Item
            name="ruleExpression"
            label={t("dqRuleParams.form.ruleExpression")}
            rules={[{ required: true }]}
          >
            <Input.TextArea rows={3} />
          </Form.Item>
        )}
      </Form>
    </Modal>
  );
}

// ===== 编辑弹窗（阈值/严重级别；对应规则 tab 的编辑入口）=====

interface EditRuleModalProps {
  rule: RuleParamsReadDto | null;
  onCancel: () => void;
  onSaved: () => void;
}

function EditRuleModal({ rule, onCancel, onSaved }: EditRuleModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (rule) {
      form.setFieldsValue({
        threshold: Number(rule.threshold),
        severity: rule.severity,
      });
    }
  }, [rule, form]);

  async function onSave() {
    if (!rule) return;
    const v = await form.validateFields();
    setSaving(true);
    try {
      await updateRule(rule.id, {
        threshold: String(v.threshold),
        severity: v.severity,
      });
      message.success(t("common.save", { defaultValue: "Save" }));
      onSaved();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      open={rule !== null}
      onCancel={onCancel}
      onOk={onSave}
      confirmLoading={saving}
      title={t("dqRuleParams.table.editTitle")}
      width={480}
      destroyOnHidden
    >
      <Form form={form} layout="vertical">
        <Form.Item
          name="threshold"
          label={t("dqRuleParams.form.threshold")}
          rules={[{ required: true }]}
        >
          <InputNumber min={0} max={100} step={0.01} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item
          name="severity"
          label={t("dqRuleParams.form.severity")}
          rules={[{ required: true }]}
        >
          <Select
            options={["LOW", "MEDIUM", "HIGH"].map((v) => ({
              value: v,
              label: t(`dataQuality.severityLabels.${v}`, { defaultValue: v }),
            }))}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}

// ===== 页面 =====

export function DataQualityRuleParamsPage() {
  const { t, i18n } = useTranslation();
  const [rows, setRows] = useState<RuleParamsReadDto[]>([]);
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);

  // 操作列状态
  const [evaluating, setEvaluating] = useState<number | null>(null);
  const [evalModal, setEvalModal] = useState<EvaluationResult | null>(null);
  const [editing, setEditing] = useState<RuleParamsReadDto | null>(null);
  const [disabling, setDisabling] = useState<number | null>(null);

  // 分页（与规则 tab 同配置）
  const [pageSize, setPageSize] = useState(20);

  async function refresh() {
    const data = await listRules();
    setRows(data);
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const [data, ds] = await Promise.all([
          listRules(),
          listDataSources(false),
        ]);
        if (cancelled) return;
        setRows(data);
        setDatasources(ds);
      } catch (e: unknown) {
        if (!cancelled) {
          message.error(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  function resolveDatasourceName(id: number): string {
    const ds = datasources.find((d) => d.id === id);
    return ds ? ds.name : `#${id}`;
  }

  async function handleEvaluate(rule: RuleParamsReadDto) {
    setEvaluating(rule.id);
    try {
      const result = await evaluateRule(rule.id);
      setEvalModal(result);
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setEvaluating(null);
    }
  }

  async function handleDisable(rule: RuleParamsReadDto) {
    setDisabling(rule.id);
    try {
      await disableRule(rule.id);
      message.success(t("dataQuality.disableSuccess"));
      await refresh();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setDisabling(null);
    }
  }

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
        loading={loading}
        dataSource={rows}
        pagination={{
          pageSize,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showSizeChanger: true,
          showTotal: (total) => t("common.totalItems", { total }),
          onChange: (_nextPage, nextSize) => setPageSize(nextSize),
        }}
        columns={[
          {
            title: t("dataQuality.ruleCode"),
            dataIndex: "ruleCode",
            width: 200,
          },
          { title: t("dataQuality.ruleName"), dataIndex: "ruleName" },
          {
            title: t("dataQuality.datasource"),
            dataIndex: "datasourceId",
            width: 100,
            render: (id: number) => resolveDatasourceName(id),
          },
          {
            title: t("dataQuality.targetTable"),
            dataIndex: "targetTable",
            width: 140,
          },
          {
            title: t("dataQuality.ruleType"),
            dataIndex: "ruleType",
            width: 110,
            render: (v: string) =>
              t(`dataQuality.ruleTypeLabels.${v}`, { defaultValue: v }),
          },
          {
            title: t("dataQuality.severity"),
            dataIndex: "severity",
            width: 100,
            render: (s: string) => (
              <Tag color={severityColor(s)}>
                {t(`dataQuality.severityLabels.${s}`, { defaultValue: s })}
              </Tag>
            ),
          },
          {
            title: t("dataQuality.threshold"),
            dataIndex: "threshold",
            width: 90,
          },
          {
            title: t("dataQuality.enabled"),
            dataIndex: "isEnabled",
            width: 80,
            render: (e: boolean) =>
              e
                ? t("dqRuleParams.table.yes")
                : t("dqRuleParams.table.no"),
          },
          { title: t("dataQuality.owner"), dataIndex: "owner", width: 120 },
          {
            title: t("dqRuleParams.table.mode"),
            dataIndex: "configMode",
            width: 100,
            render: (v: string) => (
              <Tag color={v === "structured" ? "geekblue" : "default"}>
                {v === "structured"
                  ? t("dqRuleParams.table.modeStructured")
                  : t("dqRuleParams.table.modeCustom")}
              </Tag>
            ),
          },
          {
            title: t("dqRuleParams.table.params"),
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
          {
            title: t("common.actions"),
            key: "actions",
            width: 220,
            render: (_: unknown, record: RuleParamsReadDto) => (
              <Space>
                <Button
                  size="small"
                  loading={evaluating === record.id}
                  onClick={() => void handleEvaluate(record)}
                >
                  {t("dataQuality.evaluate")}
                </Button>
                <Button size="small" onClick={() => setEditing(record)}>
                  {t("common.edit")}
                </Button>
                <Button
                  size="small"
                  danger
                  loading={disabling === record.id}
                  disabled={!record.isEnabled}
                  onClick={() => void handleDisable(record)}
                >
                  {t("common.disabled")}
                </Button>
              </Space>
            ),
          },
        ]}
      />

      {/* 评估结果（与规则 tab 同布局） */}
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
      <EditRuleModal
        rule={editing}
        onCancel={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          void refresh();
        }}
      />
    </div>
  );
}

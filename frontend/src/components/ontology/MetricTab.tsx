import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import {
  Table,
  Button,
  Modal,
  Form,
  Input,
  Select,
  Space,
  Tooltip,
  Popconfirm,
  message,
  Row,
  Col,
} from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  listMetrics,
  createMetric,
  updateMetric,
  deleteMetric,
} from "../../api/ontology";
import type {
  OntologyClass,
  OntologyMetric,
  OntologyMetricCreate,
  OntologyMetricUpdate,
  AggFunction,
  MetricType,
  MetricAggregationOrder,
  MetricExecutionSource,
  MetricStatus,
  MeasureSpec,
} from "../../types/ontology";
import { AGG_FUNCTION_OPTIONS } from "../../types/ontology";
import { useTranslation } from "../../i18n";
import FilterBar from "./FilterBar";
import type { FilterField } from "./FilterBar";
import { filterMetrics } from "../../utils/ontologyFilter";
import type { FilterValues } from "../../utils/ontologyFilter";
import { classOptions } from "./classOptions";
import MetricTechniqueForm from "./MetricTechniqueForm";
import { fromMeasureSpec, toMeasureSpec } from "../../utils/metricTechnique";

interface MetricFormValues {
  metricName: string;
  metricAlias: string;
  formula: string;
  aggFunction: AggFunction;
  targetClassId: number | undefined;
  dimensionDefaults: string;
  // ── P1 指标技术形态（扁平形状，提交时转 measure JSON）──
  metricType: MetricType;
  aggregationOrder: MetricAggregationOrder | undefined;
  executionSource: MetricExecutionSource | undefined;
  numeratorAgg: string | undefined;
  numeratorExpr: string;
  numeratorFilter: string;
  denominatorAgg: string | undefined;
  // needsDenominator=false 时这两个字段未注册 ⇒ validateFields 不返回它们。
  denominatorExpr: string | undefined;
  denominatorFilter: string | undefined;
  grainKeys: string[];
  filterExpression: string;
  owner: string;
  status: MetricStatus;
}

const EMPTY_METRIC_FORM: MetricFormValues = {
  metricName: "",
  metricAlias: "",
  formula: "",
  aggFunction: "SUM",
  targetClassId: undefined,
  dimensionDefaults: "",
  metricType: "MEASURE",
  aggregationOrder: undefined,
  executionSource: undefined,
  numeratorAgg: undefined,
  numeratorExpr: "",
  numeratorFilter: "",
  denominatorAgg: undefined,
  denominatorExpr: "",
  denominatorFilter: "",
  grainKeys: [],
  filterExpression: "",
  owner: "",
  status: "DRAFT",
};

/** 编辑时库里的原 measure —— 用于把派生字段 `class_id` / `class_alias` 原样带回。 */
interface ExistingMeasures {
  numerator: MeasureSpec | null;
  denominator: MeasureSpec | null;
}

function parseDimensionDefaults(raw: string): Record<string, string> | undefined {
  if (!raw) return undefined;
  const result: Record<string, string> = {};
  for (const pair of raw.split(",")) {
    const [k, ...rest] = pair.trim().split(":");
    if (k) {
      result[k] = rest.join(":") || k;
    }
  }
  return result;
}

function buildMetricPayload(
  values: MetricFormValues,
  existing: ExistingMeasures,
): OntologyMetricCreate {
  return {
    metricName: values.metricName,
    metricAlias: values.metricAlias || undefined,
    formula: values.formula,
    aggFunction: values.aggFunction,
    targetClassId: values.targetClassId,
    dimensionDefaults: parseDimensionDefaults(values.dimensionDefaults),
    metricType: values.metricType,
    numeratorMeasure: toMeasureSpec(
      { agg: values.numeratorAgg, expr: values.numeratorExpr, filter: values.numeratorFilter },
      existing.numerator,
    ),
    denominatorMeasure: toMeasureSpec(
      {
        agg: values.denominatorAgg,
        expr: values.denominatorExpr,
        filter: values.denominatorFilter,
      },
      existing.denominator,
    ),
    aggregationOrder: values.aggregationOrder ?? null,
    grainKeys: values.grainKeys,
    filterExpression: values.filterExpression || null,
    // `status` 恒传：编辑已发布指标 = 一次重新发布（会重跑 Task 11 发布闸门）。
    status: values.status,
    // `owner` 是用户可编辑字段 ⇒ 清空即显式 null。与 measure 内的派生字段
    // `class_id` / `class_alias`（必须保留）刻意不同。
    owner: values.owner || null,
    executionSource: values.executionSource ?? null,
  };
}

export interface MetricTabProps {
  classes: OntologyClass[];
}

export default function MetricTab({ classes }: MetricTabProps) {
  const { t, locale } = useTranslation();
  const [metrics, setMetrics] = useState<OntologyMetric[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<OntologyMetric | null>(null);
  const [form] = Form.useForm<MetricFormValues>();
  // 编辑时保留原 measure 的派生字段（class_id / class_alias）——
  // 后端更新是整块替换 JSONB，不回传就会抹掉回填脚本写入的源类信息。
  const existingMeasures = useRef<ExistingMeasures>({ numerator: null, denominator: null });
  const [filters, setFilters] = useState<FilterValues>({});
  const updateFilter = useCallback(
    (k: string, v: string) => setFilters((prev) => ({ ...prev, [k]: v })),
    []
  );
  const resetFilters = useCallback(() => setFilters({}), []);
  const filteredMetrics = useMemo(() => filterMetrics(metrics, filters), [metrics, filters]);
  const metricFilterFields: FilterField[] = [
    { key: "metricName", label: t("forms.ontology.metricLabels.metricName") },
    { key: "metricAlias", label: t("forms.ontology.metricLabels.metricAlias") },
    {
      key: "targetClassId",
      label: t("forms.ontology.metricLabels.targetClassId"),
      type: "select",
      options: classOptions(classes, locale).map((o) => ({ value: String(o.value), label: o.label })),
    },
  ];

  const aggFunctionOptions = AGG_FUNCTION_OPTIONS.map((o) => ({
    value: o.value,
    label: t(`enums.aggFunction.${o.labelKey}`),
  }));

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setMetrics(await listMetrics());
    } catch {
      // 错误已由拦截器提示
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = () => {
    setEditing(null);
    existingMeasures.current = { numerator: null, denominator: null };
    void form.setFieldsValue(EMPTY_METRIC_FORM);
    setModalOpen(true);
  };

  const openEdit = (record: OntologyMetric) => {
    const dimDefaults =
      record.dimensionDefaults
        ? Object.entries(record.dimensionDefaults)
            .map(([k, v]) => `${k}:${v}`)
            .join(",")
        : "";
    const numerator = fromMeasureSpec(record.numeratorMeasure);
    const denominator = fromMeasureSpec(record.denominatorMeasure);
    existingMeasures.current = {
      numerator: record.numeratorMeasure,
      denominator: record.denominatorMeasure,
    };
    setEditing(record);
    void form.setFieldsValue({
      metricName: record.metricName,
      metricAlias: record.metricAlias ?? "",
      formula: record.formula,
      aggFunction: record.aggFunction as AggFunction,
      targetClassId: record.targetClassId ?? undefined,
      dimensionDefaults: dimDefaults,
      metricType: record.metricType,
      aggregationOrder: record.aggregationOrder ?? undefined,
      executionSource: record.executionSource ?? undefined,
      numeratorAgg: numerator.agg,
      numeratorExpr: numerator.expr,
      numeratorFilter: numerator.filter,
      denominatorAgg: denominator.agg,
      denominatorExpr: denominator.expr,
      denominatorFilter: denominator.filter,
      grainKeys: record.grainKeys ?? [],
      filterExpression: record.filterExpression ?? "",
      owner: record.owner ?? "",
      status: record.status,
    });
    setModalOpen(true);
  };

  const handleDelete = async (id: number) => {
    try {
      await deleteMetric(id);
      void message.success(t("toast.deleted"));
      void load();
    } catch {
      // 错误已由拦截器提示
    }
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      const payload = buildMetricPayload(values, existingMeasures.current);
      if (editing) {
        const updatePayload: OntologyMetricUpdate = { ...payload };
        await updateMetric(editing.id, updatePayload);
        void message.success(t("toast.updated"));
      } else {
        await createMetric(payload);
        void message.success(t("toast.created"));
      }
      setModalOpen(false);
      void load();
    } catch {
      // 表单校验失败：antd 已在字段下就地列出原因（validateFields 的 reject 值
      // 不是 Error，无从按 message 分流）；发布闸门 409 与其它错误的文案由
      // axios 响应拦截器统一 toast。此处只需保证不关闭弹窗 ——
      // `setModalOpen(false)` 在 await 之后，抛错时不会执行。
    }
  };

  const columns = [
    { title: t("forms.ontology.metricColumns.id"), dataIndex: "id", width: 60 },
    { title: t("forms.ontology.metricColumns.metricName"), dataIndex: "metricName" },
    { title: t("forms.ontology.metricColumns.metricAlias"), dataIndex: "metricAlias" },
    {
      title: t("forms.ontology.metricColumns.formula"),
      dataIndex: "formula",
      width: 260,
      ellipsis: { showTitle: false },
      render: (v: string) => (
        <Tooltip placement="topLeft" title={v}>
          {v}
        </Tooltip>
      ),
    },
    {
      title: t("forms.ontology.metricColumns.aggFunction"),
      dataIndex: "aggFunction",
      width: 100,
      render: (fn: AggFunction) => t(`enums.aggFunction.${fn}`),
    },
    {
      title: t("forms.ontology.metricColumns.metricTypeColumn"),
      dataIndex: "metricType",
      width: 110,
      render: (v: MetricType) => t(`enums.metricType.${v}`),
    },
    {
      title: t("forms.ontology.metricColumns.aggregationOrderColumn"),
      dataIndex: "aggregationOrder",
      width: 140,
      render: (v: MetricAggregationOrder | null) =>
        v ? t(`enums.metricAggregationOrder.${v}`) : t("common.emDash"),
    },
    {
      title: t("forms.ontology.metricColumns.executionSourceColumn"),
      dataIndex: "executionSource",
      width: 140,
      render: (v: MetricExecutionSource | null) =>
        v ? t(`enums.metricExecutionSource.${v}`) : t("common.emDash"),
    },
    {
      title: t("forms.ontology.metricColumns.targetClassId"),
      dataIndex: "targetClassId",
      width: 120,
      render: (id: number | null) =>
        id
          ? classes.find((c) => c.id === id)?.className ?? `ID:${id}`
          : t("common.emDash"),
    },
    {
      title: t("forms.ontology.metricColumns.dimensionDefaults"),
      dataIndex: "dimensionDefaults",
      width: 200,
      ellipsis: { showTitle: false },
      render: (v: Record<string, string> | null) => {
        if (!v) return t("common.emDash");
        const text = Object.entries(v)
          .map(([k, val]) => `${k}:${val}`)
          .join(", ");
        return (
          <Tooltip placement="topLeft" title={text}>
            {text}
          </Tooltip>
        );
      },
    },
    {
      title: t("forms.ontology.metricColumns.actions"),
      width: 120,
      render: (_: unknown, record: OntologyMetric) => (
        <Space>
          <Button size="small" onClick={() => openEdit(record)}>
            {t("common.edit")}
          </Button>
          <Popconfirm
            title={t("forms.ontology.deleteConfirm")}
            onConfirm={() => void handleDelete(record.id)}
          >
            <Button size="small" danger>
              {t("common.delete")}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16 }}>
        <span />
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            {t("common.refresh")}
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            {t("forms.ontology.addMetricButton")}
          </Button>
        </Space>
      </div>
      <FilterBar
        fields={metricFilterFields}
        values={filters}
        onChange={updateFilter}
        onReset={resetFilters}
      />
      <Table rowKey="id" loading={loading} dataSource={filteredMetrics} columns={columns} />
      <Modal
        title={
          editing
            ? t("forms.ontology.editMetricModalTitle")
            : t("forms.ontology.addMetricModalTitle")
        }
        open={modalOpen}
        onOk={() => void handleSubmit()}
        onCancel={() => setModalOpen(false)}
        width={560}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" initialValues={EMPTY_METRIC_FORM}>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="metricName"
                label={t("forms.ontology.metricLabels.metricName")}
                rules={[{ required: true, message: t("forms.required") }]}
              >
                <Input placeholder={t("forms.ontology.metricPlaceholders.metricName")} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="metricAlias"
                label={t("forms.ontology.metricLabels.metricAlias")}
              >
                <Input placeholder={t("forms.ontology.metricPlaceholders.metricAlias")} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="aggFunction"
                label={t("forms.ontology.metricLabels.aggFunction")}
                rules={[{ required: true, message: t("forms.required") }]}
              >
                <Select options={aggFunctionOptions} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="targetClassId"
                label={t("forms.ontology.metricLabels.targetClassId")}
              >
                <Select
                  allowClear
                  placeholder={t("forms.ontology.metricPlaceholders.targetClassId")}
                  options={classOptions(classes, locale)}
                />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            name="formula"
            label={t("forms.ontology.metricLabels.formula")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Input placeholder={t("forms.ontology.metricPlaceholders.formula")} />
          </Form.Item>
          <Form.Item
            name="dimensionDefaults"
            label={t("forms.ontology.metricLabels.dimensionDefaults")}
          >
            <Input placeholder={t("forms.ontology.metricPlaceholders.dimensionDefaults")} />
          </Form.Item>
          <MetricTechniqueForm form={form} />
        </Form>
      </Modal>
    </>
  );
}

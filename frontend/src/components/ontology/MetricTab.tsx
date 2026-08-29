import { useEffect, useState, useCallback, useMemo } from "react";
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
} from "../../types/ontology";
import { AGG_FUNCTION_OPTIONS } from "../../types/ontology";
import { useTranslation } from "../../i18n";
import FilterBar from "./FilterBar";
import type { FilterField } from "./FilterBar";
import { filterMetrics } from "../../utils/ontologyFilter";
import type { FilterValues } from "../../utils/ontologyFilter";
import { classOptions } from "./classOptions";

interface MetricFormValues {
  metricName: string;
  metricAlias: string;
  formula: string;
  aggFunction: AggFunction;
  targetClassId: number | undefined;
  dimensionDefaults: string;
}

const EMPTY_METRIC_FORM: MetricFormValues = {
  metricName: "",
  metricAlias: "",
  formula: "",
  aggFunction: "SUM",
  targetClassId: undefined,
  dimensionDefaults: "",
};

export interface MetricTabProps {
  classes: OntologyClass[];
}

export default function MetricTab({ classes }: MetricTabProps) {
  const { t } = useTranslation();
  const [metrics, setMetrics] = useState<OntologyMetric[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<OntologyMetric | null>(null);
  const [form] = Form.useForm<MetricFormValues>();
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
      options: classOptions(t, classes).map((o) => ({ value: String(o.value), label: o.label })),
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
    setEditing(record);
    void form.setFieldsValue({
      metricName: record.metricName,
      metricAlias: record.metricAlias ?? "",
      formula: record.formula,
      aggFunction: record.aggFunction as AggFunction,
      targetClassId: record.targetClassId ?? undefined,
      dimensionDefaults: dimDefaults,
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
      let dimensionDefaults: Record<string, string> | undefined;
      if (values.dimensionDefaults) {
        dimensionDefaults = {};
        for (const pair of values.dimensionDefaults.split(",")) {
          const [k, ...rest] = pair.trim().split(":");
          if (k) {
            dimensionDefaults[k] = rest.join(":") || k;
          }
        }
      }
      const payload: OntologyMetricCreate = {
        metricName: values.metricName,
        metricAlias: values.metricAlias || undefined,
        formula: values.formula,
        aggFunction: values.aggFunction,
        targetClassId: values.targetClassId,
        dimensionDefaults,
      };
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
    } catch (err) {
      if (err instanceof Error && err.message.includes(t("forms.required"))) return;
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
        destroyOnClose
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
                  options={classOptions(t, classes)}
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
        </Form>
      </Modal>
    </>
  );
}

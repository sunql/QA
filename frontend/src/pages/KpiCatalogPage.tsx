import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import {
  Table,
  Button,
  Modal,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Tag,
  Tooltip,
  Popconfirm,
  Alert,
  message,
} from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  listKpis,
  createKpi,
  updateKpi,
  deleteKpi,
  getDatasourceSuggestions,
} from "../api/kpiCatalog";
import { listMetrics } from "../api/ontology";
import type { MetricStatus, OntologyMetric } from "../types/ontology";
import type {
  KpiCatalog,
  KpiDatasourceSuggestion,
  KpiStatus,
} from "../types/kpiCatalog";
import { KPI_STATUS_OPTIONS } from "../types/kpiCatalog";
import {
  buildKpiCreatePayload,
  buildKpiUpdatePayload,
  emptyKpiForm,
  DEFAULT_MATCH_THRESHOLD,
} from "../utils/kpiCatalogPayload";
import type { KpiFormValues } from "../utils/kpiCatalogPayload";
import { useTranslation } from "../i18n";
import { useDatasourceOptions, datasourceName } from "../hooks/useDatasourceOptions";
import { useTablePagination } from "../utils/useTablePagination";
import FilterBar from "../components/ontology/FilterBar";
import type { FilterField } from "../components/ontology/FilterBar";
import { contains, matchSelect } from "../utils/ontologyFilter";
import type { FilterValues } from "../utils/ontologyFilter";

/** 状态 Tag 配色（Phase 4.1）。 */
const KPI_STATUS_TAG_COLOR: Record<KpiStatus, string> = {
  DRAFT: "default",
  PUBLISHED: "green",
  DEPRECATED: "orange",
};

/** 本体指标状态 Tag 配色（位于「技术形态挂靠」列）。 */
const METRIC_STATUS_TAG_COLOR: Record<MetricStatus, string> = {
  DRAFT: "default",
  PUBLISHED: "green",
};

export default function KpiCatalogPage() {
  const { t } = useTranslation();
  const { pagination } = useTablePagination();
  const sources = useDatasourceOptions();
  const [kpis, setKpis] = useState<KpiCatalog[]>([]);
  const [metrics, setMetrics] = useState<OntologyMetric[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<KpiCatalog | null>(null);
  const [suggestions, setSuggestions] = useState<KpiDatasourceSuggestion[]>([]);
  // 建议请求序号：切换编辑目标 / 新建时让在途响应作废，避免串到别的 KPI
  const suggestionSeq = useRef(0);
  const [form] = Form.useForm<KpiFormValues>();
  const [filters, setFilters] = useState<FilterValues>({});
  const updateFilter = useCallback(
    (k: string, v: string) => setFilters((prev) => ({ ...prev, [k]: v })),
    [],
  );
  const resetFilters = useCallback(() => setFilters({}), []);
  const filtered = useMemo(() => {
    return kpis.filter((k) => {
      if (!contains(k.kpiCode, filters.kpiCode ?? "")) return false;
      if (!contains(k.kpiName, filters.kpiName ?? "")) return false;
      if (!contains(k.owner, filters.owner ?? "")) return false;
      if (!matchSelect(filters.status ?? "", k.status)) return false;
      return true;
    });
  }, [kpis, filters]);
  const filterFields: FilterField[] = [
    { key: "kpiCode", label: t("kpiCatalog.columns.kpiCode") },
    { key: "kpiName", label: t("kpiCatalog.columns.kpiName") },
    { key: "owner", label: t("kpiCatalog.columns.owner") },
    { key: "status", label: t("kpiCatalog.columns.status"), type: "select" },
  ];

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setKpis(await listKpis());
    } catch {
      // 错误由拦截器提示
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 指标列表与 KPI 列表互不依赖：一个失败不该影响另一个。
  const loadMetrics = useCallback(async () => {
    try {
      setMetrics(await listMetrics());
    } catch {
      // 错误已由 axios 拦截器 toast；此处降级为空列表，
      // 只影响「技术形态挂靠」列与「挂靠本体指标」下拉，不阻断页面其余部分。
      setMetrics([]);
    }
  }, []);

  useEffect(() => {
    void loadMetrics();
  }, [loadMetrics]);

  // 避免每行都 find
  const metricsById = useMemo(
    () => new Map(metrics.map((m) => [m.id, m])),
    [metrics],
  );

  // 表单初值必须是全新对象（tags 数组不得跨次打开共享实例）
  const initialFormValues = useMemo(() => emptyKpiForm(), []);

  const openCreate = () => {
    suggestionSeq.current += 1;
    setEditing(null);
    setSuggestions([]);
    void form.setFieldsValue(emptyKpiForm());
    setModalOpen(true);
  };

  const openEdit = (record: KpiCatalog) => {
    setEditing(record);
    setSuggestions([]);
    void form.setFieldsValue({
      kpiCode: record.kpiCode,
      kpiName: record.kpiName,
      businessDefinition: record.businessDefinition ?? "",
      formula: record.formula ?? "",
      numerator: record.numerator ?? "",
      denominator: record.denominator ?? "",
      grain: record.grain ?? "",
      unit: record.unit ?? "",
      dataSource: record.dataSource ?? "",
      datasourceId: record.datasourceId ?? undefined,
      owner: record.owner ?? "",
      version: record.version,
      status: record.status,
      // 四个数组一律展开拷贝：form store 不得持有 `kpis` state 里的同一实例
      // （rc-field-form 的 set.js 在 setValue 时会递归 merge 并重建数组）
      semanticKeywords: [...(record.semanticKeywords ?? [])],
      matchThreshold: record.matchThreshold ?? DEFAULT_MATCH_THRESHOLD,
      grainKeys: [...(record.grainKeys ?? [])],
      numeratorColumns: [...(record.numeratorColumns ?? [])],
      denominatorColumns: [...(record.denominatorColumns ?? [])],
      metricId: record.metricId ?? undefined,
    });
    setModalOpen(true);
    // 建议是增强提示：失败不影响编辑流程，静默降级为空
    const seq = ++suggestionSeq.current;
    void getDatasourceSuggestions(record.id)
      .then((rows) => {
        if (seq !== suggestionSeq.current) return;
        setSuggestions(rows.filter((s) => s.datasourceId !== record.datasourceId));
      })
      .catch(() => {
        if (seq === suggestionSeq.current) setSuggestions([]);
      });
  };

  const handleDelete = async (id: number) => {
    try {
      await deleteKpi(id);
      void message.success(t("toast.deleted"));
      void load();
    } catch {
      // 错误由拦截器提示
    }
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      if (editing) {
        await updateKpi(editing.id, buildKpiUpdatePayload(values));
        void message.success(t("toast.updated"));
      } else {
        await createKpi(buildKpiCreatePayload(values));
        void message.success(t("toast.created"));
      }
      setModalOpen(false);
      void load();
    } catch {
      // 错误由拦截器提示
    }
  };

  const topSuggestion = editing ? suggestions[0] : undefined;

  const columns = [
    { title: t("kpiCatalog.columns.kpiCode"), dataIndex: "kpiCode", width: 200 },
    { title: t("kpiCatalog.columns.kpiName"), dataIndex: "kpiName", width: 220 },
    {
      title: t("kpiCatalog.columns.businessDefinition"),
      dataIndex: "businessDefinition",
      width: 240,
      ellipsis: { showTitle: false },
      render: (v: string | null) =>
        v ? (
          <Tooltip placement="topLeft" title={v}>
            {v}
          </Tooltip>
        ) : (
          t("common.dash")
        ),
    },
    { title: t("kpiCatalog.columns.grain"), dataIndex: "grain", width: 120 },
    { title: t("kpiCatalog.columns.unit"), dataIndex: "unit", width: 80 },
    {
      title: t("kpiCatalog.columns.datasourceId"),
      dataIndex: "datasourceId",
      width: 160,
      render: (id: number | null) =>
        id ? (
          datasourceName(sources, id)
        ) : (
          <Tag color="default">{t("kpiCatalog.unbound")}</Tag>
        ),
    },
    { title: t("kpiCatalog.columns.owner"), dataIndex: "owner", width: 120 },
    {
      title: t("kpiCatalog.columns.metricId"),
      dataIndex: "metricId",
      width: 200,
      render: (id: number | null) => {
        if (!id) {
          return <Tag color="orange">{t("kpiCatalog.technical.unlinked")}</Tag>;
        }
        const metric = metricsById.get(id);
        if (!metric) {
          return <Tag color="default">{`ID:${id}`}</Tag>;
        }
        return (
          <Space size={4}>
            <span>{metric.metricName}</span>
            <Tag color={METRIC_STATUS_TAG_COLOR[metric.status]}>
              {t(`enums.metricStatus.${metric.status}`)}
            </Tag>
          </Space>
        );
      },
    },
    {
      title: t("kpiCatalog.columns.version"),
      dataIndex: "version",
      width: 90,
      render: (v: string, row: KpiCatalog) => (
        <span>
          {v}
          {row.revisionCount > 0 ? ` (+${row.revisionCount})` : ""}
        </span>
      ),
    },
    {
      title: t("kpiCatalog.columns.status"),
      dataIndex: "status",
      width: 110,
      render: (v: KpiStatus) => (
        <Tag color={KPI_STATUS_TAG_COLOR[v]}>{t(`enums.kpiStatus.${v}`)}</Tag>
      ),
    },
    {
      title: t("kpiCatalog.columns.actions"),
      width: 160,
      render: (_: unknown, record: KpiCatalog) => (
        <Space>
          <Button size="small" onClick={() => openEdit(record)}>
            {t("common.edit")}
          </Button>
          <Popconfirm
            title={t("kpiCatalog.deleteConfirm")}
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
            {t("kpiCatalog.newButton")}
          </Button>
        </Space>
      </div>
      <FilterBar
        fields={filterFields}
        values={filters}
        onChange={updateFilter}
        onReset={resetFilters}
      />
      <Table rowKey="id" loading={loading} dataSource={filtered} columns={columns} pagination={pagination} />
      <Modal
        title={
          editing
            ? t("kpiCatalog.editTitle")
            : t("kpiCatalog.createTitle")
        }
        open={modalOpen}
        onOk={() => void handleSubmit()}
        onCancel={() => setModalOpen(false)}
        okText={t("common.confirm")}
        cancelText={t("common.cancel")}
        width={640}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" initialValues={initialFormValues}>
          <Form.Item
            name="kpiCode"
            label={t("kpiCatalog.labels.kpiCode")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Input placeholder="KPI_SUPPLIER_OTD" disabled={!!editing} />
          </Form.Item>
          <Form.Item
            name="kpiName"
            label={t("kpiCatalog.labels.kpiName")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Input placeholder={t("kpiCatalog.placeholders.kpiName")} />
          </Form.Item>
          <Form.Item
            name="businessDefinition"
            label={t("kpiCatalog.labels.businessDefinition")}
          >
            <Input.TextArea
              rows={2}
              placeholder={t("kpiCatalog.placeholders.businessDefinition")}
            />
          </Form.Item>
          <Form.Item name="formula" label={t("kpiCatalog.labels.formula")}>
            <Input.TextArea
              rows={2}
              placeholder={t("kpiCatalog.placeholders.formula")}
            />
          </Form.Item>
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item
              name="numerator"
              label={t("kpiCatalog.labels.numerator")}
              style={{ width: "50%" }}
            >
              <Input placeholder={t("kpiCatalog.placeholders.numerator")} />
            </Form.Item>
            <Form.Item
              name="denominator"
              label={t("kpiCatalog.labels.denominator")}
              style={{ width: "50%" }}
            >
              <Input placeholder={t("kpiCatalog.placeholders.denominator")} />
            </Form.Item>
          </Space.Compact>
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item
              name="grain"
              label={t("kpiCatalog.labels.grain")}
              style={{ width: "33%" }}
            >
              <Input placeholder={t("kpiCatalog.placeholders.grain")} />
            </Form.Item>
            <Form.Item
              name="unit"
              label={t("kpiCatalog.labels.unit")}
              style={{ width: "33%" }}
            >
              <Input placeholder={t("kpiCatalog.placeholders.unit")} />
            </Form.Item>
            <Form.Item
              name="owner"
              label={t("kpiCatalog.labels.owner")}
              style={{ width: "33%" }}
            >
              <Input placeholder={t("kpiCatalog.placeholders.owner")} />
            </Form.Item>
          </Space.Compact>
          <Form.Item
            name="datasourceId"
            label={t("kpiCatalog.labels.datasourceId")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              allowClear={!!editing}
              options={sources.map((s) => ({ value: s.id, label: s.name }))}
              placeholder={t("kpiCatalog.placeholders.datasourceId")}
            />
          </Form.Item>
          <Form.Item
            name="metricId"
            label={t("kpiCatalog.labels.metricId")}
          >
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder={t("kpiCatalog.placeholders.metricId")}
              options={metrics.map((m) => ({
                value: m.id,
                label: `${m.metricName}（${m.metricAlias ?? m.id}）`,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="semanticKeywords"
            label={t("kpiCatalog.labels.semanticKeywords")}
          >
            <Select
              mode="tags"
              placeholder={t("kpiCatalog.placeholders.semanticKeywords")}
            />
          </Form.Item>
          <Form.Item
            name="matchThreshold"
            label={t("kpiCatalog.labels.matchThreshold")}
            rules={[
              {
                type: "number",
                min: 0,
                max: 1,
                message: t("kpiCatalog.validation.matchThresholdRange"),
              },
            ]}
          >
            <InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="grainKeys" label={t("kpiCatalog.labels.grainKeys")}>
            <Select
              mode="tags"
              placeholder={t("kpiCatalog.placeholders.grainKeys")}
            />
          </Form.Item>
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item
              name="numeratorColumns"
              label={t("kpiCatalog.labels.numeratorColumns")}
              style={{ width: "50%" }}
            >
              <Select
                mode="tags"
                placeholder={t("kpiCatalog.placeholders.numeratorColumns")}
              />
            </Form.Item>
            <Form.Item
              name="denominatorColumns"
              label={t("kpiCatalog.labels.denominatorColumns")}
              style={{ width: "50%" }}
            >
              <Select
                mode="tags"
                placeholder={t("kpiCatalog.placeholders.denominatorColumns")}
              />
            </Form.Item>
          </Space.Compact>
          {topSuggestion && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message={t("kpiCatalog.suggestions.hint", { name: topSuggestion.name })}
              description={t(
                `kpiCatalog.suggestions.reasons.${topSuggestion.reasons[0]}`,
              )}
              action={
                <Button
                  size="small"
                  onClick={() =>
                    form.setFieldValue("datasourceId", topSuggestion.datasourceId)
                  }
                >
                  {t("kpiCatalog.suggestions.apply")}
                </Button>
              }
            />
          )}
          <Form.Item name="dataSource" label={t("kpiCatalog.labels.dataSource")}>
            <Input placeholder={t("kpiCatalog.placeholders.dataSource")} />
          </Form.Item>
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item
              name="version"
              label={t("kpiCatalog.labels.version")}
              style={{ width: "50%" }}
            >
              <Input placeholder="v1.0" />
            </Form.Item>
            <Form.Item
              name="status"
              label={t("kpiCatalog.labels.status")}
              style={{ width: "50%" }}
            >
              <Select
                options={KPI_STATUS_OPTIONS.map((o) => ({
                  value: o.value,
                  label: t(`enums.kpiStatus.${o.labelKey}`),
                }))}
              />
            </Form.Item>
          </Space.Compact>
        </Form>
      </Modal>
    </>
  );
}

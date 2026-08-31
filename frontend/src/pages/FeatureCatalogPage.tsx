import { useEffect, useState, useCallback, useMemo } from "react";
import {
  Table,
  Button,
  Modal,
  Form,
  Input,
  Select,
  Switch,
  Space,
  Tag,
  Tooltip,
  Popconfirm,
  message,
} from "antd";
import {
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
  UnorderedListOutlined,
} from "@ant-design/icons";
import {
  listFeatures,
  createFeature,
  updateFeature,
  deleteFeature,
  computeFeature,
  computeAllFeatures,
  listFeatureValues,
} from "../api/feature";
import { listDataSources } from "../api/datasource";
import type { DataSource } from "../types/datasource";
import type {
  FeatureDefinition,
  FeatureDefinitionCreate,
  FeatureDefinitionUpdate,
  FeatureStatus,
  FeatureValue,
  FeatureRefreshFrequency,
} from "../types/feature";
import {
  FEATURE_STATUS_OPTIONS,
  FEATURE_REFRESH_OPTIONS,
  FEATURE_ENTITY_TYPES,
} from "../types/feature";
import type { EntityType } from "../types/entityMapping";
import { useTranslation } from "../i18n";
import FilterBar from "../components/ontology/FilterBar";
import type { FilterField } from "../components/ontology/FilterBar";
import { contains, matchSelect } from "../utils/ontologyFilter";
import type { FilterValues } from "../utils/ontologyFilter";

/** 状态 Tag 配色（Phase 4.3）。 */
const FEATURE_STATUS_TAG_COLOR: Record<FeatureStatus, string> = {
  DRAFT: "default",
  ACTIVE: "green",
  DEPRECATED: "orange",
};

interface FeatureFormValues {
  featureName: string;
  featureAlias: string;
  featureDefinition: string;
  entityType: EntityType;
  calculationLogic: string;
  windowSize: string;
  refreshFrequency: FeatureRefreshFrequency;
  unit: string;
  version: string;
  status: FeatureStatus;
  isEnabled: boolean;
  datasourceId: number | undefined;
}

const EMPTY_FEATURE_FORM: FeatureFormValues = {
  featureName: "",
  featureAlias: "",
  featureDefinition: "",
  entityType: "SUPPLIER",
  calculationLogic: "",
  windowSize: "",
  refreshFrequency: "DAILY",
  unit: "",
  version: "v1.0",
  status: "DRAFT",
  isEnabled: true,
  datasourceId: undefined,
};

export default function FeatureCatalogPage() {
  const { t } = useTranslation();
  const [features, setFeatures] = useState<FeatureDefinition[]>([]);
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<FeatureDefinition | null>(null);
  const [form] = Form.useForm<FeatureFormValues>();
  const [filters, setFilters] = useState<FilterValues>({});
  const [valuesModal, setValuesModal] = useState<{
    feature: FeatureDefinition;
    values: FeatureValue[];
    loading: boolean;
  } | null>(null);

  const updateFilter = useCallback(
    (k: string, v: string) => setFilters((prev) => ({ ...prev, [k]: v })),
    [],
  );
  const resetFilters = useCallback(() => setFilters({}), []);

  const filtered = useMemo(() => {
    return features.filter((f) => {
      if (!contains(f.featureName, filters.featureName ?? "")) return false;
      if (!contains(f.entityType, filters.entityType ?? "")) return false;
      if (!contains(f.owner, filters.owner ?? "")) return false;
      if (!matchSelect(filters.status ?? "", f.status)) return false;
      return true;
    });
  }, [features, filters]);

  const filterFields: FilterField[] = [
    { key: "featureName", label: t("feature.columns.featureName") },
    { key: "entityType", label: t("feature.columns.entityType") },
    { key: "owner", label: t("feature.columns.owner") },
    { key: "status", label: t("feature.columns.status"), type: "select" },
  ];

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setFeatures(await listFeatures());
    } catch {
      // 错误由拦截器提示
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    // 数据源仅用于表单下拉，失败静默（不阻断主列表）
    void listDataSources()
      .then(setDatasources)
      .catch(() => setDatasources([]));
  }, [load]);

  const openCreate = () => {
    setEditing(null);
    void form.setFieldsValue(EMPTY_FEATURE_FORM);
    setModalOpen(true);
  };

  const openEdit = (record: FeatureDefinition) => {
    setEditing(record);
    void form.setFieldsValue({
      featureName: record.featureName,
      featureAlias: record.featureAlias ?? "",
      featureDefinition: record.featureDefinition ?? "",
      entityType: record.entityType,
      calculationLogic: record.calculationLogic,
      windowSize: record.windowSize ?? "",
      refreshFrequency: record.refreshFrequency,
      unit: record.unit ?? "",
      version: record.version,
      status: record.status,
      isEnabled: record.isEnabled,
      datasourceId: record.datasourceId,
    });
    setModalOpen(true);
  };

  const handleDelete = async (id: number) => {
    try {
      await deleteFeature(id);
      void message.success(t("toast.deleted"));
      void load();
    } catch {
      // 错误由拦截器提示
    }
  };

  const handleCompute = async (id: number) => {
    try {
      const result = await computeFeature(id);
      void message.success(t("feature.computeDone", { rows: result.rows }));
    } catch {
      // 错误由拦截器提示
    }
  };

  const handleComputeAll = async () => {
    try {
      const result = await computeAllFeatures();
      void message.success(
        t("feature.computeAllDone", { count: result.results.length, rows: result.totalRows }),
      );
    } catch {
      // 错误由拦截器提示
    }
  };

  const openValues = async (record: FeatureDefinition) => {
    setValuesModal({ feature: record, values: [], loading: true });
    try {
      const values = await listFeatureValues(record.id);
      setValuesModal({ feature: record, values, loading: false });
    } catch {
      setValuesModal(null);
    }
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      if (editing) {
        const payload: FeatureDefinitionUpdate = {
          featureName: values.featureName,
          featureAlias: values.featureAlias || null,
          featureDefinition: values.featureDefinition || null,
          entityType: values.entityType,
          calculationLogic: values.calculationLogic,
          windowSize: values.windowSize || null,
          refreshFrequency: values.refreshFrequency,
          unit: values.unit || null,
          version: values.version || undefined,
          status: values.status,
          isEnabled: values.isEnabled,
          datasourceId: values.datasourceId,
        };
        await updateFeature(editing.id, payload);
        void message.success(t("toast.updated"));
      } else {
        const payload: FeatureDefinitionCreate = {
          featureName: values.featureName,
          featureAlias: values.featureAlias || undefined,
          featureDefinition: values.featureDefinition || undefined,
          entityType: values.entityType,
          calculationLogic: values.calculationLogic,
          windowSize: values.windowSize || undefined,
          refreshFrequency: values.refreshFrequency,
          unit: values.unit || undefined,
          version: values.version || undefined,
          status: values.status,
          isEnabled: values.isEnabled,
          datasourceId: values.datasourceId as number,
        };
        await createFeature(payload);
        void message.success(t("toast.created"));
      }
      setModalOpen(false);
      void load();
    } catch {
      // 错误由拦截器提示
    }
  };

  const columns = [
    { title: t("feature.columns.featureName"), dataIndex: "featureName", width: 190 },
    {
      title: t("feature.columns.featureAlias"),
      dataIndex: "featureAlias",
      width: 160,
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
    { title: t("feature.columns.entityType"), dataIndex: "entityType", width: 100 },
    {
      title: t("feature.columns.refreshFrequency"),
      dataIndex: "refreshFrequency",
      width: 110,
      render: (v: FeatureRefreshFrequency) => t(`enums.featureRefreshFrequency.${v}`),
    },
    { title: t("feature.columns.unit"), dataIndex: "unit", width: 80 },
    { title: t("feature.columns.owner"), dataIndex: "owner", width: 120 },
    { title: t("feature.columns.version"), dataIndex: "version", width: 80 },
    {
      title: t("feature.columns.status"),
      dataIndex: "status",
      width: 100,
      render: (v: FeatureStatus) => (
        <Tag color={FEATURE_STATUS_TAG_COLOR[v]}>{t(`enums.featureStatus.${v}`)}</Tag>
      ),
    },
    {
      title: t("feature.columns.isEnabled"),
      dataIndex: "isEnabled",
      width: 80,
      render: (v: boolean) => (v ? t("common.enabled") : t("common.disabled")),
    },
    {
      title: t("feature.columns.actions"),
      width: 280,
      render: (_: unknown, record: FeatureDefinition) => (
        <Space>
          <Button size="small" onClick={() => void openValues(record)} icon={<UnorderedListOutlined />}>
            {t("feature.values")}
          </Button>
          <Button size="small" onClick={() => void handleCompute(record.id)} icon={<ThunderboltOutlined />}>
            {t("feature.compute")}
          </Button>
          <Button size="small" onClick={() => openEdit(record)}>
            {t("common.edit")}
          </Button>
          <Popconfirm
            title={t("feature.deleteConfirm")}
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

  const valuesColumns = [
    { title: t("feature.valueColumns.entityKey"), dataIndex: "entityKey", width: 140 },
    { title: t("feature.valueColumns.value"), dataIndex: "value", width: 140 },
    { title: t("feature.valueColumns.valueText"), dataIndex: "valueText", width: 180 },
    { title: t("feature.valueColumns.validAt"), dataIndex: "validAt", width: 120 },
    { title: t("feature.valueColumns.computedAt"), dataIndex: "computedAt", width: 200 },
  ];

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16 }}>
        <span />
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            {t("common.refresh")}
          </Button>
          <Button icon={<ThunderboltOutlined />} onClick={() => void handleComputeAll()}>
            {t("feature.computeAll")}
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            {t("feature.newButton")}
          </Button>
        </Space>
      </div>
      <FilterBar
        fields={filterFields}
        values={filters}
        onChange={updateFilter}
        onReset={resetFilters}
      />
      <Table rowKey="id" loading={loading} dataSource={filtered} columns={columns} />
      <Modal
        title={editing ? t("feature.editTitle") : t("feature.createTitle")}
        open={modalOpen}
        onOk={() => void handleSubmit()}
        onCancel={() => setModalOpen(false)}
        okText={t("common.confirm")}
        cancelText={t("common.cancel")}
        width={720}
        destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={EMPTY_FEATURE_FORM}>
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item
              name="featureName"
              label={t("feature.labels.featureName")}
              rules={[{ required: true, message: t("forms.required") }]}
              style={{ width: "50%" }}
            >
              <Input placeholder={t("feature.placeholders.featureName")} disabled={!!editing} />
            </Form.Item>
            <Form.Item
              name="featureAlias"
              label={t("feature.labels.featureAlias")}
              style={{ width: "50%" }}
            >
              <Input placeholder={t("feature.placeholders.featureAlias")} />
            </Form.Item>
          </Space.Compact>
          <Form.Item
            name="featureDefinition"
            label={t("feature.labels.featureDefinition")}
          >
            <Input.TextArea
              rows={2}
              placeholder={t("feature.placeholders.featureDefinition")}
            />
          </Form.Item>
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item
              name="entityType"
              label={t("feature.labels.entityType")}
              rules={[{ required: true, message: t("forms.required") }]}
              style={{ width: "33%" }}
            >
              <Select
                options={FEATURE_ENTITY_TYPES.map((et) => ({ value: et, label: et }))}
              />
            </Form.Item>
            <Form.Item
              name="refreshFrequency"
              label={t("feature.labels.refreshFrequency")}
              style={{ width: "33%" }}
            >
              <Select
                options={FEATURE_REFRESH_OPTIONS.map((o) => ({
                  value: o.value,
                  label: t(`enums.featureRefreshFrequency.${o.labelKey}`),
                }))}
              />
            </Form.Item>
            <Form.Item
              name="windowSize"
              label={t("feature.labels.windowSize")}
              style={{ width: "33%" }}
            >
              <Input placeholder={t("feature.placeholders.windowSize")} />
            </Form.Item>
          </Space.Compact>
          <Form.Item
            name="calculationLogic"
            label={t("feature.labels.calculationLogic")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Input.TextArea
              rows={4}
              placeholder={t("feature.placeholders.calculationLogic")}
            />
          </Form.Item>
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item
              name="datasourceId"
              label={t("feature.labels.datasourceId")}
              rules={[{ required: true, message: t("forms.required") }]}
              style={{ width: "40%" }}
            >
              <Select
                placeholder={t("feature.placeholders.datasourceId")}
                options={datasources.map((ds) => ({
                  value: ds.id,
                  label: `${ds.name} (${ds.type})`,
                }))}
              />
            </Form.Item>
            <Form.Item
              name="unit"
              label={t("feature.labels.unit")}
              style={{ width: "20%" }}
            >
              <Input placeholder={t("feature.placeholders.unit")} />
            </Form.Item>
            <Form.Item
              name="version"
              label={t("feature.labels.version")}
              style={{ width: "20%" }}
            >
              <Input placeholder="v1.0" />
            </Form.Item>
            <Form.Item
              name="status"
              label={t("feature.labels.status")}
              style={{ width: "20%" }}
            >
              <Select
                options={FEATURE_STATUS_OPTIONS.map((o) => ({
                  value: o.value,
                  label: t(`enums.featureStatus.${o.labelKey}`),
                }))}
              />
            </Form.Item>
          </Space.Compact>
          <Form.Item
            name="isEnabled"
            label={t("feature.labels.isEnabled")}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={t("feature.valuesTitle", { name: valuesModal?.feature.featureName ?? "" })}
        open={valuesModal !== null}
        onCancel={() => setValuesModal(null)}
        footer={null}
        width={760}
      >
        <Table
          rowKey="id"
          size="small"
          loading={valuesModal?.loading}
          dataSource={valuesModal?.values ?? []}
          columns={valuesColumns}
        />
      </Modal>
    </>
  );
}

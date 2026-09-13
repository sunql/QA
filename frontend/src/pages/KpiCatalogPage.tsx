import { useEffect, useState, useCallback, useMemo } from "react";
import {
  Table,
  Button,
  Modal,
  Form,
  Input,
  Select,
  Space,
  Tag,
  Tooltip,
  Popconfirm,
  message,
} from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  listKpis,
  createKpi,
  updateKpi,
  deleteKpi,
} from "../api/kpiCatalog";
import type {
  KpiCatalog,
  KpiCatalogCreate,
  KpiCatalogUpdate,
  KpiStatus,
} from "../types/kpiCatalog";
import { KPI_STATUS_OPTIONS } from "../types/kpiCatalog";
import { useTranslation } from "../i18n";
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

interface KpiFormValues {
  kpiCode: string;
  kpiName: string;
  businessDefinition: string;
  formula: string;
  numerator: string;
  denominator: string;
  grain: string;
  unit: string;
  dataSource: string;
  owner: string;
  version: string;
  status: KpiStatus;
}

const EMPTY_KPI_FORM: KpiFormValues = {
  kpiCode: "",
  kpiName: "",
  businessDefinition: "",
  formula: "",
  numerator: "",
  denominator: "",
  grain: "",
  unit: "",
  dataSource: "",
  owner: "",
  version: "v1.0",
  status: "DRAFT",
};

export default function KpiCatalogPage() {
  const { t } = useTranslation();
  const [kpis, setKpis] = useState<KpiCatalog[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<KpiCatalog | null>(null);
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

  const openCreate = () => {
    setEditing(null);
    void form.setFieldsValue(EMPTY_KPI_FORM);
    setModalOpen(true);
  };

  const openEdit = (record: KpiCatalog) => {
    setEditing(record);
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
      owner: record.owner ?? "",
      version: record.version,
      status: record.status,
    });
    setModalOpen(true);
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
        const payload: KpiCatalogUpdate = {
          kpiCode: values.kpiCode,
          kpiName: values.kpiName,
          businessDefinition: values.businessDefinition || null,
          formula: values.formula || null,
          numerator: values.numerator || null,
          denominator: values.denominator || null,
          grain: values.grain || null,
          unit: values.unit || null,
          dataSource: values.dataSource || null,
          owner: values.owner || null,
          version: values.version || undefined,
          status: values.status,
        };
        await updateKpi(editing.id, payload);
        void message.success(t("toast.updated"));
      } else {
        const payload: KpiCatalogCreate = {
          kpiCode: values.kpiCode,
          kpiName: values.kpiName,
          businessDefinition: values.businessDefinition || undefined,
          formula: values.formula || undefined,
          numerator: values.numerator || undefined,
          denominator: values.denominator || undefined,
          grain: values.grain || undefined,
          unit: values.unit || undefined,
          dataSource: values.dataSource || undefined,
          owner: values.owner || undefined,
          version: values.version || undefined,
          status: values.status,
        };
        await createKpi(payload);
        void message.success(t("toast.created"));
      }
      setModalOpen(false);
      void load();
    } catch {
      // 错误由拦截器提示
    }
  };

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
    { title: t("kpiCatalog.columns.owner"), dataIndex: "owner", width: 120 },
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
      <Table rowKey="id" loading={loading} dataSource={filtered} columns={columns} />
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
        <Form form={form} layout="vertical" initialValues={EMPTY_KPI_FORM}>
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

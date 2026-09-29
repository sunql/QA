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
  Popconfirm,
  message,
  Row,
  Col,
  Checkbox,
} from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  listPropertiesByClass,
  createProperty,
  updateProperty,
  deleteProperty,
} from "../../api/ontology";
import type {
  OntologyClass,
  OntologyProperty,
  OntologyPropertyCreate,
  OntologyPropertyUpdate,
  DataType,
} from "../../types/ontology";
import { DATA_TYPE_OPTIONS } from "../../types/ontology";
import { useTranslation } from "../../i18n";
import FilterBar from "./FilterBar";
import type { FilterField } from "./FilterBar";
import { filterProperties } from "../../utils/ontologyFilter";
import type { FilterValues } from "../../utils/ontologyFilter";
import { classOptions } from "./classOptions";

interface PropertyFormValues {
  classId: number;
  propertyName: string;
  propertyAlias: string;
  dataType: DataType;
  sourceColumn: string;
  isPrimaryKey: boolean;
  isForeignKey: boolean;
}

const EMPTY_PROPERTY_FORM: PropertyFormValues = {
  classId: 0,
  propertyName: "",
  propertyAlias: "",
  dataType: "STRING",
  sourceColumn: "",
  isPrimaryKey: false,
  isForeignKey: false,
};

export interface PropertyTabProps {
  classes: OntologyClass[];
  refreshClasses: () => Promise<void>;
}

export default function PropertyTab({ classes, refreshClasses }: PropertyTabProps) {
  const { t } = useTranslation();
  const [properties, setProperties] = useState<OntologyProperty[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<OntologyProperty | null>(null);
  const [form] = Form.useForm<PropertyFormValues>();

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const allProps: OntologyProperty[] = [];
      for (const cls of classes) {
        try {
          const props = await listPropertiesByClass(cls.id);
          allProps.push(...props.map((p) => ({ ...p, classId: cls.id })));
        } catch {
          // 忽略单个类的错误
        }
      }
      setProperties(allProps);
    } finally {
      setLoading(false);
    }
  }, [classes]);

  useEffect(() => {
    if (classes.length > 0) {
      void loadAll();
    } else {
      setProperties([]);
    }
  }, [classes, loadAll]);

  const dataTypeOptions = DATA_TYPE_OPTIONS.map((o) => ({
    value: o.value,
    label: t(`enums.dataType.${o.labelKey}`),
  }));

  const [filters, setFilters] = useState<FilterValues>({});
  const updateFilter = useCallback(
    (k: string, v: string) => setFilters((prev) => ({ ...prev, [k]: v })),
    []
  );
  const resetFilters = useCallback(() => setFilters({}), []);
  const filteredProperties = useMemo(
    () => filterProperties(properties, filters),
    [properties, filters]
  );
  const propertyFilterFields: FilterField[] = [
    { key: "propertyName", label: t("forms.ontology.propertyLabels.propertyName") },
    { key: "propertyAlias", label: t("forms.ontology.propertyLabels.propertyAlias") },
    { key: "sourceColumn", label: t("forms.ontology.propertyLabels.sourceColumn") },
    {
      key: "classId",
      label: t("forms.ontology.propertyLabels.classId"),
      type: "select",
      options: classOptions(t, classes).map((o) => ({ value: String(o.value), label: o.label })),
    },
    {
      key: "dataType",
      label: t("forms.ontology.propertyLabels.dataType"),
      type: "select",
      options: dataTypeOptions,
    },
  ];

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    void form.setFieldsValue(EMPTY_PROPERTY_FORM);
    setModalOpen(true);
  };

  const openEdit = (record: OntologyProperty) => {
    setEditing(record);
    void form.setFieldsValue({
      classId: record.classId,
      propertyName: record.propertyName,
      propertyAlias: record.propertyAlias ?? "",
      dataType: record.dataType as DataType,
      sourceColumn: record.sourceColumn ?? "",
      isPrimaryKey: record.isPrimaryKey,
      isForeignKey: record.isForeignKey,
    });
    setModalOpen(true);
  };

  const handleDelete = async (id: number) => {
    try {
      await deleteProperty(id);
      void message.success(t("toast.deleted"));
      void loadAll();
    } catch {
      // 错误已由拦截器提示
    }
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      const payload: OntologyPropertyCreate = {
        classId: values.classId,
        propertyName: values.propertyName,
        propertyAlias: values.propertyAlias || undefined,
        dataType: values.dataType,
        sourceColumn: values.sourceColumn || undefined,
        isPrimaryKey: values.isPrimaryKey,
        isForeignKey: values.isForeignKey,
      };
      if (editing) {
        const updatePayload: OntologyPropertyUpdate = {
          propertyName: payload.propertyName,
          propertyAlias: payload.propertyAlias,
          dataType: payload.dataType,
          sourceColumn: payload.sourceColumn,
          isPrimaryKey: payload.isPrimaryKey,
          isForeignKey: payload.isForeignKey,
        };
        await updateProperty(editing.id, updatePayload);
        void message.success(t("toast.updated"));
      } else {
        await createProperty(payload);
        void message.success(t("toast.created"));
      }
      setModalOpen(false);
      void loadAll();
      await refreshClasses();
    } catch (err) {
      if (err instanceof Error && err.message.includes(t("forms.required"))) return;
    }
  };

  const columns = [
    { title: t("forms.ontology.propertyColumns.id"), dataIndex: "id", width: 60 },
    {
      title: t("forms.ontology.propertyColumns.classId"),
      dataIndex: "classId",
      width: 120,
      render: (classId: number) =>
        classes.find((c) => c.id === classId)?.className ?? `ID:${classId}`,
    },
    {
      title: t("forms.ontology.propertyColumns.propertyName"),
      dataIndex: "propertyName",
    },
    { title: t("forms.ontology.propertyColumns.propertyAlias"), dataIndex: "propertyAlias" },
    {
      title: t("forms.ontology.propertyColumns.dataType"),
      dataIndex: "dataType",
      width: 120,
      render: (dt: DataType) => t(`enums.dataType.${dt}`),
    },
    { title: t("forms.ontology.propertyColumns.sourceColumn"), dataIndex: "sourceColumn" },
    {
      title: t("forms.ontology.propertyColumns.flags"),
      width: 100,
      render: (_: unknown, record: OntologyProperty) => (
        <Space>
          {record.isPrimaryKey && <Tag color="blue">PK</Tag>}
          {record.isForeignKey && <Tag color="green">FK</Tag>}
        </Space>
      ),
    },
    {
      title: t("forms.ontology.propertyColumns.actions"),
      width: 120,
      render: (_: unknown, record: OntologyProperty) => (
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
          <Button icon={<ReloadOutlined />} onClick={() => void loadAll()}>
            {t("common.refresh")}
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={openCreate}
            disabled={classes.length === 0}
          >
            {t("forms.ontology.addPropertyButton")}
          </Button>
        </Space>
      </div>
      {classes.length === 0 && (
        <div style={{ marginBottom: 8, color: "#faad14" }}>
          {t("forms.ontology.propertyEmptyHint")}
        </div>
      )}
      <FilterBar
        fields={propertyFilterFields}
        values={filters}
        onChange={updateFilter}
        onReset={resetFilters}
      />
      <Table rowKey="id" loading={loading} dataSource={filteredProperties} columns={columns} />
      <Modal
        title={
          editing
            ? t("forms.ontology.editPropertyModalTitle")
            : t("forms.ontology.addPropertyModalTitle")
        }
        open={modalOpen}
        onOk={() => void handleSubmit()}
        onCancel={() => setModalOpen(false)}
        width={520}
        destroyOnHidden
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="classId"
            label={t("forms.ontology.propertyLabels.classId")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Select
              placeholder={t("forms.ontology.propertyPlaceholders.classId")}
              options={classOptions(t, classes)}
              disabled={!!editing}
            />
          </Form.Item>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="propertyName"
                label={t("forms.ontology.propertyLabels.propertyName")}
                rules={[{ required: true, message: t("forms.required") }]}
              >
                <Input placeholder={t("forms.ontology.propertyPlaceholders.propertyName")} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="propertyAlias"
                label={t("forms.ontology.propertyLabels.propertyAlias")}
              >
                <Input placeholder={t("forms.ontology.propertyPlaceholders.propertyAlias")} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="dataType"
                label={t("forms.ontology.propertyLabels.dataType")}
                rules={[{ required: true, message: t("forms.required") }]}
              >
                <Select options={dataTypeOptions} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="sourceColumn"
                label={t("forms.ontology.propertyLabels.sourceColumn")}
              >
                <Input placeholder={t("forms.ontology.propertyPlaceholders.sourceColumn")} />
              </Form.Item>
            </Col>
          </Row>
          <Space>
            <Form.Item name="isPrimaryKey" valuePropName="checked" noStyle>
              <Checkbox>{t("forms.ontology.propertyLabels.isPrimaryKey")}</Checkbox>
            </Form.Item>
            <Form.Item name="isForeignKey" valuePropName="checked" noStyle>
              <Checkbox>{t("forms.ontology.propertyLabels.isForeignKey")}</Checkbox>
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </>
  );
}

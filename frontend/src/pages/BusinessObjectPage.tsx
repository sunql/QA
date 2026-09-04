import { useEffect, useState } from "react";
import {
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  BUSINESS_OBJECT_OPTIONS,
  type BusinessObjectCreate,
  type BusinessObjectRead,
  type BusinessObjectUpdate,
} from "../types/businessObject";
import {
  createBusinessObject,
  deleteBusinessObject,
  listBusinessObjects,
  updateBusinessObject,
} from "../api/businessObject";

const BUSINESS_ENTITY_LABELS = [
  "Supplier",
  "ItemMaster",
  "PurchaseOrder",
  "Receipt",
  "IncomingInspection",
  "Contract",
];

export default function BusinessObjectPage() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<BusinessObjectRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<BusinessObjectRead | null>(null);
  const [form] = Form.useForm();

  const load = async () => {
    setLoading(true);
    try {
      setRows(await listBusinessObjects());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const onCreate = () => {
    setEditing(null);
    form.resetFields();
    setModalOpen(true);
  };

  const onEdit = (row: BusinessObjectRead) => {
    setEditing(row);
    form.setFieldsValue({
      code: row.code,
      name: row.name,
      headerClassId: row.headerClassId ?? undefined,
      graphLabel: row.graphLabel ?? undefined,
      description: row.description ?? undefined,
    });
    setModalOpen(true);
  };

  const onSubmit = async () => {
    const values = await form.validateFields();
    if (editing) {
      const payload: BusinessObjectUpdate = {
        name: values.name,
        headerClassId: values.headerClassId ?? null,
        graphLabel: values.graphLabel ?? null,
        description: values.description ?? null,
      };
      await updateBusinessObject(editing.code, payload);
    } else {
      const payload: BusinessObjectCreate = {
        code: values.code,
        name: values.name,
        headerClassId: values.headerClassId ?? null,
        graphLabel: values.graphLabel ?? null,
        description: values.description ?? null,
      };
      await createBusinessObject(payload);
    }
    setModalOpen(false);
    void load();
  };

  const onDelete = async (code: string) => {
    await deleteBusinessObject(code);
    void load();
  };

  return (
    <div style={{ padding: 24 }}>
      <h2 style={{ marginBottom: 16 }}>{t("businessObject.title")}</h2>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" onClick={onCreate}>
          {t("businessObject.newButton")}
        </Button>
      </Space>
      <Table
        loading={loading}
        dataSource={rows}
        rowKey="code"
        columns={[
          { title: t("businessObject.columns.code"), dataIndex: "code" },
          { title: t("businessObject.columns.name"), dataIndex: "name" },
          {
            title: t("businessObject.columns.graphLabel"),
            dataIndex: "graphLabel",
            render: (v: string | null) => (v ? <Tag>{v}</Tag> : "—"),
          },
          { title: t("businessObject.columns.description"), dataIndex: "description" },
          {
            title: "",
            key: "actions",
            render: (_, row) => (
              <Space>
                <Button size="small" onClick={() => onEdit(row)}>
                  {t("common.edit")}
                </Button>
                <Popconfirm
                  title={t("businessObject.modal.confirmDelete", { code: row.code })}
                  onConfirm={() => onDelete(row.code)}
                >
                  <Button size="small" danger>
                    {t("common.delete")}
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal
        open={modalOpen}
        title={editing ? t("businessObject.modal.editTitle") : t("businessObject.modal.createTitle")}
        onCancel={() => setModalOpen(false)}
        onOk={onSubmit}
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="code"
            label={t("businessObject.columns.code")}
            rules={[{ required: true }]}
          >
            {editing ? (
              <Input disabled />
            ) : (
              <Select options={BUSINESS_OBJECT_OPTIONS.map((c) => ({ value: c, label: c }))} />
            )}
          </Form.Item>
          <Form.Item name="name" label={t("businessObject.columns.name")} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="graphLabel" label={t("businessObject.columns.graphLabel")}>
            <Select
              allowClear
              options={BUSINESS_ENTITY_LABELS.map((l) => ({ value: l, label: l }))}
              placeholder={t("businessObject.placeholders.selectHeaderClass")}
            />
          </Form.Item>
          <Form.Item name="description" label={t("businessObject.columns.description")}>
            <Input.TextArea rows={3} maxLength={4000} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

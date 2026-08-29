import { useEffect, useState, useCallback } from "react";
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
  Popconfirm,
  message,
  Typography,
} from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  listEmbeddingProviders,
  createEmbeddingProvider,
  updateEmbeddingProvider,
  activateEmbeddingProvider,
  deactivateEmbeddingProvider,
} from "../api/embeddingProviders";
import type {
  EmbeddingProvider,
  EmbeddingProviderCreate,
  EmbeddingProviderUpdate,
  EmbeddingProviderType,
} from "../types/embeddingProvider";
import { PROVIDER_TYPE_OPTIONS } from "../types/embeddingProvider";
import { useTranslation } from "../i18n";

const { Title } = Typography;

interface FormValues {
  name: string;
  providerType: EmbeddingProviderType;
  baseUrl: string;
  modelName: string;
  dimension: number;
  apiKey?: string;
}

const EMPTY_FORM: FormValues = {
  name: "",
  providerType: "ollama",
  baseUrl: "",
  modelName: "",
  dimension: 1024,
  apiKey: "",
};

export default function EmbeddingProvidersPage() {
  const { t } = useTranslation();
  const [providers, setProviders] = useState<EmbeddingProvider[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<EmbeddingProvider | null>(null);
  const [form] = Form.useForm<FormValues>();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listEmbeddingProviders();
      setProviders(data);
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
    form.resetFields();
    void form.setFieldsValue(EMPTY_FORM);
    setModalOpen(true);
  };

  const openEdit = (record: EmbeddingProvider) => {
    setEditing(record);
    void form.setFieldsValue({
      name: record.name,
      providerType: record.providerType,
      baseUrl: record.baseUrl,
      modelName: record.modelName,
      dimension: Number(record.dimension),
      apiKey: "",
    });
    setModalOpen(true);
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      if (editing) {
        const payload: EmbeddingProviderUpdate = { ...values };
        // 编辑时不传空 apiKey（留空表示不修改）
        if (!values.apiKey) {
          delete payload.apiKey;
        }
        await updateEmbeddingProvider(editing.id, payload);
        void message.success(t("toast.updated"));
      } else {
        const payload: EmbeddingProviderCreate = {
          name: values.name,
          providerType: values.providerType,
          baseUrl: values.baseUrl,
          modelName: values.modelName,
          dimension: values.dimension,
          apiKey: values.apiKey ?? "",
        };
        await createEmbeddingProvider(payload);
        void message.success(t("toast.created"));
      }
      setModalOpen(false);
      void load();
    } catch (err) {
      if (err instanceof Error && err.message.includes(t("forms.required"))) {
        return; // 表单校验错误，不关闭弹窗
      }
      // 其他错误已由拦截器提示
    }
  };

  const handleActivate = async (id: number) => {
    try {
      await activateEmbeddingProvider(id);
      void message.success(t("toast.enabled"));
      void load();
    } catch {
      // 错误已由拦截器提示
    }
  };

  const handleDeactivate = async (id: number) => {
    try {
      await deactivateEmbeddingProvider(id);
      void message.success(t("toast.disabled"));
      void load();
    } catch {
      // 错误已由拦截器提示
    }
  };

  const columns = [
    { title: t("forms.embeddingProviders.columns.id"), dataIndex: "id", width: 60 },
    { title: t("forms.embeddingProviders.columns.name"), dataIndex: "name" },
    {
      title: t("forms.embeddingProviders.columns.type"),
      dataIndex: "providerType",
      width: 150,
      render: (p: EmbeddingProviderType) =>
        PROVIDER_TYPE_OPTIONS.find((o) => o.value === p)?.label ?? p,
    },
    { title: t("forms.embeddingProviders.columns.baseUrl"), dataIndex: "baseUrl", ellipsis: true },
    { title: t("forms.embeddingProviders.columns.modelName"), dataIndex: "modelName" },
    { title: t("forms.embeddingProviders.columns.dimension"), dataIndex: "dimension", width: 90 },
    {
      title: t("forms.embeddingProviders.columns.status"),
      dataIndex: "isActive",
      width: 90,
      render: (active: boolean) =>
        active ? <Tag color="green">{t("common.enabled")}</Tag> : <Tag color="red">{t("common.disabled")}</Tag>,
    },
    {
      title: t("forms.embeddingProviders.columns.actions"),
      width: 140,
      render: (_: unknown, record: EmbeddingProvider) => (
        <Space>
          <Button size="small" onClick={() => openEdit(record)}>
            {t("common.edit")}
          </Button>
          {record.isActive ? (
            <Popconfirm
              title={t("forms.embeddingProviders.deactivateConfirm")}
              onConfirm={() => handleDeactivate(record.id)}
            >
              <Button size="small" danger>
                {t("common.disabled")}
              </Button>
            </Popconfirm>
          ) : (
            <Button size="small" onClick={() => handleActivate(record.id)}>
              {t("common.enabled")}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          marginBottom: 16,
        }}
      >
        <Title level={4} style={{ margin: 0 }}>
          {t("pages.embeddings")}
        </Title>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={load}>
            {t("common.refresh")}
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            {t("forms.embeddingProviders.addButton")}
          </Button>
        </Space>
      </div>

      <Table
        rowKey="id"
        loading={loading}
        dataSource={providers}
        columns={columns}
        pagination={{ pageSize: 10 }}
      />

      <Modal
        title={editing ? t("forms.embeddingProviders.editModalTitle") : t("forms.embeddingProviders.addModalTitle")}
        open={modalOpen}
        onOk={() => void handleSubmit()}
        onCancel={() => setModalOpen(false)}
        width={560}
        destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={EMPTY_FORM}>
          <Form.Item
            name="name"
            label={t("forms.embeddingProviders.labels.name")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Input placeholder={t("forms.embeddingProviders.placeholders.name")} />
          </Form.Item>
          <Form.Item
            name="providerType"
            label={t("forms.embeddingProviders.labels.providerType")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Select options={PROVIDER_TYPE_OPTIONS} />
          </Form.Item>
          <Form.Item
            name="baseUrl"
            label={t("forms.embeddingProviders.labels.baseUrl")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Input placeholder={t("forms.embeddingProviders.placeholders.baseUrl")} />
          </Form.Item>
          <Form.Item
            name="modelName"
            label={t("forms.embeddingProviders.labels.modelName")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Input placeholder={t("forms.embeddingProviders.placeholders.modelName")} />
          </Form.Item>
          <Space style={{ display: "flex" }} size="middle">
            <Form.Item
              name="dimension"
              label={t("forms.embeddingProviders.labels.dimension")}
              extra={t("forms.embeddingProviders.labels.dimensionHint")}
            >
              <InputNumber min={1} step={128} style={{ width: 160 }} />
            </Form.Item>
            <Form.Item
              name="apiKey"
              label={editing ? t("forms.embeddingProviders.labels.apiKeyKeep") : t("forms.embeddingProviders.labels.apiKey")}
            >
              <Input.Password placeholder={t("forms.embeddingProviders.placeholders.apiKey")} autoComplete="new-password" />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </div>
  );
}

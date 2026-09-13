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
  listModels,
  createModel,
  updateModel,
  deactivateModel,
} from "../api/modelConfig";
import type {
  ModelConfig,
  ModelConfigCreate,
  ModelConfigUpdate,
  ProviderType,
} from "../types/modelConfig";
import { PROVIDER_OPTIONS } from "../types/modelConfig";
import { useTranslation } from "../i18n";

const { Title } = Typography;

interface FormValues {
  modelName: string;
  provider: ProviderType;
  apiEndpoint: string;
  apiKey?: string;
  costPer1KInput: number;
  costPer1KOutput: number;
  maxInputTokens: number;
  weight: number;
  costThreshold: number;
  temperature?: number;
}

const EMPTY_FORM: FormValues = {
  modelName: "",
  provider: "openai_compatible_proxy",
  apiEndpoint: "",
  apiKey: "",
  costPer1KInput: 0,
  costPer1KOutput: 0,
  maxInputTokens: 4096,
  weight: 1,
  costThreshold: 1,
  temperature: undefined,
};

export default function ModelConfigPage() {
  const { t } = useTranslation();
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ModelConfig | null>(null);
  const [form] = Form.useForm<FormValues>();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listModels(false);
      setModels(data);
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

  const openEdit = (record: ModelConfig) => {
    setEditing(record);
    void form.setFieldsValue({
      modelName: record.modelName,
      provider: record.provider,
      apiEndpoint: record.apiEndpoint,
      apiKey: "",
      costPer1KInput: Number(record.costPer1KInput),
      costPer1KOutput: Number(record.costPer1KOutput),
      maxInputTokens: record.maxInputTokens,
      weight: record.weight,
      costThreshold: Number(record.costThreshold),
      temperature: record.temperature,
    });
    setModalOpen(true);
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      if (editing) {
        const payload: ModelConfigUpdate = { ...values };
        // 编辑时不传空 apiKey
        if (!values.apiKey) {
          delete payload.apiKey;
        }
        // 编辑时不传 undefined temperature
        if (values.temperature === undefined) {
          delete payload.temperature;
        }
        await updateModel(editing.id, payload);
        void message.success(t("toast.updated"));
      } else {
        const payload: ModelConfigCreate = {
          modelName: values.modelName,
          provider: values.provider,
          apiEndpoint: values.apiEndpoint,
          apiKey: values.apiKey ?? "",
          costPer1KInput: values.costPer1KInput,
          costPer1KOutput: values.costPer1KOutput,
          maxInputTokens: values.maxInputTokens,
          weight: values.weight,
          costThreshold: values.costThreshold,
        };
        await createModel(payload);
        void message.success(t("toast.created"));
      }
      void load();
      setModalOpen(false);
    } catch (err) {
      if (err instanceof Error && err.message.includes(t("forms.required"))) {
        return; // 表单校验错误，不关闭弹窗
      }
      // 其他错误已由拦截器提示
    }
  };

  const handleCancel = () => {
    setModalOpen(false);
  };

  const handleDeactivate = async (id: number) => {
    try {
      await deactivateModel(id);
      void message.success(t("toast.disabled"));
      void load();
    } catch {
      // 错误已由拦截器提示
    }
  };

  const handleActivate = async (id: number) => {
    const payload: ModelConfigUpdate = { isActive: true };
    await updateModel(id, payload);
    void message.success(t("toast.enabled"));
    void load();
  };

  const providerOptions = PROVIDER_OPTIONS.map((o) => ({
    value: o.value,
    label: t(o.labelKey),
  }));

  const columns = [
    { title: t("forms.modelConfig.columns.id"), dataIndex: "id", width: 60 },
    { title: t("forms.modelConfig.columns.modelName"), dataIndex: "modelName" },
    {
      title: t("forms.modelConfig.columns.provider"),
      dataIndex: "provider",
      width: 180,
      render: (p: ProviderType) =>
        providerOptions.find((o) => o.value === p)?.label ?? p,
    },
    { title: t("forms.modelConfig.columns.apiEndpoint"), dataIndex: "apiEndpoint", ellipsis: true },
    {
      title: t("forms.modelConfig.columns.inputCost"),
      dataIndex: "costPer1KInput",
      width: 120,
      render: (v: number | string) => `$${Number(v).toFixed(4)}`,
    },
    {
      title: t("forms.modelConfig.columns.outputCost"),
      dataIndex: "costPer1KOutput",
      width: 120,
      render: (v: number | string) => `$${Number(v).toFixed(4)}`,
    },
    { title: t("forms.modelConfig.columns.weight"), dataIndex: "weight", width: 80 },
    { title: t("forms.modelConfig.columns.costThreshold"), dataIndex: "costThreshold", width: 100 },
    {
      title: t("forms.modelConfig.columns.status"),
      dataIndex: "isActive",
      width: 80,
      render: (active: boolean) =>
        active ? <Tag color="green">{t("common.enabled")}</Tag> : <Tag color="red">{t("common.disabled")}</Tag>,
    },
    {
      title: t("forms.modelConfig.columns.actions"),
      width: 140,
      render: (_: unknown, record: ModelConfig) => (
        <Space>
          <Button size="small" onClick={() => openEdit(record)}>
            {t("common.edit")}
          </Button>
          {record.isActive ? (
            <Popconfirm
              title={t("forms.modelConfig.deactivateConfirm")}
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
          {t("pages.modelConfig")}
        </Title>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={load}>
            {t("common.refresh")}
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            {t("forms.modelConfig.addButton")}
          </Button>
        </Space>
      </div>

      <Table
        rowKey="id"
        loading={loading}
        dataSource={models}
        columns={columns}
        pagination={{ pageSize: 10 }}
      />

      <Modal
        title={editing ? t("forms.modelConfig.editModalTitle") : t("forms.modelConfig.addModalTitle")}
        open={modalOpen}
        onOk={() => void handleSubmit()}
        onCancel={handleCancel}
        width={560}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" initialValues={EMPTY_FORM}>
          <Form.Item
            name="modelName"
            label={t("forms.modelConfig.labels.modelName")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Input placeholder={t("forms.modelConfig.placeholders.modelName")} />
          </Form.Item>
          <Form.Item
            name="provider"
            label={t("forms.modelConfig.labels.provider")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Select options={providerOptions} />
          </Form.Item>
          <Form.Item
            name="apiEndpoint"
            label={t("forms.modelConfig.labels.apiEndpoint")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Input placeholder={t("forms.modelConfig.placeholders.apiEndpoint")} />
          </Form.Item>
          <Form.Item
            name="apiKey"
            label={editing ? t("forms.modelConfig.labels.apiKeyKeep") : t("forms.modelConfig.labels.apiKey")}
            rules={editing ? [] : [{ required: true, message: t("forms.required") }]}
          >
            <Input.Password placeholder={t("forms.modelConfig.placeholders.apiKey")} autoComplete="new-password" />
          </Form.Item>
          <Space style={{ display: "flex" }} size="middle">
            <Form.Item name="costPer1KInput" label={t("forms.modelConfig.labels.costPer1KInput")}>
              <InputNumber min={0} step={0.0001} style={{ width: 140 }} />
            </Form.Item>
            <Form.Item name="costPer1KOutput" label={t("forms.modelConfig.labels.costPer1KOutput")}>
              <InputNumber min={0} step={0.0001} style={{ width: 140 }} />
            </Form.Item>
          </Space>
          <Space style={{ display: "flex" }} size="middle">
            <Form.Item name="maxInputTokens" label={t("forms.modelConfig.labels.maxInputTokens")}>
              <InputNumber min={1} step={512} style={{ width: 140 }} />
            </Form.Item>
            <Form.Item name="weight" label={t("forms.modelConfig.labels.weight")}>
              <InputNumber min={0} step={1} style={{ width: 140 }} />
            </Form.Item>
            <Form.Item name="costThreshold" label={t("forms.modelConfig.labels.costThreshold")}>
              <InputNumber min={0} step={0.1} style={{ width: 140 }} />
            </Form.Item>
            <Form.Item name="temperature" label={t("forms.modelConfig.labels.temperature")}>
              <InputNumber min={0} max={2} step={0.1} style={{ width: 140 }} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </div>
  );
}
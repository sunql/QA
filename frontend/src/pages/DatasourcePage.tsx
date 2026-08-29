import { useCallback, useEffect, useState } from "react";
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
  Switch,
  message,
  Typography,
} from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  listDataSources,
  createDataSource,
  updateDataSource,
  deleteDataSource,
  testDataSource,
} from "../api/datasource";
import type {
  DataSource,
  DataSourceCreate,
  DataSourceTestRequest,
  DataSourceTestResponse,
  DataSourceType,
  DataSourceUpdate,
} from "../types/datasource";
import {
  DATASOURCE_TYPE_OPTIONS,
  DATASOURCE_DEFAULT_PORTS,
  ORACLE_VERSION_OPTIONS,
} from "../types/datasource";
import { useTranslation } from "../i18n";

const { Title } = Typography;

const TYPE_COLORS: Record<DataSourceType, string> = {
  oracle: "red",
  postgresql: "blue",
  mysql: "green",
};

interface FormValues {
  name: string;
  type: DataSourceType;
  host: string;
  port: number;
  databaseName: string;
  username: string;
  password?: string;
  description?: string;
  isActive: boolean;
  isDefault: boolean;
  oracleVersion: string | null;
}

const EMPTY_FORM: FormValues = {
  name: "",
  type: "postgresql",
  host: "",
  port: 5432,
  databaseName: "",
  username: "",
  password: "",
  description: "",
  isActive: true,
  isDefault: false,
  oracleVersion: null,
};

// 连接测试所需的表单字段
const CONN_FIELDS = ["type", "host", "port", "databaseName", "username", "password"] as const;

export default function DatasourcePage() {
  const { t } = useTranslation();
  const [sources, setSources] = useState<DataSource[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [testing, setTesting] = useState(false);
  const [editing, setEditing] = useState<DataSource | null>(null);
  const [form] = Form.useForm<FormValues>();
  // 监听类型字段，用于条件展示 Oracle 版本下拉
  const currentType = Form.useWatch("type", form);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listDataSources(false);
      setSources(data);
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

  const openEdit = (record: DataSource) => {
    setEditing(record);
    void form.setFieldsValue({
      name: record.name,
      type: record.type,
      host: record.host,
      port: record.port,
      databaseName: record.databaseName,
      username: record.username,
      password: "",
      description: record.description ?? "",
      isActive: record.isActive,
      isDefault: record.isDefault,
      oracleVersion: record.oracleVersion ?? null,
    });
    setModalOpen(true);
  };

  const handleTypeChange = (type: DataSourceType) => {
    void form.setFieldsValue({
      port: DATASOURCE_DEFAULT_PORTS[type],
      // 切换离开 Oracle 时清空版本，避免脏值残留
      oracleVersion: type === "oracle" ? form.getFieldValue("oracleVersion") : null,
    });
  };

  const handleTestConnection = async () => {
    let values: Pick<FormValues, (typeof CONN_FIELDS)[number]>;
    try {
      values = await form.validateFields([...CONN_FIELDS]);
    } catch {
      return; // 表单校验未通过
    }
    if (!values.password) {
      void message.warning(t("toast.pleaseFillPassword"));
      return;
    }
    setTesting(true);
    try {
      const req: DataSourceTestRequest = {
        type: values.type,
        host: values.host,
        port: values.port,
        databaseName: values.databaseName,
        username: values.username,
        password: values.password,
      };
      const res: DataSourceTestResponse = await testDataSource(req);
      if (res.success) {
        void message.success(t("toast.connectSuccess", { message: res.message }));
      } else {
        void message.error(t("toast.connectFailed", { message: res.message }));
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : t("toast.networkError");
      void message.error(t("toast.connectFailed", { message: msg }));
    } finally {
      setTesting(false);
    }
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      if (editing) {
        const payload: DataSourceUpdate = {
          ...values,
          description: values.description || null,
        };
        // 编辑时不传空密码（留空表示不修改）
        if (!values.password) {
          delete payload.password;
        }
        await updateDataSource(editing.id, payload);
        void message.success(t("toast.updated"));
      } else {
        const payload: DataSourceCreate = {
          ...values,
          password: values.password ?? "",
          description: values.description || null,
        };
        await createDataSource(payload);
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

  const handleDelete = async (id: number) => {
    try {
      await deleteDataSource(id);
      void message.success(t("toast.deleted"));
      void load();
    } catch {
      // 错误已由拦截器提示
    }
  };

  const columns = [
    { title: t("forms.datasource.columns.id"), dataIndex: "id", width: 60 },
    { title: t("forms.datasource.columns.name"), dataIndex: "name" },
    {
      title: t("forms.datasource.columns.type"),
      dataIndex: "type",
      width: 110,
      render: (tp: DataSourceType) => <Tag color={TYPE_COLORS[tp]}>{tp.toUpperCase()}</Tag>,
    },
    { title: t("forms.datasource.columns.host"), dataIndex: "host", ellipsis: true },
    { title: t("forms.datasource.columns.port"), dataIndex: "port", width: 80 },
    {
      title: t("forms.datasource.columns.databaseName"),
      dataIndex: "databaseName",
      ellipsis: true,
    },
    { title: t("forms.datasource.columns.username"), dataIndex: "username" },
    {
      title: t("forms.datasource.columns.isDefault"),
      dataIndex: "isDefault",
      width: 80,
      render: (isDefault: boolean) =>
        isDefault ? <Tag color="gold">{t("forms.datasource.columns.default")}</Tag> : null,
    },
    {
      title: t("forms.datasource.columns.status"),
      dataIndex: "isActive",
      width: 80,
      render: (active: boolean) =>
        active ? <Tag color="green">{t("common.enabled")}</Tag> : <Tag color="red">{t("common.disabled")}</Tag>,
    },
    {
      title: t("forms.datasource.columns.actions"),
      width: 150,
      render: (_: unknown, record: DataSource) => (
        <Space>
          <Button size="small" onClick={() => openEdit(record)}>
            {t("common.edit")}
          </Button>
          <Popconfirm
            title={t("forms.datasource.deleteConfirm")}
            onConfirm={() => handleDelete(record.id)}
          >
            <Button size="small" danger>
              {t("common.delete")}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const typeOptions = DATASOURCE_TYPE_OPTIONS.map((o) => ({
    value: o.value,
    label: t(`enums.datasourceType.${o.labelKey}`),
  }));
  const oracleVersionOptions = ORACLE_VERSION_OPTIONS.map((o) => ({
    value: o.value,
    label: t(`enums.oracleVersion.${o.labelKey}`),
  }));

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
          {t("pages.datasource")}
        </Title>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={load}>
            {t("common.refresh")}
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            {t("forms.datasource.addButton")}
          </Button>
        </Space>
      </div>

      <Table
        rowKey="id"
        loading={loading}
        dataSource={sources}
        columns={columns}
        pagination={{ pageSize: 10 }}
      />

      <Modal
        title={editing ? t("forms.datasource.editModalTitle") : t("forms.datasource.addModalTitle")}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        width={560}
        destroyOnHidden
        footer={[
          <Button
            key="test"
            onClick={() => void handleTestConnection()}
            loading={testing}
          >
            {t("forms.datasource.testConnection")}
          </Button>,
          <Button key="cancel" onClick={() => setModalOpen(false)}>
            {t("common.cancel")}
          </Button>,
          <Button key="ok" type="primary" onClick={() => void handleSubmit()}>
            {t("common.confirm")}
          </Button>,
        ]}
      >
        <Form form={form} layout="vertical" initialValues={EMPTY_FORM}>
          <Space style={{ display: "flex" }} size="middle">
            <Form.Item
              name="name"
              label={t("forms.datasource.labels.name")}
              style={{ flex: 1 }}
              rules={[{ required: true, message: t("forms.required") }]}
            >
              <Input placeholder={t("forms.datasource.placeholders.name")} />
            </Form.Item>
            <Form.Item
              name="type"
              label={t("forms.datasource.labels.type")}
              style={{ width: 160 }}
              rules={[{ required: true, message: t("forms.required") }]}
            >
              <Select
                options={typeOptions}
                onChange={(value) => handleTypeChange(value as DataSourceType)}
              />
            </Form.Item>
          </Space>
          <Space style={{ display: "flex" }} size="middle">
            <Form.Item
              name="host"
              label={t("forms.datasource.labels.host")}
              style={{ flex: 1 }}
              rules={[{ required: true, message: t("forms.required") }]}
            >
              <Input placeholder={t("forms.datasource.placeholders.host")} />
            </Form.Item>
            <Form.Item
              name="port"
              label={t("forms.datasource.labels.port")}
              rules={[
                { required: true, message: t("forms.required") },
                { type: "number", min: 1, max: 65535, message: t("forms.datasource.validation.portRange") },
              ]}
            >
              <InputNumber min={1} max={65535} style={{ width: 120 }} />
            </Form.Item>
          </Space>
          <Form.Item
            name="databaseName"
            label={t("forms.datasource.labels.databaseName")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Input placeholder={t("forms.datasource.placeholders.databaseName")} />
          </Form.Item>
          {currentType === "oracle" && (
            <Form.Item
              name="oracleVersion"
              label={t("forms.datasource.labels.oracleVersion")}
              tooltip={t("forms.datasource.tooltips.oracleVersion")}
            >
              <Select
                allowClear
                placeholder={t("forms.datasource.placeholders.oracleVersion")}
                options={oracleVersionOptions}
              />
            </Form.Item>
          )}
          <Space style={{ display: "flex" }} size="middle">
            <Form.Item
              name="username"
              label={t("forms.datasource.labels.username")}
              style={{ flex: 1 }}
              rules={[{ required: true, message: t("forms.required") }]}
            >
              <Input autoComplete="off" />
            </Form.Item>
            <Form.Item
              name="password"
              label={editing ? t("forms.datasource.labels.passwordKeep") : t("forms.datasource.labels.password")}
              style={{ flex: 1 }}
              rules={editing ? [] : [{ required: true, message: t("forms.required") }]}
            >
              <Input.Password
                placeholder={editing ? t("forms.datasource.placeholders.passwordKeep") : t("forms.datasource.placeholders.password")}
                autoComplete="new-password"
              />
            </Form.Item>
          </Space>
          <Form.Item name="description" label={t("forms.datasource.labels.description")}>
            <Input.TextArea rows={2} placeholder={t("forms.datasource.placeholders.description")} />
          </Form.Item>
          <Space size="large">
            <Form.Item name="isActive" label={t("common.enabled")} valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="isDefault" label={t("forms.datasource.setAsDefault")} valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </div>
  );
}
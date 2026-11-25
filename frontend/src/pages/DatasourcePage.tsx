import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Empty,
  Spin,
  Table,
  Button,
  Modal,
  Progress,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Tag,
  Dropdown,
  Switch,
  message,
  Typography,
} from "antd";
import {
  ImportOutlined,
  MoreOutlined,
  PlusOutlined,
  ReloadOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import {
  listDataSources,
  createDataSource,
  updateDataSource,
  deleteDataSource,
  testDataSource,
  startIntrospectJob,
  getIntrospectJob,
  getDatasourceLayers,
} from "../api/datasource";
import type { IntrospectJob } from "../api/datasource";
import type { AppError } from "../api/client";
import type {
  DataSource,
  DataSourceCreate,
  DataSourceLayersRead,
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
import ImportWizard from "../components/localImport/ImportWizard";

const { Title } = Typography;

// 内省 job 轮询：请求成功即视为「正常进行中」，不判超时；仅连续失败才判失联。
const POLL_INTERVAL_MS = 1500;
const MAX_POLL_FAILURES = 5;

interface ActiveJob {
  datasourceId: number;
  jobId: string;
}

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
  const [importingId, setImportingId] = useState<number | null>(null);
  const [introspectingId, setIntrospectingId] = useState<number | null>(null);
  // 数据层分布弹窗（方案 2）：cache-only 只读，未缓存时展示引导提示
  const [layersRecord, setLayersRecord] = useState<DataSource | null>(null);
  const [layersData, setLayersData] = useState<DataSourceLayersRead | null>(null);
  const [layersLoading, setLayersLoading] = useState(false);
  // 缓存 Schema 异步 job：进度弹窗 + 轮询
  const [activeJob, setActiveJob] = useState<ActiveJob | null>(null);
  const [jobProgress, setJobProgress] = useState<IntrospectJob | null>(null);
  const [pollFailures, setPollFailures] = useState(0);
  // 404 = 后端 job 已淘汰（服务重启 / 过期），与「网络失联」区分展示。
  const [jobExpired, setJobExpired] = useState(false);
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

  // 轮询内省进度：只要请求成功就视为「正常进行中」，永不判超时；
  // 仅连续 MAX_POLL_FAILURES 次失败（与服务器失联）才中断。
  // handle 先声明后赋值：tick 可能在 setInterval 之前就跑到 catch 分支，
  // 直接引用 const handle 会命中 TDZ（ReferenceError）。
  useEffect(() => {
    if (!activeJob) return;
    let cancelled = false;
    let stopped = false;
    // 请求超时（30s）远大于轮询间隔（1.5s）：黑洞网络下不设闸会叠出约 20 个在飞请求。
    // in-flight 守卫保证同一时刻最多一个进度请求；stopped 屏蔽终态后的 stale 返回。
    let inFlight = false;
    let failures = 0;
    let handle: number | undefined;
    const stopPolling = () => {
      stopped = true;
      if (handle !== undefined) window.clearInterval(handle);
    };
    const tick = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      try {
        const next = await getIntrospectJob(activeJob.datasourceId, activeJob.jobId);
        // 终态（成功/失败/失效）后仍可能有 stale in-flight 返回：
        // 不得再清空失败计数或重复弹错误。
        if (cancelled || stopped) return;
        failures = 0;
        setPollFailures(0);
        setJobProgress(next);
        if (next.status === "succeeded") {
          stopPolling();
          setActiveJob(null);
          setJobProgress(null);
          void message.success(t("toast.schemaCached", { count: next.total }));
        } else if (next.status === "failed") {
          stopPolling(); // 终态失败：停止轮询，错误留在弹窗里
        }
      } catch (err) {
        if (cancelled || stopped) return;
        // 404 = 后端 job 已被淘汰（服务重启 / 过期），不是网络问题：
        // 不能计成「与服务器连接中断」，改为明确提示重新发起。
        if ((err as AppError)?.status === 404) {
          stopPolling();
          setJobExpired(true);
          void message.error(t("datasource.introspectExpired"));
          return;
        }
        failures += 1;
        setPollFailures(failures);
        if (failures >= MAX_POLL_FAILURES) {
          stopPolling();
          void message.error(t("datasource.introspectConnectionLost"));
        }
      } finally {
        inFlight = false;
      }
    };
    handle = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    void tick();
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [activeJob, t]);

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

  // 手工缓存：启动异步 schema 内省 job，弹窗展示进度并由轮询推进。
  // 同一数据源+schema 已在跑时后端返回既有 jobId（findRunning），
  // 故关闭弹窗后再次点击按钮会拿回同一任务。
  const handleIntrospect = async (record: DataSource) => {
    setIntrospectingId(record.id);
    try {
      const job = await startIntrospectJob(record.id);
      setPollFailures(0);
      setJobExpired(false);
      setJobProgress(job);
      setActiveJob({ datasourceId: record.id, jobId: job.jobId });
    } catch {
      // 错误已由拦截器提示
    } finally {
      setIntrospectingId(null);
    }
  };

  // 数据层分布（方案 2）：cache-only，只读 schema_cache；未探测时展示「先缓存 Schema」引导。
  const handleShowLayers = async (record: DataSource) => {
    setLayersRecord(record);
    setLayersData(null);
    setLayersLoading(true);
    try {
      setLayersData(await getDatasourceLayers(record.id));
    } catch {
      // 加载失败直接收起空弹窗，错误已由拦截器提示
      setLayersRecord(null);
    } finally {
      setLayersLoading(false);
    }
  };

  // 「更多」下拉：数据层分布 / 编辑 / 删除。删除走 Modal.confirm 二次确认（原 Popconfirm 无法置于 Dropdown 菜单内）。
  const handleRowMenu = (record: DataSource, key: string) => {
    if (key === "layers") {
      void handleShowLayers(record);
      return;
    }
    if (key === "edit") {
      openEdit(record);
      return;
    }
    if (key === "delete") {
      Modal.confirm({
        title: t("forms.datasource.deleteConfirm"),
        okText: t("common.confirm"),
        cancelText: t("common.cancel"),
        okButtonProps: { danger: true },
        onOk: () => handleDelete(record.id),
      });
    }
  };

  const columns = [
    { title: t("forms.datasource.columns.id"), dataIndex: "id", width: 60 },
    { title: t("forms.datasource.columns.name"), dataIndex: "name", width: 180, ellipsis: true },
    {
      title: t("forms.datasource.columns.type"),
      dataIndex: "type",
      width: 110,
      render: (tp: DataSourceType) => <Tag color={TYPE_COLORS[tp]}>{tp.toUpperCase()}</Tag>,
    },
    { title: t("forms.datasource.columns.host"), dataIndex: "host", width: 120, ellipsis: true },
    { title: t("forms.datasource.columns.port"), dataIndex: "port", width: 80 },
    {
      title: t("forms.datasource.columns.databaseName"),
      dataIndex: "databaseName",
      width: 110,
      ellipsis: true,
    },
    { title: t("forms.datasource.columns.username"), dataIndex: "username", width: 90, ellipsis: true },
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
      key: "actions",
      fixed: "right" as const,
      width: 300,
      render: (_: unknown, record: DataSource) => (
        <Space size={4}>
          <Button
            size="small"
            icon={<SyncOutlined />}
            loading={introspectingId === record.id}
            onClick={() => void handleIntrospect(record)}
          >
            {t("datasource.cacheSchema")}
          </Button>
          <Button
            size="small"
            icon={<ImportOutlined />}
            onClick={() => setImportingId(record.id)}
          >
            {t("datasource.importToOntology")}
          </Button>
          <Dropdown
            trigger={["click"]}
            menu={{
              items: [
                { key: "layers", label: t("datasource.layers.action") },
                { key: "edit", label: t("common.edit") },
                { key: "delete", label: t("common.delete"), danger: true },
              ],
              onClick: ({ key }) => handleRowMenu(record, key),
            }}
          >
            <Button size="small" icon={<MoreOutlined />} aria-label={t("common.more")} />
          </Dropdown>
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
        scroll={{ x: 1210 }}
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

      <Modal
        title={t("datasource.layers.title", { name: layersRecord?.name ?? "" })}
        open={layersRecord !== null}
        footer={null}
        onCancel={() => setLayersRecord(null)}
        width={520}
      >
        {layersLoading ? (
          <div style={{ textAlign: "center", padding: 24 }}>
            <Spin />
          </div>
        ) : layersData ? (
          <>
            {!layersData.fromCache && (
              <Alert
                type="warning"
                showIcon
                message={t("datasource.layers.notCached")}
                style={{ marginBottom: 12 }}
              />
            )}
            <div style={{ marginBottom: 8 }}>
              {t("datasource.layers.totalTables", { count: layersData.totalTables })}
            </div>
            {layersData.layers.length === 0 ? (
              <Empty description={t("datasource.layers.noLayerData")} />
            ) : (
              <Table
                rowKey="layer"
                size="small"
                pagination={false}
                dataSource={layersData.layers}
                columns={[
                  {
                    title: t("datasource.layers.layer"),
                    dataIndex: "layer",
                    render: (v: string) => <Tag color="blue">{v}</Tag>,
                  },
                  {
                    title: t("datasource.layers.tableCount"),
                    dataIndex: "tableCount",
                    width: 100,
                  },
                ]}
              />
            )}
            {layersData.unknownTables > 0 && (
              <div style={{ marginTop: 12 }}>
                {t("datasource.layers.unknownTables", { count: layersData.unknownTables })}
              </div>
            )}
          </>
        ) : null}
      </Modal>

      <Modal
        open={activeJob !== null}
        title={t("datasource.cacheSchema")}
        onCancel={() => {
          // 允许关闭，后台任务继续；再次点击按钮会拿回同一 jobId（含当前进度）
          setActiveJob(null);
          setJobProgress(null);
          setJobExpired(false);
          setPollFailures(0);
        }}
        footer={null}
      >
        {jobExpired ? (
          <Alert type="error" showIcon message={t("datasource.introspectExpired")} />
        ) : jobProgress?.status === "failed" ? (
          <Alert
            type="error"
            showIcon
            message={jobProgress.error ?? t("datasource.introspectFailed")}
          />
        ) : (
          <>
            {jobProgress?.status === "running" && (jobProgress?.total ?? 0) === 0 ? (
              // 新建 job 的 total 恒为 0（后台任务尚未查到表名列表），此时渲染
              // 「0/0 + 0%」会让人误以为卡死；用不确定态进度条 + 「正在统计」文案。
              <>
                <p>{t("datasource.introspectCounting")}</p>
                <Progress
                  percent={0}
                  status={pollFailures >= MAX_POLL_FAILURES ? "exception" : "active"}
                  showInfo={false}
                />
              </>
            ) : (
              <>
                <p>
                  {t("datasource.introspectProgress", {
                    done: jobProgress?.completed ?? 0,
                    total: jobProgress?.total ?? 0,
                  })}
                </p>
                <Progress
                  percent={
                    jobProgress && jobProgress.total > 0
                      ? Math.round((jobProgress.completed / jobProgress.total) * 100)
                      : 0
                  }
                  // 失联后停掉 active 动画：红色异常态表明进度不再推进。
                  status={pollFailures >= MAX_POLL_FAILURES ? "exception" : "active"}
                />
              </>
            )}
            {pollFailures >= MAX_POLL_FAILURES && (
              <Alert type="warning" showIcon message={t("datasource.introspectConnectionLost")} />
            )}
          </>
        )}
      </Modal>

      <ImportWizard
        key={importingId ?? "none"}
        open={importingId !== null}
        datasourceId={importingId ?? 0}
        onClose={() => setImportingId(null)}
      />
    </div>
  );
}
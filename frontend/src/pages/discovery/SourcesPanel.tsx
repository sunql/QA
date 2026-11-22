/** 发现源管理：登记授权目录 / 采集快照 / 增量监测 / 启停。 */
import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../../i18n";
import { useAuthStore } from "../../stores/authStore";
import {
  captureSnapshot,
  createSource,
  listDatasources,
  listSources,
  monitorSource,
  updateSource,
  type DatasourceOption,
  type DiscoverySource,
} from "../../api/discoveryAdmin";

const STATUS_COLOR: Record<string, string> = { ACTIVE: "success", DISABLED: "default" };

export default function SourcesPanel() {
  const { t } = useTranslation();
  const user = useAuthStore((state) => state.user);
  const isAdmin = (user?.roles ?? []).includes("admin");

  const [rows, setRows] = useState<DiscoverySource[]>([]);
  const [datasources, setDatasources] = useState<DatasourceOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [form] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [sources, dss] = await Promise.all([listSources(), listDatasources()]);
      setRows(sources.rows);
      setDatasources(dss);
    } catch {
      // interceptor 已提示
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const datasourceName = (id: number) =>
    datasources.find((item) => item.id === id)?.name ?? `#${id}`;

  const runAction = async (id: number, action: () => Promise<void>) => {
    setBusyId(id);
    try {
      await action();
      await load();
    } catch {
      // interceptor 已提示
    } finally {
      setBusyId(null);
    }
  };

  const columns: ColumnsType<DiscoverySource> = [
    { title: "ID", dataIndex: "id", width: 70 },
    { title: t("discoveryAdmin.sourceName"), dataIndex: "name" },
    {
      title: t("discoveryAdmin.datasource"),
      dataIndex: "datasourceId",
      width: 180,
      render: (value: number) => datasourceName(value),
    },
    { title: t("discoveryAdmin.owner"), dataIndex: "owner", width: 140 },
    {
      title: t("discoveryAdmin.status"),
      dataIndex: "status",
      width: 100,
      render: (value: string) => <Tag color={STATUS_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    { title: t("discoveryAdmin.snapshots"), dataIndex: "snapshotCount", width: 100 },
    {
      title: t("discoveryAdmin.action"),
      width: 360,
      render: (_, row) => (
        <Space wrap>
          <Button
            size="small"
            disabled={!isAdmin}
            loading={busyId === row.id}
            onClick={() =>
              void runAction(row.id, async () => {
                const result = await captureSnapshot(row.id);
                message.success(
                  t("discoveryAdmin.snapshotCaptured", {
                    id: result.snapshotId,
                    tables: result.tableCount,
                    reused: String(result.reused),
                  }),
                );
              })
            }
          >
            {t("discoveryAdmin.capture")}
          </Button>
          <Button
            size="small"
            disabled={!isAdmin}
            loading={busyId === row.id}
            onClick={() =>
              void runAction(row.id, async () => {
                const result = await monitorSource(row.id);
                message[result.changed ? "success" : "info"](
                  result.changed
                    ? t("discoveryAdmin.monitorChanged", { runId: String(result.runId) })
                    : t("discoveryAdmin.monitorUnchanged"),
                );
              })
            }
          >
            {t("discoveryAdmin.monitor")}
          </Button>
          <Button
            size="small"
            disabled={!isAdmin}
            loading={busyId === row.id}
            onClick={() =>
              void runAction(row.id, async () => {
                await updateSource(row.id, {
                  status: row.status === "ACTIVE" ? "DISABLED" : "ACTIVE",
                });
              })
            }
          >
            {row.status === "ACTIVE" ? t("discoveryAdmin.disable") : t("discoveryAdmin.enable")}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Button onClick={() => void load()}>{t("common.refresh")}</Button>
        {isAdmin && (
          <Button type="primary" onClick={() => setCreateOpen(true)}>
            {t("discoveryAdmin.createSource")}
          </Button>
        )}
      </Space>
      <Table rowKey="id" size="small" loading={loading} dataSource={rows} columns={columns} pagination={{ pageSize: 20 }} />

      <Modal
        title={t("discoveryAdmin.createSource")}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={async () => {
          const values = await form.validateFields();
          try {
            await createSource({
              name: values.name,
              datasourceId: values.datasourceId,
              owner: values.owner,
              accessPolicy: values.schemaOwner
                ? { mode: "READ_ONLY", schema_owner: values.schemaOwner }
                : { mode: "READ_ONLY" },
              note: values.note ?? null,
            });
            message.success(t("discoveryAdmin.opSuccess"));
            setCreateOpen(false);
            form.resetFields();
            await load();
          } catch {
            // interceptor 已提示
          }
        }}
        destroyOnHidden
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t("discoveryAdmin.sourceName")} rules={[{ required: true }]}>
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item name="datasourceId" label={t("discoveryAdmin.datasource")} rules={[{ required: true }]}>
            <Select
              options={datasources
                .filter((item) => item.isActive)
                .map((item) => ({ value: item.id, label: `${item.name} (#${item.id} · ${item.type})` }))}
            />
          </Form.Item>
          <Form.Item name="owner" label={t("discoveryAdmin.owner")} rules={[{ required: true }]}>
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item
            name="schemaOwner"
            label={t("discoveryAdmin.schemaOwner")}
            extra={t("discoveryAdmin.schemaOwnerHint")}
          >
            <Input maxLength={100} placeholder="THBI" />
          </Form.Item>
          <Form.Item name="note" label={t("discoveryAdmin.note")}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

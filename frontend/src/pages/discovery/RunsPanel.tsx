/** 发现运行：触发（选源/快照/表 + 是否生成候选）与进度列表。 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Form,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../../i18n";
import { useAuthStore } from "../../stores/authStore";
import { getSnapshot, listSources, listSnapshots, startRun, type DiscoverySource } from "../../api/discoveryAdmin";
import { listDiscoveryRuns, type DiscoveryRunSummary } from "../../api/discoveryReview";

const STATUS_COLOR: Record<string, string> = { RUNNING: "processing", SUCCEEDED: "success", FAILED: "error" };

export default function RunsPanel() {
  const { t } = useTranslation();
  const user = useAuthStore((state) => state.user);
  const isAdmin = (user?.roles ?? []).includes("admin");

  const [runs, setRuns] = useState<DiscoveryRunSummary[]>([]);
  const [sources, setSources] = useState<DiscoverySource[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [tables, setTables] = useState<Array<{ value: string; label: string }>>([]);
  const [tablesLoading, setTablesLoading] = useState(false);
  const [form] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [runPage, sourcePage] = await Promise.all([listDiscoveryRuns({ limit: 50 }), listSources()]);
      setRuns(runPage.rows);
      setSources(sourcePage.rows);
    } catch {
      // interceptor 已提示
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const hasRunning = useMemo(() => runs.some((row) => row.status === "RUNNING"), [runs]);
  useEffect(() => {
    if (!hasRunning) return;
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [hasRunning, load]);

  const loadTablesFor = async (sourceId: number) => {
    setTablesLoading(true);
    try {
      const snapshots = await listSnapshots({ sourceId, limit: 1 });
      const latest = snapshots.rows[0];
      if (!latest) {
        setTables([]);
        return;
      }
      const detail = await getSnapshot(latest.id);
      const options = (detail.snapshot.tables ?? []).map((table) => ({
        value: table.tableName,
        label: `${table.owner ? `${table.owner}.` : ""}${table.tableName}`,
      }));
      setTables(options);
    } catch {
      setTables([]);
    } finally {
      setTablesLoading(false);
    }
  };

  const columns: ColumnsType<DiscoveryRunSummary> = [
    { title: "ID", dataIndex: "id", width: 70 },
    {
      title: t("discoveryAdmin.status"),
      dataIndex: "status",
      width: 110,
      render: (value: string) => <Tag color={STATUS_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    { title: t("discoveryAdmin.source"), dataIndex: "sourceId", width: 90, render: (value: number) => `#${value}` },
    { title: t("discoveryAdmin.snapshot"), dataIndex: "snapshotId", width: 100, render: (value: number | null) => (value ? `#${value}` : "—") },
    { title: t("discoveryAdmin.model"), dataIndex: "modelVersion", width: 150, render: (value: string | null) => value ?? "—" },
    {
      title: "tokens",
      width: 130,
      render: (_, row) => `${row.promptTokens} / ${row.completionTokens}`,
    },
    { title: "cost", dataIndex: "cost", width: 100, render: (value: number) => value.toFixed(4) },
    { title: t("discoveryAdmin.started"), dataIndex: "startedAt", width: 180, render: (value: string) => new Date(value).toLocaleString() },
    { title: t("discoveryAdmin.error"), dataIndex: "errorMessage", render: (value: string | null) => value ?? "—" },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Button onClick={() => void load()}>{t("common.refresh")}</Button>
        {isAdmin && (
          <Button
            type="primary"
            onClick={() => {
              form.resetFields();
              setTables([]);
              setCreateOpen(true);
            }}
          >
            {t("discoveryAdmin.startRun")}
          </Button>
        )}
      </Space>
      <Table rowKey="id" size="small" loading={loading} dataSource={runs} columns={columns} pagination={{ pageSize: 20 }} />

      <Modal
        title={t("discoveryAdmin.startRun")}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={async () => {
          const values = await form.validateFields();
          try {
            const run = await startRun({
              sourceId: values.sourceId,
              snapshotId: values.snapshotId ?? null,
              tables: values.tables?.length ? values.tables : null,
              withCandidates: Boolean(values.withCandidates),
            });
            message.success(t("discoveryAdmin.runStarted", { id: String(run.id) }));
            setCreateOpen(false);
            form.resetFields();
            setTables([]);
            await load();
          } catch {
            // interceptor 已提示
          }
        }}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" initialValues={{ withCandidates: true }}>
          <Form.Item name="sourceId" label={t("discoveryAdmin.source")} rules={[{ required: true }]}>
            <Select
              options={sources.map((source) => ({
                value: source.id,
                label: `#${source.id} · ${source.name}（${source.status}）`,
              }))}
              onChange={(value: number) => void loadTablesFor(value)}
            />
          </Form.Item>
          <Form.Item
            name="tables"
            label={t("discoveryAdmin.tables")}
            extra={t("discoveryAdmin.tablesHint")}
          >
            <Select mode="multiple" allowClear loading={tablesLoading} options={tables} placeholder={t("discoveryAdmin.tablesPlaceholder")} />
          </Form.Item>
          <Form.Item name="withCandidates" label={t("discoveryAdmin.withCandidates")} valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

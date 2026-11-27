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
import {
  buildDiscoveryTableOptions,
  matchTableOption,
  type DiscoveryTableOption,
} from "../../utils/discoveryTableFilter";
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
  const [tables, setTables] = useState<DiscoveryTableOption[]>([]);
  const [tablesLoading, setTablesLoading] = useState(false);
  /** 被隐藏的「不可剖析」表（Oracle 回收站对象等）—— 只提示，不列出来。 */
  const [hiddenTableNames, setHiddenTableNames] = useState<string[]>([]);
  /** 选择器的搜索词（用于把「按列名命中」标注到选项上）。 */
  const [tableQuery, setTableQuery] = useState("");
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
      // 不可剖析的表（回收站对象等）**不显示** —— 它们必然被剖析护栏拒拼，
      // 显示出来只会让用户选到然后整轮运行失败（见 discoveryTableFilter 注释）。
      const built = buildDiscoveryTableOptions(
        detail.snapshot.tables ?? [],
        detail.unprofilableTables ?? [],
      );
      setTables(built.options);
      setHiddenTableNames(built.hiddenTableNames);
    } catch {
      setTables([]);
      setHiddenTableNames([]);
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
              setHiddenTableNames([]);
              setTableQuery("");
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
            setHiddenTableNames([]);
            setTableQuery("");
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
            extra={
              <>
                {t("discoveryAdmin.tablesHint")}
                {hiddenTableNames.length > 0 && (
                  <div style={{ marginTop: 4 }}>
                    {t("discoveryAdmin.tablesHidden", {
                      count: String(hiddenTableNames.length),
                    })}
                  </div>
                )}
              </>
            }
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              loading={tablesLoading}
              options={tables}
              placeholder={t("discoveryAdmin.tablesPlaceholder")}
              /** 自定义过滤（**替换** antd 默认过滤，不是叠加）：表名 / 属主.表名 /
               *  **列名** 都算命中 ⇒ 能「找出包含某列的表」。 */
              filterOption={(input: string, option) =>
                !option ||
                matchTableOption(
                  option as unknown as DiscoveryTableOption,
                  input,
                ).matched
              }
              onSearch={setTableQuery}
              optionRender={(option) => {
                const matched = option
                  ? matchTableOption(
                      option as unknown as DiscoveryTableOption,
                      tableQuery,
                    )
                  : { matched: true, columnHit: null };
                return (
                  <span>
                    {option.label}
                    {matched.columnHit && (
                      <span style={{ marginLeft: 6, opacity: 0.65 }}>
                        {t("discoveryAdmin.tablesColumnHit", {
                          column: matched.columnHit,
                        })}
                      </span>
                    )}
                  </span>
                );
              }}
            />
          </Form.Item>
          <Form.Item name="withCandidates" label={t("discoveryAdmin.withCandidates")} valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

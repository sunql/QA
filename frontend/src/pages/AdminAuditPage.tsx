/** 审计日志管理页（Phase 4.5 治理 API）。
 *
 * 顶部筛选栏支持 entity_type 下拉 / entity_id 输入 / actor 输入 /
 * actorDepartments 输入 / action 下拉 / since-until 日期范围（RangePicker）。
 * 下方 Table 展示审计日志列表，支持展开行查看 before_json / after_json 变更明细。
 * 分页由 antd Pagination 组件驱动（limit=20 固定）。
 */
import { useEffect, useState, useCallback } from "react";
import {
  Table,
  Button,
  Space,
  DatePicker,
  Input,
  Select,
  Tag,
  Tooltip,
  Pagination,
  Dropdown,
} from "antd";
import type { MenuProps } from "antd";
import { ReloadOutlined, DownloadOutlined } from "@ant-design/icons";
import { listAuditLogs, exportAuditLogs } from "../api/audit";
import type { AuditLog, AuditLogFilters } from "../types/audit";
import { useTranslation } from "../i18n";

const PAGE_SIZE = 20;

const ACTION_OPTIONS = [
  { value: "", label: "" },
  { value: "CREATE", label: "CREATE" },
  { value: "UPDATE", label: "UPDATE" },
  { value: "DELETE", label: "DELETE" },
  { value: "READ", label: "READ" },
];

/** 操作类型 Tag 配色。 */
const ACTION_TAG_COLOR: Record<string, string> = {
  CREATE: "green",
  UPDATE: "blue",
  DELETE: "red",
  READ: "default",
};

interface FilterValues {
  entityType: string;
  entityId: string;
  actor: string;
  actorDepartments: string;
  action: string;
  since: string;
  until: string;
}

const EMPTY_FILTERS: FilterValues = {
  entityType: "",
  entityId: "",
  actor: "",
  actorDepartments: "",
  action: "",
  since: "",
  until: "",
};

const ENTITY_TYPE_OPTIONS = [
  { value: "", label: "" },
  { value: "ONTOLOGY_CLASS", label: "ONTOLOGY_CLASS" },
  { value: "ONTOLOGY_PROPERTY", label: "ONTOLOGY_PROPERTY" },
  { value: "ONTOLOGY_METRIC", label: "ONTOLOGY_METRIC" },
  { value: "ONTOLOGY_JOIN", label: "ONTOLOGY_JOIN" },
  { value: "KPI_CATALOG", label: "KPI_CATALOG" },
  { value: "FEATURE_DEFINITION", label: "FEATURE_DEFINITION" },
  { value: "ENTITY_MAPPING", label: "ENTITY_MAPPING" },
  { value: "DATA_SOURCE", label: "DATA_SOURCE" },
  { value: "AGENT_DEFINITION", label: "AGENT_DEFINITION" },
  { value: "AGENT_TOOL_CONFIG", label: "AGENT_TOOL_CONFIG" },
];

export default function AdminAuditPage() {
  const { t } = useTranslation();
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [loading, setLoading] = useState(false);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState<FilterValues>(EMPTY_FILTERS);
  const [exporting, setExporting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: AuditLogFilters = {
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      };
      if (filters.entityType) params.entityType = filters.entityType;
      if (filters.entityId) params.entityId = filters.entityId;
      if (filters.actor) params.actor = filters.actor;
      if (filters.actorDepartments) params.actorDepartments = filters.actorDepartments;
      if (filters.action) params.action = filters.action;
      if (filters.since) params.since = filters.since;
      if (filters.until) params.until = filters.until;

      const res = await listAuditLogs(params);
      setLogs(res.rows);
      setTotal(res.total);
    } catch {
      // 错误由 axios 拦截器提示
    } finally {
      setLoading(false);
    }
  }, [page, filters]);

  useEffect(() => {
    void load();
  }, [load]);

  const updateFilter = (key: keyof FilterValues, value: string) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
    setPage(1);
  };

  const resetFilters = () => {
    setFilters(EMPTY_FILTERS);
    setPage(1);
  };

  const handleExport = async (format: "csv" | "json") => {
    setExporting(true);
    try {
      const params: AuditLogFilters = {};
      if (filters.entityType) params.entityType = filters.entityType;
      if (filters.entityId) params.entityId = filters.entityId;
      if (filters.actor) params.actor = filters.actor;
      if (filters.actorDepartments) params.actorDepartments = filters.actorDepartments;
      if (filters.action) params.action = filters.action;
      if (filters.since) params.since = filters.since;
      if (filters.until) params.until = filters.until;

      const blob = await exportAuditLogs(params, format);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = format === "csv" ? "audit.csv" : "audit.jsonl";
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setExporting(false);
    }
  };

  const exportMenuItems: MenuProps["items"] = [
    {
      key: "csv",
      label: t("audit.export.csv"),
      onClick: () => handleExport("csv"),
    },
    {
      key: "json",
      label: t("audit.export.json"),
      onClick: () => handleExport("json"),
    },
  ];

  const columns = [
    {
      title: t("audit.columns.id"),
      dataIndex: "id",
      width: 70,
    },
    {
      title: t("audit.columns.entityType"),
      dataIndex: "entityType",
      width: 160,
    },
    {
      title: t("audit.columns.entityId"),
      dataIndex: "entityId",
      width: 90,
    },
    {
      title: t("audit.columns.action"),
      dataIndex: "action",
      width: 90,
      render: (v: string) => (
        <Tag color={ACTION_TAG_COLOR[v] ?? "default"}>{v}</Tag>
      ),
    },
    {
      title: t("audit.columns.actor"),
      dataIndex: "actor",
      width: 140,
      ellipsis: { showTitle: false },
      render: (v: string) => (
        <Tooltip placement="topLeft" title={v}>
          {v}
        </Tooltip>
      ),
    },
    {
      title: t("audit.columns.actorDepartments"),
      dataIndex: "actorDepartments",
      width: 140,
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
    {
      title: t("audit.columns.createdAt"),
      dataIndex: "createdAt",
      width: 180,
      render: (v: string) => {
        const d = new Date(v);
        return isNaN(d.getTime()) ? v : d.toLocaleString();
      },
    },
  ];

  // 展开行：展示 before_json / after_json
  const expandedRowRender = (record: AuditLog) => (
    <div style={{ padding: "0 0 8px 0" }}>
      {record.beforeJson != null && (
        <div style={{ marginBottom: 8 }}>
          <strong>{t("audit.columns.beforeJson")}:</strong>
          <pre
            style={{
              background: "#f5f5f5",
              padding: 8,
              borderRadius: 4,
              marginTop: 4,
              fontSize: 12,
              maxHeight: 200,
              overflow: "auto",
            }}
          >
            {JSON.stringify(record.beforeJson, null, 2)}
          </pre>
        </div>
      )}
      {record.afterJson != null && (
        <div>
          <strong>{t("audit.columns.afterJson")}:</strong>
          <pre
            style={{
              background: "#f5f5f5",
              padding: 8,
              borderRadius: 4,
              marginTop: 4,
              fontSize: 12,
              maxHeight: 200,
              overflow: "auto",
            }}
          >
            {JSON.stringify(record.afterJson, null, 2)}
          </pre>
        </div>
      )}
      {record.beforeJson == null && record.afterJson == null && (
        <span style={{ color: "#999" }}>{t("audit.noChanges")}</span>
      )}
    </div>
  );

  const rangePickerProps = {
    showTime: true as const,
    onChange: (_val: unknown, dateStrings: [string, string]) => {
      updateFilter("since", dateStrings[0] ?? "");
      updateFilter("until", dateStrings[1] ?? "");
    },
  };

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16 }}>
        <Space wrap>
          <Select
            allowClear
            placeholder={t("audit.filters.entityType")}
            style={{ width: 180 }}
            options={ENTITY_TYPE_OPTIONS}
            value={filters.entityType ? filters.entityType : undefined}
            onChange={(v) => updateFilter("entityType", v ?? "")}
          />
          <Input
            allowClear
            placeholder={t("audit.filters.entityId")}
            style={{ width: 160 }}
            value={filters.entityId}
            onChange={(e) => updateFilter("entityId", e.target.value)}
          />
          <Input
            allowClear
            placeholder={t("audit.filters.actor")}
            style={{ width: 160 }}
            value={filters.actor}
            onChange={(e) => updateFilter("actor", e.target.value)}
          />
          <Input
            allowClear
            placeholder={t("audit.filters.actorDepartments")}
            style={{ width: 140 }}
            value={filters.actorDepartments}
            onChange={(e) => updateFilter("actorDepartments", e.target.value)}
          />
          <Select
            allowClear
            placeholder={t("audit.filters.action")}
            style={{ width: 120 }}
            options={ACTION_OPTIONS}
            value={filters.action ? filters.action : undefined}
            onChange={(v) => updateFilter("action", v ?? "")}
          />
          <DatePicker.RangePicker
            {...rangePickerProps}
            placeholder={[t("audit.filters.since"), t("audit.filters.until")]}
            style={{ width: 340 }}
          />
          <Button
            icon={<ReloadOutlined />}
            disabled={
              !filters.entityType &&
              !filters.entityId &&
              !filters.actor &&
              !filters.actorDepartments &&
              !filters.action &&
              !filters.since &&
              !filters.until
            }
            onClick={resetFilters}
          >
            {t("forms.ontology.filter.reset")}
          </Button>
        </Space>
        <Space>
          <Dropdown menu={{ items: exportMenuItems }} trigger={["click"]}>
            <Button icon={<DownloadOutlined />} loading={exporting}>
              {t("audit.export.button")}
            </Button>
          </Dropdown>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            {t("common.refresh")}
          </Button>
        </Space>
      </div>

      <Table
        rowKey="id"
        loading={loading}
        dataSource={logs}
        columns={columns}
        expandable={{
          expandedRowRender,
          columnTitle: t("audit.expandChanges"),
          columnWidth: 60,
        }}
        pagination={false}
      />

      <div style={{ marginTop: 16, textAlign: "right" }}>
        <Pagination
          current={page}
          pageSize={PAGE_SIZE}
          total={total}
          onChange={(p) => setPage(p)}
          showSizeChanger={false}
          showTotal={(tot) => t("audit.total", { total: tot })}
        />
      </div>
    </>
  );
}

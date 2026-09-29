/** 数据质量评估报告 — 列表页（feat-dq-evaluation-report，Phase 5）。
 *
 * 表格列：name / classCount / ruleCount / status / createdBy / timeWindow / actions
 * 筛选：name 输入 + createdBy 输入 + 日期 RangePicker（classId/ruleId 过滤留给后端，由 v2
 *       增强弹窗支持；本 phase 先做最常用三个）。
 * 分页：20/页。
 *
 * 模式参考 `frontend/src/pages/DataQualityPage.tsx`（Rule 列表）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  App,
  Button,
  DatePicker,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import {
  deleteReport,
  listReports,
  regenerateReport,
} from "../api/evaluationReport";
import { listClasses } from "../api/ontology";
import type {
  EvaluationReport,
  ListReportsQuery,
} from "../types/evaluationReport";
import type { OntologyClass } from "../types/ontology";

const { RangePicker } = DatePicker;

interface FilterValues {
  name: string | undefined;
  classId: number | undefined;
  createdBy: string | undefined;
  range: [string, string] | null;
}

const EMPTY_FILTERS: FilterValues = {
  name: undefined,
  classId: undefined,
  createdBy: undefined,
  range: null,
};

function statusColor(status: EvaluationReport["status"]): string {
  switch (status) {
    case "PUBLISHED":
      return "green";
    case "DRAFT":
      return "default";
    default:
      return "default";
  }
}

function formatTimeWindow(start: string, end: string): string {
  const s = start ? new Date(start).toLocaleString() : "—";
  const e = end ? new Date(end).toLocaleString() : "—";
  return `${s} → ${e}`;
}

export default function DataQualityReportListPage() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const [reports, setReports] = useState<EvaluationReport[]>([]);
  const [loading, setLoading] = useState(false);
  const [total, setTotal] = useState(0);
  const [filters, setFilters] = useState<FilterValues>(EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [classes, setClasses] = useState<OntologyClass[]>([]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cs = await listClasses();
        if (!cancelled) setClasses(cs);
      } catch {
        // ignore — class dropdown can be empty
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const params: ListReportsQuery = {
        limit: pageSize,
        offset: (page - 1) * pageSize,
      };
      if (filters.name) params.name = filters.name;
      if (filters.classId !== undefined) params.classId = filters.classId;
      if (filters.createdBy) params.createdBy = filters.createdBy;
      if (filters.range) {
        params.start = filters.range[0];
        params.end = filters.range[1];
      }
      const out = await listReports(params);
      setReports(out.rows);
      setTotal(out.total);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    } finally {
      setLoading(false);
    }
  }, [filters, page, pageSize, message]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleDelete = useCallback(
    (report: EvaluationReport) => {
      Modal.confirm({
        title: t("dataQuality.reports.actions.confirmDelete", {
          name: report.name,
        }),
        okText: t("common.confirm", { defaultValue: "确认" }),
        cancelText: t("common.cancel", { defaultValue: "取消" }),
        okButtonProps: { danger: true },
        onOk: async () => {
          try {
            await deleteReport(report.id);
            void message.success(t("dataQuality.reports.actions.deleteSuccess"));
            void refresh();
          } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : String(err);
            void message.error(msg);
          }
        },
      });
    },
    [message, refresh, t],
  );

  const handleRegenerate = useCallback(
    async (report: EvaluationReport) => {
      try {
        await regenerateReport(report.id);
        void message.success(t("dataQuality.reports.actions.regenerateSuccess"));
        void refresh();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        void message.error(msg);
      }
    },
    [message, refresh, t],
  );

  const columns: ColumnsType<EvaluationReport> = useMemo(
    () => [
      {
        title: t("dataQuality.reports.columns.name"),
        dataIndex: "name",
        key: "name",
        width: 220,
        ellipsis: true,
      },
      {
        title: t("dataQuality.reports.columns.classCount"),
        key: "classCount",
        width: 90,
        render: (_, row) => row.classIds.length,
      },
      {
        title: t("dataQuality.reports.columns.ruleCount"),
        key: "ruleCount",
        width: 90,
        render: (_, row) => row.ruleIds.length,
      },
      {
        title: t("dataQuality.reports.columns.status"),
        dataIndex: "status",
        key: "status",
        width: 110,
        render: (s: EvaluationReport["status"]) => (
          <Tag color={statusColor(s)}>
            {t(`dataQuality.reports.statusLabels.${s}`)}
          </Tag>
        ),
      },
      {
        title: t("dataQuality.reports.columns.createdBy"),
        dataIndex: "createdBy",
        key: "createdBy",
        width: 130,
      },
      {
        title: t("dataQuality.reports.columns.timeWindow"),
        key: "timeWindow",
        width: 280,
        render: (_, row) =>
          formatTimeWindow(row.timeWindowStart, row.timeWindowEnd),
      },
      {
        title: t("dataQuality.reports.columns.createdTime"),
        dataIndex: "createdTime",
        key: "createdTime",
        width: 180,
        render: (v: string) =>
          v ? new Date(v).toLocaleString() : "—",
      },
      {
        title: t("dataQuality.reports.columns.actions"),
        key: "actions",
        width: 260,
        fixed: "right",
        render: (_, row) => (
          <Space>
            <Button
              type="link"
              onClick={() => navigate(`/data-quality/reports/${row.id}`)}
            >
              {t("dataQuality.reports.actions.view")}
            </Button>
            <Button type="link" onClick={() => void handleRegenerate(row)}>
              {t("dataQuality.reports.actions.regenerate")}
            </Button>
            <Button
              type="link"
              danger
              onClick={() => handleDelete(row)}
            >
              {t("dataQuality.reports.actions.delete")}
            </Button>
          </Space>
        ),
      },
    ],
    [handleDelete, handleRegenerate, navigate, t],
  );

  const classOptions = useMemo(
    () =>
      classes.map((c) => ({
        label: c.className,
        value: c.id,
      })),
    [classes],
  );

  return (
    <div style={{ padding: 24 }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
        }}
      >
        <h2 style={{ margin: 0 }}>{t("dataQuality.reports.title")}</h2>
        <Button
          type="primary"
          onClick={() => navigate("/data-quality/reports/new")}
        >
          {t("dataQuality.reports.newReport")}
        </Button>
      </div>

      <Form
        layout="inline"
        style={{ marginBottom: 16 }}
        onValuesChange={(_, all: FilterValues) => {
          setFilters({
            ...all,
            range: all.range ? [all.range[0], all.range[1]] : null,
          });
          setPage(1);
        }}
        initialValues={EMPTY_FILTERS}
      >
        <Form.Item name="name">
          <Input
            placeholder={t("dataQuality.reports.filter.name")}
            allowClear
          />
        </Form.Item>
        <Form.Item name="classId">
          <Select
            placeholder={t("dataQuality.reports.filter.classId")}
            allowClear
            options={classOptions}
            style={{ minWidth: 180 }}
            showSearch
            optionFilterProp="label"
          />
        </Form.Item>
        <Form.Item name="createdBy">
          <Input
            placeholder={t("dataQuality.reports.filter.createdBy")}
            allowClear
          />
        </Form.Item>
        <Form.Item name="range">
          <RangePicker
            placeholder={[
              t("dataQuality.reports.filter.dateRange"),
              t("dataQuality.reports.filter.dateRange"),
            ]}
          />
        </Form.Item>
      </Form>

      <Table<EvaluationReport>
        rowKey="id"
        dataSource={reports}
        columns={columns}
        loading={loading}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          onChange: (p, ps) => {
            setPage(p);
            setPageSize(ps);
          },
        }}
        locale={{
          emptyText: t("dataQuality.reports.empty"),
        }}
      />
    </div>
  );
}
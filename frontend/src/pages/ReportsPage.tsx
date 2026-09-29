/**
 * M4 Report 模板 MVP（A8）——「我的报告」轻量版页面。
 *
 * 功能面：
 * - 模板选择器（GET /reports/templates）+ 按 paramsSchema 动态渲染参数表单
 * - 同步生成（POST /reports/generate）→ 落库即 PENDING_REVIEW
 * - 报告列表：状态徽标（PENDING_REVIEW=orange / APPROVED=green / REJECTED=red）
 * - 报告详情 Drawer：table → antd Table；kpi_cards → 卡片行；总结按
 *   [事实]/[推断]/[假设] 前缀着色（蓝图 §21 硬约束 4）
 * - admin 审批弹窗（APPROVE/REJECT + note，对齐 audit_history admin-only ACL）
 *
 * 可见性由后端收敛（非 admin 只看自己的 + 全员 APPROVED），前端只按返回渲染。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Drawer,
  Form,
  Input,
  Modal,
  Radio,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import { useAuthStore } from "../stores/authStore";
import {
  generateReport,
  getReport,
  listReports,
  listReportTemplates,
  reviewReport,
} from "../api/reports";
import {
  REPORT_STATUS_META,
  type ReportInstance,
  type ReportListItem,
  type ReportSection,
  type ReportTemplate,
  type ReviewDecision,
} from "../types/reports";

const { Title, Paragraph, Text } = Typography;

const SUMMARY_PREFIX_COLORS: Record<string, string> = {
  "[事实]": "green",
  "[推断]": "orange",
  "[假设]": "purple",
};

const SUMMARY_PREFIX_FALLBACK = "[推断]";

function formatTime(value: string | null): string {
  if (!value) {
    return "-";
  }
  const d = new Date(value);
  return isNaN(d.getTime()) ? value : d.toLocaleString();
}

function summaryPrefixOf(line: string): string {
  for (const prefix of Object.keys(SUMMARY_PREFIX_COLORS)) {
    if (line.startsWith(prefix)) {
      return prefix;
    }
  }
  return SUMMARY_PREFIX_FALLBACK;
}

/** 分节渲染：table → Table；kpi_cards → 卡片行（蓝图 §21 硬约束 4/5）。 */
function SectionBlock({ section }: { section: ReportSection }) {
  const { t } = useTranslation();
  const rows = useMemo(
    () => (Array.isArray(section.data) ? section.data : []),
    [section.data],
  );

  const tableColumns: ColumnsType<Record<string, unknown>> = useMemo(() => {
    const keys = rows.length > 0 ? Object.keys(rows[0]) : [];
    return keys.map((key) => ({
      title: key,
      dataIndex: key,
      key,
      render: (value: unknown) =>
        value === null || value === undefined || value === "" ? "-" : String(value),
    }));
  }, [rows]);

  const body = (() => {
    if (section.renderError) {
      return (
        <Alert
          type="warning"
          showIcon
          message={t("reportsPage.section.renderError")}
          description={section.renderError}
        />
      );
    }
    if (section.kind === "kpi_cards") {
      return (
        <Row gutter={[12, 12]}>
          {rows.map((row, idx) => (
            <Col xs={24} sm={12} md={8} key={idx}>
              <Card size="small" title={String(row.kpiName ?? row.featureName ?? row.kpiCode ?? idx)}>
                <Text strong style={{ fontSize: 18 }}>
                  {String(row.valueText ?? row.value ?? row.unit ?? "-")}
                </Text>
                {row.unit ? <Text type="secondary"> {String(row.unit)}</Text> : null}
                {row.found === false ? (
                  <div>
                    <Text type="warning">{t("reportsPage.section.notFound")}</Text>
                  </div>
                ) : null}
              </Card>
            </Col>
          ))}
          {rows.length === 0 ? (
            <Text type="secondary">{t("reportsPage.section.empty")}</Text>
          ) : null}
        </Row>
      );
    }
    if (rows.length === 0) {
      return <Text type="secondary">{t("reportsPage.section.empty")}</Text>;
    }
    return (
      <Table<Record<string, unknown>>
        size="small"
        rowKey={(row) => Object.values(row).map((v) => String(v)).join("|")}
        columns={tableColumns}
        dataSource={rows}
        pagination={false}
      />
    );
  })();

  return (
    <div style={{ marginBottom: 24 }}>
      <Title level={5} style={{ marginTop: 0 }}>
        {section.title}
      </Title>
      {body}
    </div>
  );
}

/** 总结块：逐行按 [事实]/[推断]/[假设] 前缀着色；解析不出前缀按 [推断] 兜底。 */
function SummaryBlock({ summary }: { summary: string }) {
  const { t } = useTranslation();
  const lines = summary.split("\n").filter((line) => line.trim().length > 0);
  return (
    <div style={{ marginBottom: 8 }}>
      <Title level={5} style={{ marginTop: 0 }}>
        {t("reportsPage.detail.summary")}
      </Title>
      {lines.map((line, idx) => {
        const prefix = summaryPrefixOf(line);
        // 仅对自带前缀的行剥前缀；无前缀行（兜底 [推断]）保留全文
        const body = line.startsWith(prefix) ? line.slice(prefix.length).trim() : line;
        return (
          <div key={idx} style={{ marginBottom: 4 }}>
            <Tag color={SUMMARY_PREFIX_COLORS[prefix]}>{prefix}</Tag>
            <Text>{body}</Text>
          </div>
        );
      })}
    </div>
  );
}

export default function ReportsPage() {
  const { t } = useTranslation();
  const user = useAuthStore((s) => s.user);
  const isAdmin = (user?.roles ?? []).includes("admin");

  const [templates, setTemplates] = useState<ReportTemplate[]>([]);
  const [templateCode, setTemplateCode] = useState<string | undefined>();
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [rows, setRows] = useState<ReportListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [listLoading, setListLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [detail, setDetail] = useState<ReportInstance | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [reviewTarget, setReviewTarget] = useState<ReportListItem | null>(null);
  const [reviewDecision, setReviewDecision] = useState<ReviewDecision>("APPROVE");
  const [reviewNote, setReviewNote] = useState("");
  const [reviewing, setReviewing] = useState(false);

  const selectedTemplate = useMemo(
    () => templates.find((tpl) => tpl.code === templateCode),
    [templates, templateCode],
  );

  const refreshList = useCallback(async () => {
    setListLoading(true);
    try {
      const page = await listReports({ limit: 50 });
      setRows(page.rows);
      setTotal(page.total);
    } catch {
      message.error(t("reportsPage.list.loadFailed"));
    } finally {
      setListLoading(false);
    }
  }, [t]);

  useEffect(() => {
    let cancelled = false;
    listReportTemplates()
      .then((tpls) => {
        if (cancelled) {
          return;
        }
        setTemplates(tpls);
        if (tpls.length > 0) {
          setTemplateCode(tpls[0].code);
        }
      })
      .catch(() => message.error(t("reportsPage.templates.loadFailed")));
    refreshList();
    return () => {
      cancelled = true;
    };
  }, [refreshList, t]);

  const handleGenerate = useCallback(async () => {
    if (!templateCode) {
      return;
    }
    const missing = (selectedTemplate?.paramsSchema ?? [])
      .filter((field) => field.required && !paramValues[field.name]?.trim())
      .map((field) => field.name);
    if (missing.length > 0) {
      message.warning(
        t("reportsPage.generate.missingParams", { fields: missing.join(", ") }),
      );
      return;
    }
    setGenerating(true);
    try {
      const instance = await generateReport({ templateCode, params: paramValues });
      message.success(t("reportsPage.generate.success", { id: instance.id }));
      await refreshList();
      const full = await getReport(instance.id);
      setDetail(full);
    } catch {
      message.error(t("reportsPage.generate.failed"));
    } finally {
      setGenerating(false);
    }
  }, [templateCode, selectedTemplate, paramValues, refreshList, t]);

  const openDetail = useCallback(async (id: number) => {
    setDetailLoading(true);
    try {
      setDetail(await getReport(id));
    } catch {
      message.error(t("reportsPage.detail.loadFailed"));
    } finally {
      setDetailLoading(false);
    }
  }, [t]);

  const submitReview = useCallback(async () => {
    if (!reviewTarget) {
      return;
    }
    setReviewing(true);
    try {
      await reviewReport(reviewTarget.id, {
        decision: reviewDecision,
        note: reviewNote.trim() || null,
      });
      message.success(t("reportsPage.review.success"));
      setReviewTarget(null);
      setReviewNote("");
      await refreshList();
    } catch {
      message.error(t("reportsPage.review.failed"));
    } finally {
      setReviewing(false);
    }
  }, [reviewTarget, reviewDecision, reviewNote, refreshList, t]);

  const columns: ColumnsType<ReportListItem> = [
    { title: t("reportsPage.list.colId"), dataIndex: "id", key: "id", width: 90 },
    { title: t("reportsPage.list.colTitle"), dataIndex: "title", key: "title" },
    {
      title: t("reportsPage.list.colStatus"),
      dataIndex: "status",
      key: "status",
      width: 140,
      render: (status: ReportListItem["status"]) => (
        <Tag color={REPORT_STATUS_META[status].color}>
          {t(REPORT_STATUS_META[status].i18nKey)}
        </Tag>
      ),
    },
    {
      title: t("reportsPage.list.colCreatedBy"),
      dataIndex: "createdBy",
      key: "createdBy",
      width: 120,
    },
    {
      title: t("reportsPage.list.colCreatedTime"),
      dataIndex: "createdTime",
      key: "createdTime",
      width: 180,
      render: formatTime,
    },
    {
      title: t("reportsPage.list.colActions"),
      key: "actions",
      width: 160,
      render: (_value, record) => (
        <Space>
          <Button size="small" onClick={() => void openDetail(record.id)}>
            {t("reportsPage.list.view")}
          </Button>
          {isAdmin && record.status === "PENDING_REVIEW" ? (
            <Button
              size="small"
              type="primary"
              onClick={() => {
                setReviewTarget(record);
                setReviewDecision("APPROVE");
                setReviewNote("");
              }}
            >
              {t("reportsPage.list.review")}
            </Button>
          ) : null}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card size="small">
        <Title level={4} style={{ marginTop: 0 }}>
          {t("reportsPage.title")}
        </Title>
        <Paragraph type="secondary">{t("reportsPage.hint")}</Paragraph>
        <Form layout="inline" onFinish={() => void handleGenerate()}>
          <Form.Item label={t("reportsPage.generate.template")}>
            <Select
              style={{ minWidth: 220 }}
              value={templateCode}
              onChange={(code) => {
                setTemplateCode(code);
                setParamValues({});
              }}
              options={templates.map((tpl) => ({
                value: tpl.code,
                label: `${tpl.title}（${tpl.code}）`,
              }))}
            />
          </Form.Item>
          {(selectedTemplate?.paramsSchema ?? []).map((field) => (
            <Form.Item key={field.name} label={field.name} htmlFor={`report-param-${field.name}`}>
              <Input
                id={`report-param-${field.name}`}
                style={{ width: 160 }}
                value={paramValues[field.name] ?? ""}
                placeholder={field.pattern ?? undefined}
                onChange={(e) =>
                  setParamValues((prev) => ({
                    ...prev,
                    [field.name]: e.target.value,
                  }))
                }
              />
            </Form.Item>
          ))}
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={generating}>
              {t("reportsPage.generate.submit")}
            </Button>
          </Form.Item>
        </Form>
      </Card>

      <Card size="small">
        <Table<ReportListItem>
          rowKey="id"
          size="small"
          loading={listLoading}
          columns={columns}
          dataSource={rows}
          pagination={{ total, pageSize: 50, showTotal: (n) => t("reportsPage.list.total", { n }) }}
        />
      </Card>

      <Drawer
        title={detail?.title ?? t("reportsPage.detail.title")}
        width={720}
        open={detail !== null}
        loading={detailLoading}
        onClose={() => setDetail(null)}
      >
        {detail ? (
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            <div>
              <Tag color={REPORT_STATUS_META[detail.status].color}>
                {t(REPORT_STATUS_META[detail.status].i18nKey)}
              </Tag>
              <Text type="secondary">
                {t("reportsPage.detail.meta", {
                  template: detail.templateCode,
                  creator: detail.createdBy,
                  time: formatTime(detail.createdTime),
                })}
              </Text>
              {detail.reviewNote ? (
                <div style={{ marginTop: 8 }}>
                  <Text type="secondary">
                    {t("reportsPage.detail.reviewNote", {
                      by: detail.reviewedBy ?? "-",
                      note: detail.reviewNote,
                    })}
                  </Text>
                </div>
              ) : null}
            </div>
            {detail.sections.map((section) => (
              <SectionBlock key={section.sectionId} section={section} />
            ))}
            {detail.summary ? <SummaryBlock summary={detail.summary} /> : null}
          </Space>
        ) : null}
      </Drawer>

      <Modal
        title={t("reportsPage.review.title", { id: reviewTarget?.id ?? "" })}
        open={reviewTarget !== null}
        confirmLoading={reviewing}
        onOk={() => void submitReview()}
        onCancel={() => setReviewTarget(null)}
        okText={t("reportsPage.review.submit")}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Radio.Group
            value={reviewDecision}
            onChange={(e) => setReviewDecision(e.target.value as ReviewDecision)}
          >
            <Radio.Button value="APPROVE">
              {t("reportsPage.review.approve")}
            </Radio.Button>
            <Radio.Button value="REJECT">{t("reportsPage.review.reject")}</Radio.Button>
          </Radio.Group>
          <Input.TextArea
            rows={3}
            value={reviewNote}
            placeholder={t("reportsPage.review.notePlaceholder") as string}
            onChange={(e) => setReviewNote(e.target.value)}
          />
        </Space>
      </Modal>
    </Space>
  );
}

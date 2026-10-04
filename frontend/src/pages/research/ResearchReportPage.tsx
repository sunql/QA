/** 研究报告页（feat-research-entry Task 10）。
 *
 * 拉取报告 payload（+ 版本列表），按版本点选切换；payload 的 JSONB 形状由
 * backend report_planner.py 定义（camelCase），经 ReportRenderer 只读渲染。
 */
import { useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Button, Empty, Space, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { useResearchStore } from "../../stores/researchStore";
import { ReportRenderer, parseReportPayload } from "../../components/research/ReportRenderer";
import type { ReportPayload } from "../../components/research/ReportRenderer";

export default function ResearchReportPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const report = useResearchStore((s) => s.report);
  const reports = useResearchStore((s) => s.reports);
  const loadReport = useResearchStore((s) => s.loadReport);
  const loadReports = useResearchStore((s) => s.loadReports);

  useEffect(() => {
    if (!id) return;
    void loadReports(id);
    void loadReport(id);
  }, [id, loadReports, loadReport]);

  const payload: ReportPayload | null = report ? parseReportPayload(report.payload) : null;

  return (
    <div style={{ padding: 16 }}>
      <Space style={{ marginBottom: 16 }}>
        <Button onClick={() => id && navigate(`/research/${id}`)}>
          {t("research.report.back")}
        </Button>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {payload?.title || t("research.report.title")}
        </Typography.Title>
      </Space>
      {reports.length > 0 ? (
        <Space wrap style={{ marginBottom: 16 }}>
          {reports.map((summary) => (
            <Tag
              key={summary.id}
              color={summary.status === "published" ? "blue" : "default"}
              style={{ cursor: "pointer" }}
              onClick={() => id && loadReport(id, summary.version)}
            >
              {t("research.report.versions")} {summary.version}
            </Tag>
          ))}
        </Space>
      ) : null}
      {payload ? (
        <ReportRenderer payload={payload} />
      ) : (
        <Empty description={t("research.report.empty")} />
      )}
    </div>
  );
}

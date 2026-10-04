/** 研究对比视图（feat-research-entry Task 10 空壳，Task 12 填充）。
 *
 * 本任务只落 props 契约：sessionIds 指定要并排对比的会话集合。渲染留 Task 12
 * （拉取各会话报告 payload 后并排渲染 ReportRenderer）。
 */
import { Empty } from "antd";
import { useTranslation } from "react-i18next";

export interface ResearchCompareViewProps {
  sessionIds?: string[];
}

export function ResearchCompareView({ sessionIds = [] }: ResearchCompareViewProps) {
  const { t } = useTranslation();
  const label =
    sessionIds.length > 0 ? t("research.compare.title") : t("research.compare.empty");
  return <Empty description={label} />;
}

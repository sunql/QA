/** 本体发现（发现源 → 快照 → 运行 → 评审）三段式工作台。 */
import { Tabs } from "antd";
import { useTranslation } from "../i18n";
import SourcesPanel from "./discovery/SourcesPanel";
import RunsPanel from "./discovery/RunsPanel";
import DiscoveryReviewPage from "./DiscoveryReviewPage";

export default function OntologyDiscoveryPage() {
  const { t } = useTranslation();

  return (
    <div style={{ padding: 24 }}>
      <h2>{t("discoveryAdmin.title")}</h2>
      <Tabs
        items={[
          { key: "sources", label: t("discoveryAdmin.tabSources"), children: <SourcesPanel /> },
          { key: "runs", label: t("discoveryAdmin.tabRuns"), children: <RunsPanel /> },
          { key: "review", label: t("discoveryAdmin.tabReview"), children: <DiscoveryReviewPage /> },
        ]}
      />
    </div>
  );
}

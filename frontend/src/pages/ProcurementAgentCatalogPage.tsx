/** M4-3 采购场景目录页（消费 M3-3 catalog API）。
 *
 * 设计稿 §8.4：场景 + 可用工具 + 能力说明 + 可运行性——**不暴露 handlerRef**
 * （后端目录本就不含，前端纯展示）。
 */

import { useCallback, useEffect, useState } from "react";
import { Alert, Card, Space, Spin, Tag } from "antd";
import { useTranslation } from "../i18n";
import {
  getAgentCatalog,
  type AgentCatalogEntry,
} from "../api/procurementKnowledge";

export default function ProcurementAgentCatalogPage() {
  const { t } = useTranslation();
  const [entries, setEntries] = useState<AgentCatalogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setEntries(await getAgentCatalog());
    } catch (err) {
      setLoadError(
        err instanceof Error ? err.message : t("procurement.catalog.loadFailed"),
      );
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return (
    <div style={{ padding: 24 }}>
      <h2>{t("procurement.catalog.title")}</h2>
      {loadError && (
        <Alert type="error" message={loadError} style={{ marginBottom: 12 }} showIcon />
      )}
      <Spin spinning={loading}>
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          {entries.map((e) => (
            <Card
              key={e.agentCode}
              size="small"
              title={
                <Space>
                  {e.agentName}
                  <Tag color={e.runnable ? "success" : "default"}>
                    {e.runnable
                      ? t("procurement.catalog.runnable")
                      : t("procurement.catalog.notRunnable")}
                  </Tag>
                  <Tag>{e.status}</Tag>
                </Space>
              }
            >
              <p>{e.description ?? "-"}</p>
              {e.tool != null && (
                <p>
                  <Tag color="blue">{e.tool.name}</Tag>
                  {e.tool.description ?? ""}
                </p>
              )}
            </Card>
          ))}
          {!loading && entries.length === 0 && !loadError && (
            <Alert type="info" message={t("procurement.catalog.empty")} showIcon />
          )}
        </Space>
      </Spin>
    </div>
  );
}

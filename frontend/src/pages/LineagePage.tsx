/** LineagePage — 血缘可视化主页（Phase 2.3）。
 *
 * 流程：
 * 1. mount → listEdges(activeOnly=true) 拉取所有活跃边
 * 2. LayerFilter 维护 selectedLayers（默认 7 层全选）
 * 3. 按层过滤 edges 后传给 LineageGraph 渲染
 * 4. 错误显示 message.error
 * 5. 空数据显示「暂无血缘」空状态
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Empty, Space, Spin, message } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { useTranslation } from "../i18n";
import { listEdges } from "../api/lineage";
import type { LineageEdgeRead, LineageLayer } from "../types/lineage";
import LayerFilter, { ALL_LAYERS } from "../components/lineage/LayerFilter";
import LineageGraph from "../components/lineage/LineageGraph";

function errorMessageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

export default function LineagePage() {
  const { t } = useTranslation();
  const [edges, setEdges] = useState<LineageEdgeRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedLayers, setSelectedLayers] = useState<Set<LineageLayer>>(
    () => new Set(ALL_LAYERS),
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const data = await listEdges({ activeOnly: true });
      setEdges(data);
    } catch (error: unknown) {
      const msg = errorMessageOf(error);
      setLoadError(msg);
      message.error(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const filteredEdges = useMemo(
    () =>
      edges.filter(
        (e) => selectedLayers.has(e.sourceLayer) && selectedLayers.has(e.targetLayer),
      ),
    [edges, selectedLayers],
  );

  return (
    <div style={{ padding: 16 }}>
      <Card
        title={t("lineage.page.title")}
        extra={
          <Space>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => void refresh()}
              loading={loading}
            >
              {t("common.refresh")}
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <div>
            <strong style={{ marginRight: 8 }}>{t("lineage.filter.layers")}:</strong>
            <LayerFilter value={selectedLayers} onChange={setSelectedLayers} />
          </div>
          <div style={{ color: "#666", fontSize: 12 }}>
            {t("lineage.filter.summary", {
              selected: selectedLayers.size,
              total: ALL_LAYERS.length,
              edges: filteredEdges.length,
              allEdges: edges.length,
            })}
          </div>
          {loadError ? (
            <Alert type="error" message={loadError} showIcon />
          ) : null}
          <Spin spinning={loading}>
            {filteredEdges.length > 0 ? (
              <LineageGraph edges={filteredEdges} height={620} />
            ) : (
              <Empty
                description={
                  edges.length === 0
                    ? t("lineage.empty.noData")
                    : t("lineage.empty.filteredOut")
                }
              />
            )}
          </Spin>
        </Space>
      </Card>
    </div>
  );
}

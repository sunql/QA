/** LineagePage — 血缘可视化主页（Phase 2.3 + Step 5 对象级筛选）。
 *
 * 流程：
 * 1. mount → listEdges(activeOnly=true) 拉取所有活跃边
 * 2. LayerFilter 维护 selectedLayers（默认 7 层全选）
 * 3. 按层过滤 edges → collectObjectCandidates 派生对象候选 → ObjectFilter
 * 4. 对象选择（复合键 layer/object）进一步过滤：仅保留触及选中对象的边
 * 5. effectiveSelected 级联裁剪：取消某层 → 该层对象选择自动失效
 * 6. 错误显示 message.error；空数据显示「暂无血缘」空状态
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Empty, Space, Spin, message } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { useTranslation } from "../i18n";
import { listEdges } from "../api/lineage";
import type { LineageEdgeRead, LineageLayer } from "../types/lineage";
import LayerFilter, { ALL_LAYERS } from "../components/lineage/LayerFilter";
import ObjectFilter from "../components/lineage/ObjectFilter";
import {
  collectObjectCandidates,
  filterEdgesByObjects,
  objectKey,
} from "../components/lineage/lineageFilter";
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
  const [selectedObjects, setSelectedObjects] = useState<Set<string>>(new Set());

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

  const layerFilteredEdges = useMemo(
    () =>
      edges.filter(
        (e) => selectedLayers.has(e.sourceLayer) && selectedLayers.has(e.targetLayer),
      ),
    [edges, selectedLayers],
  );

  // 对象候选：来自按层过滤后的 edges（随层筛选联动）
  const objectCandidates = useMemo(
    () => collectObjectCandidates(layerFilteredEdges),
    [layerFilteredEdges],
  );
  const candidateKeys = useMemo(
    () => new Set(objectCandidates.map((c) => objectKey(c.layer, c.object))),
    [objectCandidates],
  );

  // 级联裁剪：选中对象必须仍属于当前层过滤后的候选（取消某层 → 该层对象选择失效）
  const effectiveSelected = useMemo(
    () => new Set([...selectedObjects].filter((k) => candidateKeys.has(k))),
    [selectedObjects, candidateKeys],
  );

  const filteredEdges = useMemo(
    () => filterEdgesByObjects(layerFilteredEdges, effectiveSelected),
    [layerFilteredEdges, effectiveSelected],
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
          <div>
            <strong style={{ marginRight: 8 }}>{t("lineage.filter.objects")}:</strong>
            <ObjectFilter
              candidates={objectCandidates}
              value={effectiveSelected}
              onChange={setSelectedObjects}
            />
          </div>
          <div style={{ color: "#666", fontSize: 12 }}>
            {t("lineage.filter.summary", {
              selected: selectedLayers.size,
              total: ALL_LAYERS.length,
              objects: effectiveSelected.size,
              edges: filteredEdges.length,
              allEdges: edges.length,
            })}
          </div>
          {loadError ? (
            <Alert type="error" message={loadError} showIcon />
          ) : null}
          <Spin spinning={loading}>
            {filteredEdges.length > 0 ? (
              <LineageGraph edges={filteredEdges} height={820} />
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

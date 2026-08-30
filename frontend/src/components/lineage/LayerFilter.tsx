/** LayerFilter — 受控多选层筛选器（Phase 2.3）。
 *
 * - 7 层 SOURCE_SYSTEM → AI 全列出
 * - 受控：value 由父组件提供，onChange 回传新 Set
 * - 用 Ant Design Checkbox + 各层色块预览
 */
import { Checkbox, Space, Tag } from "antd";
import type { LineageLayer } from "../../types/lineage";
import { LAYER_COLORS } from "./LineageGraph";

export const ALL_LAYERS: LineageLayer[] = [
  "SOURCE_SYSTEM",
  "ODS",
  "DWD",
  "DWS",
  "ADS",
  "KPI",
  "AI",
];

export function layerColor(layer: LineageLayer): string {
  return LAYER_COLORS[layer];
}

interface LayerFilterProps {
  value: Set<LineageLayer>;
  onChange: (next: Set<LineageLayer>) => void;
}

export default function LayerFilter({ value, onChange }: LayerFilterProps) {
  const handleToggle = (layer: LineageLayer) => {
    const next = new Set(value);
    if (next.has(layer)) {
      next.delete(layer);
    } else {
      next.add(layer);
    }
    onChange(next);
  };

  return (
    <Space wrap>
      {ALL_LAYERS.map((layer) => {
        const checked = value.has(layer);
        return (
          <Checkbox
            key={layer}
            checked={checked}
            onChange={() => handleToggle(layer)}
            aria-label={layer}
          >
            <Tag color={LAYER_COLORS[layer]} style={{ marginInlineEnd: 0 }}>
              {layer}
            </Tag>
          </Checkbox>
        );
      })}
    </Space>
  );
}

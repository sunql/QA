/** ObjectFilter — 血缘图对象级多选筛选器（Step 5）。
 *
 * - 候选来自 LineagePage 按层过滤后的 edges（collectObjectCandidates）
 * - antd Select 多选 + 按层 OptGroup 分组 + 搜索 + 可清空
 * - 无候选时禁用（避免空下拉干扰）
 */
import { useMemo } from "react";
import { Select } from "antd";
import { useTranslation } from "../../i18n";
import { objectKey, type ObjectCandidate } from "./lineageFilter";

interface GroupedOption {
  label: string;
  options: Array<{ label: string; value: string }>;
}

/** 候选 → antd Select 分组 options（候选已按层排序，同层相邻即可成组）。 */
function toGroupedOptions(candidates: ObjectCandidate[]): GroupedOption[] {
  const groups: GroupedOption[] = [];
  let currentLayer: string | null = null;
  for (const c of candidates) {
    if (c.layer !== currentLayer) {
      currentLayer = c.layer;
      groups.push({ label: c.layer, options: [] });
    }
    groups[groups.length - 1].options.push({
      label: `${c.object} (${c.count})`,
      value: objectKey(c.layer, c.object),
    });
  }
  return groups;
}

interface ObjectFilterProps {
  candidates: ObjectCandidate[];
  value: ReadonlySet<string>;
  onChange: (next: Set<string>) => void;
}

export default function ObjectFilter({ candidates, value, onChange }: ObjectFilterProps) {
  const { t } = useTranslation();
  const groupedOptions = useMemo(() => toGroupedOptions(candidates), [candidates]);

  return (
    <Select
      mode="multiple"
      showSearch
      allowClear
      placeholder={t("lineage.filter.objectPlaceholder")}
      value={[...value]}
      onChange={(keys: string[]) => onChange(new Set(keys))}
      options={groupedOptions}
      optionFilterProp="label"
      disabled={candidates.length === 0}
      style={{ minWidth: 320 }}
      aria-label={t("lineage.filter.objects")}
    />
  );
}

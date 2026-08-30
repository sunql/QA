import { Tag, Tooltip } from "antd";
import type { DataQualityBadge } from "../../types/chat";
import { useTranslation } from "../../i18n";

interface DataQualityBadgeTagProps {
  badge: DataQualityBadge;
}

/** 评分档位 → 颜色：>=90 绿、>=70 黄、<70 红；未评估默认灰。
 *  与「评估中/未评估」明确区分（避免误把 0 分当作未评估）。
 */
function scoreColor(score: number): string {
  if (score >= 90) return "green";
  if (score >= 70) return "orange";
  return "red";
}

function formatPercent(score: string | null): string {
  if (score === null) return "";
  // 后端 Decimal → string；保留两位小数（与后端 Decimal 精度对齐）
  const n = Number(score);
  if (!Number.isFinite(n)) return score;
  return `${n.toFixed(2)}%`;
}

export default function DataQualityBadgeTag({ badge }: DataQualityBadgeTagProps) {
  const { t } = useTranslation();
  if (!badge.evaluated) {
    return (
      <Tooltip title={t("queryPlan.dataQuality.tooltip.unevaluated")}>
        <Tag color="default" style={{ marginBottom: 4 }}>
          {t("queryPlan.dataQuality.unevaluated")}
        </Tag>
      </Tooltip>
    );
  }
  const score = Number(badge.overallScore);
  const color = Number.isFinite(score) ? scoreColor(score) : "default";
  return (
    <Tooltip title={t("queryPlan.dataQuality.tooltip.view")}>
      <Tag color={color} style={{ marginBottom: 4 }}>
        DQ: {formatPercent(badge.overallScore)}
      </Tag>
    </Tooltip>
  );
}
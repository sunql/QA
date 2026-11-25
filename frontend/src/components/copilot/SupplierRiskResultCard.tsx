/** 供应商风险速览结果卡（P4）：头部「供应商 编码（企业键 …）+ 名称 + 分析结论」，
 * 正文为 KPI 贡献明细与建议动作。

数据源 = supplier_risk 工具步骤的 observation JSON（SupplierRiskRead，
camelCase）；与 PendingDeliveryResultCard 同模式：坏 JSON / 不匹配 ⇒ null
（回退纯文本答案，结果卡不经 LLM 复述）。
 */

import { Alert, Card, Collapse, Tag, Typography } from "antd";
import { useTranslation } from "../../i18n";
import type { CopilotStepPayload } from "../../api/procurementCopilot";

const { Text } = Typography;

/** 风险等级 → Tag 颜色（unknown 灰、low 绿、medium 橙、high 红）。 */
const LEVEL_COLOR: Record<string, string> = {
  low: "green",
  medium: "orange",
  high: "red",
  unknown: "default",
};

const LEVEL_LABEL: Record<string, string> = {
  low: "低风险",
  medium: "中风险",
  high: "高风险",
  unknown: "无法判定",
};

export interface SupplierRiskContribution {
  featureName: string;
  featureAlias?: string | null;
  value?: string | null;
  unit?: string | null;
  threshold?: string | null;
  passed?: boolean;
  note?: string | null;
}

export interface SupplierRiskResult {
  profile: {
    enterpriseKey: number;
    enterpriseCode: string;
    supplierName?: string | null;
  };
  level: string;
  levelSource?: string | null;
  contributions: SupplierRiskContribution[];
  riskPoints?: string | null;
  riskPointsSource?: string | null;
  recommendedActions?: string[] | null;
}

/** 从 supplier_risk 工具步骤解析结构化结果；不匹配/坏 JSON ⇒ null。 */
export function parseSupplierRiskStep(
  step: CopilotStepPayload,
): SupplierRiskResult | null {
  if (step.toolName !== "supplier_risk") return null;
  if (step.status !== "SUCCEEDED") return null;
  const text = (step.output as { text?: string } | null | undefined)?.text;
  if (!text) return null;
  try {
    const parsed = JSON.parse(text) as {
      data?: {
        profile?: SupplierRiskResult["profile"];
        level?: string;
        levelSource?: string;
        contributions?: SupplierRiskContribution[];
        riskPoints?: string;
        riskPointsSource?: string;
        recommendedActions?: string[];
      };
    };
    const data = parsed.data;
    if (!data?.profile?.enterpriseKey || !data.profile.enterpriseCode) return null;
    return {
      profile: data.profile,
      level: data.level ?? "unknown",
      levelSource: data.levelSource ?? null,
      contributions: Array.isArray(data.contributions) ? data.contributions : [],
      riskPoints: data.riskPoints ?? null,
      riskPointsSource: data.riskPointsSource ?? null,
      recommendedActions: data.recommendedActions ?? null,
    };
  } catch {
    return null;
  }
}

interface SupplierRiskResultCardProps {
  result: SupplierRiskResult;
}

export default function SupplierRiskResultCard({
  result,
}: SupplierRiskResultCardProps) {
  const { t } = useTranslation();
  const { profile } = result;
  const name = profile.supplierName?.trim();

  return (
    <div>
      {/* 头部：供应商 编码（企业键 …）+ 名称 —— 2026-11-25 需求的展示口径 */}
      <div style={{ marginBottom: 12 }}>
        <Text strong style={{ fontSize: 16 }}>
          {`供应商 ${profile.enterpriseCode}（企业键 ${profile.enterpriseKey}）`}
        </Text>
        {name && (
          <Text type="secondary" style={{ display: "block", marginTop: 2 }}>
            {name}
          </Text>
        )}
        <div style={{ marginTop: 6 }}>
          <Tag color={LEVEL_COLOR[result.level] ?? "default"}>
            {`${t("procurement.copilot.riskLevel")}：${
              LEVEL_LABEL[result.level] ?? result.level
            }`}
          </Tag>
          {result.levelSource && (
            <Tag>{`判定来源：${result.levelSource}`}</Tag>
          )}
        </div>
      </div>
      {/* 分析结论（LLM 风险点；fallback 模板同样展示，来源打标） */}
      {result.riskPoints && (
        <Alert
          type={result.level === "high" ? "error" : result.level === "medium" ? "warning" : "info"}
          showIcon
          style={{ marginBottom: 12 }}
          message={
            <>
              <Text strong>{t("procurement.copilot.riskConclusion")}</Text>
              <div style={{ marginTop: 4, whiteSpace: "pre-wrap" }}>{result.riskPoints}</div>
              {result.riskPointsSource && (
                <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 4 }}>
                  {`（来源：${result.riskPointsSource === "llm" ? "LLM 生成" : result.riskPointsSource}）`}
                </Text>
              )}
            </>
          }
        />
      )}
      {result.contributions.length > 0 && (
        <Collapse
          style={{ marginBottom: 12 }}
          items={[
            {
              key: "contributions",
              label: "指标贡献明细",
              children: (
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {result.contributions.map((c) => (
                    <div key={c.featureName} style={{ fontSize: 13 }}>
                      <Text strong>{c.featureAlias || c.featureName}</Text>
                      <Text type="secondary" style={{ marginLeft: 8 }}>
                        {`值：${c.value ?? "暂无数据"}${c.unit ?? ""}`}
                        {c.threshold ? ` ｜ 阈值：${c.threshold}${c.unit ?? ""}` : ""}
                        {c.passed === undefined ? "" : c.passed ? " ｜ 通过" : " ｜ 触发"}
                      </Text>
                      {c.note && (
                        <Text type="secondary" style={{ display: "block", fontSize: 12 }}>
                          {c.note}
                        </Text>
                      )}
                    </div>
                  ))}
                </div>
              ),
            },
          ]}
        />
      )}
      {result.recommendedActions && result.recommendedActions.length > 0 && (
        <Card size="small" title="建议动作">
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {result.recommendedActions.map((action) => (
              <li key={action}>
                <Text style={{ fontSize: 13 }}>{action}</Text>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

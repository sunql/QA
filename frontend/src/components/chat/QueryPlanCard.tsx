import { Collapse, Space, Tag, Typography } from "antd";
import type { DataQualityBadge, QueryPlan } from "../../types/chat";
import { useTranslation } from "../../i18n";
import DataQualityBadgeTag from "./DataQualityBadgeTag";

const { Text } = Typography;

interface QueryPlanCardProps {
  plan: QueryPlan;
  // Phase 1.4：每张 selectedClass 对应一条 badge；顺序对齐 plan.selectedClasses
  // 未传入 / 空数组 → 不渲染 badge 区（向后兼容）
  dataQuality?: DataQualityBadge[] | null;
}

function ItemLabel({ children }: { children: string }) {
  return (
    <Text type="secondary" style={{ display: "block", fontSize: 12, marginTop: 8 }}>
      {children}
    </Text>
  );
}

function FieldTags({ items }: { items: string[] }) {
  const { t } = useTranslation();
  if (!items.length) {
    return <Text type="secondary">{t("common.emDash")}</Text>;
  }
  return (
    <span>
      {items.map((item, idx) => (
        // key 附加索引：本体数据可能含重复列名，避免 React key 冲突警告（L1）
        <Tag key={`${item}-${idx}`} style={{ marginBottom: 4 }}>
          {item}
        </Tag>
      ))}
    </span>
  );
}

/** 把 dataQuality 数组按 selectedClasses 顺序建立 table → badge 索引，
 *  未匹配（selectedClasses 比 dataQuality 多）的情况视为未评估占位。
 *  不可变：用 reduce + spread 构造新对象。
 */
function buildBadgeIndex(
  plan: QueryPlan,
  dataQuality?: DataQualityBadge[] | null
): Map<string, DataQualityBadge> {
  if (!dataQuality || dataQuality.length === 0) {
    return new Map();
  }
  return plan.selectedClasses.reduce<Map<string, DataQualityBadge>>(
    (acc, cls, idx) => {
      const badge = dataQuality[idx];
      if (badge !== undefined) {
        acc.set(cls, badge);
      }
      return acc;
    },
    new Map()
  );
}

export default function QueryPlanCard({ plan, dataQuality }: QueryPlanCardProps) {
  const badgeIndex = buildBadgeIndex(plan, dataQuality);
  const { t } = useTranslation();
  return (
    <Collapse
      size="small"
      items={[
        {
          key: "plan",
          label: t("queryPlan.panelLabel"),
          children: (
            <div>
              {plan.interpretation ? (
                <>
                  <ItemLabel>{t("queryPlan.interpretation")}</ItemLabel>
                  <Text>{plan.interpretation}</Text>
                </>
              ) : null}
              <ItemLabel>{t("queryPlan.target")}</ItemLabel>
              <Text>{plan.target || t("common.emDash")}</Text>
              <ItemLabel>{t("queryPlan.tables")}</ItemLabel>
              <FieldTags items={plan.selectedClasses} />
              {badgeIndex.size > 0 ? (
                <div style={{ marginTop: 4 }}>
                  {plan.selectedClasses.map((cls, idx) => {
                    const badge = badgeIndex.get(cls);
                    if (!badge) return null;
                    return (
                      <span
                        // key 附加 idx：selectedClasses 可能含重复表名（罕见），避免 React key 冲突
                        key={`${cls}-${idx}`}
                        style={{ marginRight: 4 }}
                      >
                        <DataQualityBadgeTag badge={badge} />
                      </span>
                    );
                  })}
                </div>
              ) : null}
              <ItemLabel>{t("queryPlan.selectedFields")}</ItemLabel>
              <FieldTags items={plan.selectedProperties} />
              {plan.aggregations.length ? (
                <>
                  <ItemLabel>{t("queryPlan.aggregation")}</ItemLabel>
                  <span>
                    {plan.aggregations.map((agg, idx) => (
                      // key 附加索引：公式聚合与普通聚合可能 property 相同，避免 React key 冲突
                      <Tag
                        key={`${agg.function}-${agg.property}-${agg.formula ?? ""}-${idx}`}
                        color="geekblue"
                        style={{ marginBottom: 4 }}
                      >
                        {agg.formula ?? `${agg.function}(${agg.property})`}
                        {agg.alias ? ` AS ${agg.alias}` : ""}
                      </Tag>
                    ))}
                  </span>
                </>
              ) : null}
              {plan.groupBy.length ? (
                <>
                  <ItemLabel>{t("queryPlan.groupBy")}</ItemLabel>
                  <FieldTags items={plan.groupBy} />
                </>
              ) : null}
              {plan.conditions.length ? (
                <>
                  <ItemLabel>{t("queryPlan.filters")}</ItemLabel>
                  <FieldTags items={plan.conditions} />
                </>
              ) : null}
              {plan.sortBy.length ? (
                <>
                  <ItemLabel>{t("queryPlan.sort")}</ItemLabel>
                  <span>
                    {plan.sortBy.map((s) => (
                      <Tag key={`${s.property}-${s.direction}`} color="cyan" style={{ marginBottom: 4 }}>
                        {s.property} {s.direction.toUpperCase()}
                      </Tag>
                    ))}
                  </span>
                </>
              ) : null}
              {plan.joins.length ? (
                <>
                  <ItemLabel>{t("queryPlan.joins")}</ItemLabel>
                  <span>
                    {plan.joins.map((j) => (
                      <Tag key={`${j.sourceClass}-${j.targetClass}`} color="purple" style={{ marginBottom: 4 }}>
                        {j.sourceClass} ↔ {j.targetClass}
                      </Tag>
                    ))}
                  </span>
                </>
              ) : null}
              {plan.rowLimit != null ? (
                <>
                  <ItemLabel>{t("queryPlan.rowLimit")}</ItemLabel>
                  <Space size={4}>
                    <Text>{plan.rowLimit}</Text>
                  </Space>
                </>
              ) : null}
            </div>
          ),
        },
      ]}
    />
  );
}
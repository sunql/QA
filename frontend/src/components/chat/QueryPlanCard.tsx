import { Collapse, Space, Tag, Typography } from "antd";
import type { QueryPlan } from "../../types/chat";
import { useTranslation } from "../../i18n";

const { Text } = Typography;

interface QueryPlanCardProps {
  plan: QueryPlan;
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

export default function QueryPlanCard({ plan }: QueryPlanCardProps) {
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
import { Form, Input, Row, Col, Select } from "antd";
import type { FormInstance } from "antd";
import {
  METRIC_AGGREGATION_ORDER_OPTIONS,
  METRIC_EXECUTION_SOURCE_OPTIONS,
  METRIC_STATUS_OPTIONS,
  METRIC_TYPE_OPTIONS,
} from "../../types/ontology";
import { useTranslation } from "../../i18n";

/** 需要分母的指标类型（比率 / 差值），与后端 kpi_metric_reconcile 一致。 */
const TYPES_REQUIRING_DENOMINATOR = new Set(["RATIO", "DIFFERENCE"]);

/** 结构化度量允许的聚合（与后端 _MEASURE_AGG_ALLOWED 一致；≠ AggFunction）。 */
const MEASURE_AGG_OPTIONS = ["SUM", "COUNT", "AVG", "COUNT_DISTINCT"].map((value) => ({
  value,
  label: value,
}));

/** 后端 schemas.py 的字段长度上限（超长会 422，前端先截断）。 */
const MEASURE_EXPR_MAX_LENGTH = 1000;
const FILTER_EXPRESSION_MAX_LENGTH = 2000;
const OWNER_MAX_LENGTH = 100;

/** 指标类型 / 聚合阶 / 执行来源。 */
function MetricTypeRow() {
  const { t } = useTranslation();
  return (
    <Row gutter={12}>
      <Col span={8}>
        <Form.Item name="metricType" label={t("forms.ontology.metricLabels.metricType")}>
          <Select
            options={METRIC_TYPE_OPTIONS.map((o) => ({
              value: o.value,
              label: t(`enums.metricType.${o.labelKey}`),
            }))}
          />
        </Form.Item>
      </Col>
      <Col span={8}>
        <Form.Item name="aggregationOrder" label={t("forms.ontology.metricLabels.aggregationOrder")}>
          <Select
            allowClear
            options={METRIC_AGGREGATION_ORDER_OPTIONS.map((o) => ({
              value: o.value,
              label: t(`enums.metricAggregationOrder.${o.labelKey}`),
            }))}
          />
        </Form.Item>
      </Col>
      <Col span={8}>
        <Form.Item name="executionSource" label={t("forms.ontology.metricLabels.executionSource")}>
          <Select
            allowClear
            options={METRIC_EXECUTION_SOURCE_OPTIONS.map((o) => ({
              value: o.value,
              label: t(`enums.metricExecutionSource.${o.labelKey}`),
            }))}
          />
        </Form.Item>
      </Col>
    </Row>
  );
}

interface MeasureRowProps {
  /** 分子 / 分母 —— 决定字段名与 i18n key 前缀。 */
  role: "numerator" | "denominator";
}

/** 分子或分母的聚合 / 表达式 / 过滤（同一份选项复用）。 */
function MeasureRow({ role }: MeasureRowProps) {
  const { t } = useTranslation();
  return (
    <Row gutter={12}>
      <Col span={6}>
        <Form.Item
          name={`${role}Agg`}
          label={t(`forms.ontology.metricLabels.${role}Agg`)}
        >
          <Select allowClear options={MEASURE_AGG_OPTIONS} />
        </Form.Item>
      </Col>
      <Col span={10}>
        <Form.Item
          name={`${role}Expr`}
          label={t(`forms.ontology.metricLabels.${role}Expr`)}
        >
          <Input
            maxLength={MEASURE_EXPR_MAX_LENGTH}
            placeholder={t(`forms.ontology.metricPlaceholders.${role}Expr`)}
          />
        </Form.Item>
      </Col>
      <Col span={8}>
        <Form.Item
          name={`${role}Filter`}
          label={t(`forms.ontology.metricLabels.${role}Filter`)}
        >
          <Input
            maxLength={MEASURE_EXPR_MAX_LENGTH}
            placeholder={t(`forms.ontology.metricPlaceholders.${role}Filter`)}
          />
        </Form.Item>
      </Col>
    </Row>
  );
}

/** 粒度键 / 全局过滤 / 负责人 / 状态。 */
function MetricMetaRow() {
  const { t } = useTranslation();
  return (
    <>
      <Row gutter={12}>
        <Col span={12}>
          <Form.Item name="grainKeys" label={t("forms.ontology.metricLabels.grainKeys")}>
            <Select
              mode="tags"
              placeholder={t("forms.ontology.metricPlaceholders.grainKeys")}
            />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item
            name="filterExpression"
            label={t("forms.ontology.metricLabels.filterExpression")}
          >
            <Input
              maxLength={FILTER_EXPRESSION_MAX_LENGTH}
              placeholder={t("forms.ontology.metricPlaceholders.filterExpression")}
            />
          </Form.Item>
        </Col>
      </Row>
      <Row gutter={12}>
        <Col span={12}>
          <Form.Item name="owner" label={t("forms.ontology.metricLabels.owner")}>
            <Input
              maxLength={OWNER_MAX_LENGTH}
              placeholder={t("forms.ontology.metricPlaceholders.owner")}
            />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item name="status" label={t("forms.ontology.metricLabels.status")}>
            <Select
              options={METRIC_STATUS_OPTIONS.map((o) => ({
                value: o.value,
                label: t(`enums.metricStatus.${o.labelKey}`),
              }))}
            />
          </Form.Item>
        </Col>
      </Row>
    </>
  );
}

export interface MetricTechniqueFormProps {
  form: FormInstance;
}

/**
 * 指标技术形态的结构化编辑区。
 *
 * 渲染在**父 Form 内部** —— 里面的 `Form.Item` 直接注册到父表单，
 * 父组件负责把扁平字段转成后端 measure JSON。
 */
export default function MetricTechniqueForm({ form }: MetricTechniqueFormProps) {
  const metricType = Form.useWatch("metricType", form) as string | undefined;
  const needsDenominator = TYPES_REQUIRING_DENOMINATOR.has(metricType ?? "");

  return (
    <>
      <MetricTypeRow />
      <MeasureRow role="numerator" />
      {needsDenominator && <MeasureRow role="denominator" />}
      <MetricMetaRow />
    </>
  );
}

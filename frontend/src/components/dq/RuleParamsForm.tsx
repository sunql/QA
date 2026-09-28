/**
 * RuleParamsForm — renders structured rule-param fields dispatched by ruleType + kind.
 *
 * Wire format note: backend DTOs use `alias_generator=to_camel + populate_by_name=True`,
 * so JSON on the wire uses camelCase (refTable, refColumn, ruleCode, datasourceId, etc.).
 * The brief's snake_case refs (ref_table, ref_column) would read undefined from the wire
 * and serialize incorrectly. We use camelCase here to match the actual contract.
 */
import { useTranslation } from "react-i18next";
import { Form, Input, InputNumber, Select } from "antd";
import { useMemo } from "react";
import type { AnyRuleParams, RuleParamsKind } from "../../utils/ruleParamsSummary";
import type { RuleTypeLiteral } from "../../utils/ruleExpressionTemplates";

interface ColumnLike {
  name: string;
  dataType?: string;
}

interface Props {
  ruleType: RuleTypeLiteral;
  columns: ColumnLike[];
  value: AnyRuleParams | null;
  onChange: (v: AnyRuleParams) => void;
}

const KIND_OPTIONS_BY_RULE: Record<RuleTypeLiteral, { value: RuleParamsKind; labelKey: string }[]> = {
  COMPLETENESS: [{ value: "not_null", labelKey: "not_null" }],
  UNIQUENESS:   [{ value: "unique",   labelKey: "unique" }],
  VALIDITY:     [
    { value: "range",   labelKey: "range" },
    { value: "in_set",  labelKey: "in_set" },
    { value: "regex",   labelKey: "regex" },
    { value: "compare", labelKey: "compare" },
  ],
  REFERENTIAL:  [{ value: "ref",            labelKey: "ref" }],
  CONSISTENCY:  [{ value: "cross_column",   labelKey: "cross_column" }],
  TIMELINESS:   [],
};

export function RuleParamsForm({ ruleType, columns, value, onChange }: Props) {
  const { t } = useTranslation();
  const kindOptions = KIND_OPTIONS_BY_RULE[ruleType] ?? [];
  const currentKind = value?.kind ?? kindOptions[0]?.value;
  const columnOptions = useMemo(
    () => columns.map((c) => ({ value: c.name, label: c.name })),
    [columns],
  );

  function setKind(kind: RuleParamsKind) {
    // Seed a minimal object for the chosen kind
    const seed = { kind } as AnyRuleParams;
    onChange(seed);
  }

  // Spread-safe partial update — value may be null/undefined on first render.
  // Include currentKind as seed so the first patch call always carries the kind.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function patch(p: Record<string, any>) {
    const base = (value as AnyRuleParams) ?? { kind: currentKind };
    onChange({ ...base, ...p } as AnyRuleParams);
  }

  if (!currentKind) {
    return <div>{t("dqRuleParams.unsupported")}</div>;
  }

  return (
    // 注意：本组件通常嵌在外层 antd Form 的 Form.Item 里，不能再包一层 <Form>（form 嵌套告警）。
    // Form.Item 在无 name 时只做布局，脱离 Form 上下文可正常渲染。
    <div>
      {kindOptions.length > 1 && (
        <Form.Item label={t("dqRuleParams.fields.kind")}>
          <Select
            value={currentKind}
            options={kindOptions.map((o) => ({ value: o.value, label: o.value }))}
            onChange={setKind}
          />
        </Form.Item>
      )}

      {currentKind === "range" && (
        <>
          <Form.Item label={t("dqRuleParams.fields.min")}>
            <InputNumber
              // value may be a different union member; use optional chaining + nullish fallback
              value={((value as { min?: number } | null)?.min) ?? null}
              onChange={(v) => patch({ min: v })}
            />
          </Form.Item>
          <Form.Item label={t("dqRuleParams.fields.max")}>
            <InputNumber
              value={((value as { max?: number } | null)?.max) ?? null}
              onChange={(v) => patch({ max: v })}
            />
          </Form.Item>
        </>
      )}

      {currentKind === "in_set" && (
        <Form.Item label={t("dqRuleParams.fields.values")}>
          <Select
            mode="tags"
            value={((value as { values?: string[] } | null)?.values) ?? []}
            onChange={(v) => patch({ values: v })}
          />
        </Form.Item>
      )}

      {currentKind === "regex" && (
        <Form.Item label={t("dqRuleParams.fields.pattern")}>
          <Input
            value={((value as { pattern?: string } | null)?.pattern) ?? ""}
            onChange={(e) => patch({ pattern: e.target.value })}
          />
        </Form.Item>
      )}

      {currentKind === "compare" && (
        <>
          <Form.Item label={t("dqRuleParams.fields.op")}>
            <Select
              value={((value as { op?: string } | null)?.op) ?? ">"}
              options={[">", ">=", "<", "<=", "=", "!="].map((o) => ({ value: o, label: o }))}
              onChange={(v) => patch({ op: v })}
            />
          </Form.Item>
          <Form.Item label={t("dqRuleParams.fields.value")}>
            <InputNumber
              value={((value as { value?: number } | null)?.value) ?? null}
              onChange={(v) => patch({ value: v })}
            />
          </Form.Item>
        </>
      )}

      {currentKind === "ref" && (
        <>
          {/*
            Wire-format note: backend uses camelCase (refTable, refColumn) per
            alias_generator=to_camel + populate_by_name. Using snake_case here
            would silently fail to read/write on the JSON contract.
          */}
          <Form.Item label={t("dqRuleParams.fields.refTable")}>
            <Input
              value={((value as { refTable?: string } | null)?.refTable) ?? ""}
              onChange={(e) => patch({ refTable: e.target.value })}
            />
          </Form.Item>
          <Form.Item label={t("dqRuleParams.fields.refColumn")}>
            <Input
              value={((value as { refColumn?: string } | null)?.refColumn) ?? ""}
              onChange={(e) => patch({ refColumn: e.target.value })}
            />
          </Form.Item>
        </>
      )}

      {currentKind === "cross_column" && (
        <>
          <Form.Item label={t("dqRuleParams.fields.left")}>
            <Select
              value={((value as { left?: string } | null)?.left)}
              options={columnOptions}
              onChange={(v) => patch({ left: v })}
            />
          </Form.Item>
          <Form.Item label={t("dqRuleParams.fields.op")}>
            <Select
              value={((value as { op?: string } | null)?.op) ?? "<="}
              options={[">", ">=", "<", "<=", "=", "!="].map((o) => ({ value: o, label: o }))}
              onChange={(v) => patch({ op: v })}
            />
          </Form.Item>
          <Form.Item label={t("dqRuleParams.fields.right")}>
            <Select
              value={((value as { right?: string } | null)?.right)}
              options={columnOptions}
              onChange={(v) => patch({ right: v })}
            />
          </Form.Item>
          <Form.Item label={t("dqRuleParams.fields.factor")}>
            <InputNumber
              value={((value as { factor?: number } | null)?.factor) ?? null}
              onChange={(v) => patch({ factor: v })}
              placeholder="1"
            />
          </Form.Item>
        </>
      )}
    </div>
  );
}

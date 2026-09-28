export type RuleParamsKind =
  | "not_null" | "unique" | "range" | "in_set" | "regex"
  | "compare" | "ref" | "cross_column";

export interface RuleParamsBase { kind: RuleParamsKind }
export interface RangeParams extends RuleParamsBase {
  kind: "range"; min?: number | string; max?: number | string;
}
export interface InSetParams extends RuleParamsBase {
  kind: "in_set"; values: string[];
}
export interface RegexParams extends RuleParamsBase {
  kind: "regex"; pattern: string;
}
export interface CompareParams extends RuleParamsBase {
  kind: "compare"; op: ">" | ">=" | "<" | "<=" | "=" | "!="; value: number | string;
}
export interface RefParams extends RuleParamsBase {
  kind: "ref"; ref_table: string; ref_column: string;
}
export interface CrossColumnParams extends RuleParamsBase {
  kind: "cross_column";
  left: string; op: ">" | ">=" | "<" | "<=" | "=" | "!=";
  right: string; factor?: number | string;
}
export type AnyRuleParams =
  | RuleParamsBase | RangeParams | InSetParams | RegexParams
  | CompareParams | RefParams | CrossColumnParams;

const OP_GLYPH: Record<string, string> = {
  ">": ">", ">=": "≥", "<": "<", "<=": "≤", "=": "=", "!=": "≠",
};

export function summarizeRuleParams(
  params: AnyRuleParams,
  columnHint: string,
  locale: "zh-CN" | "en-US",
): string {
  switch (params.kind) {
    case "not_null":
      return locale === "zh-CN" ? `${columnHint} 非空` : `${columnHint} IS NOT NULL`;
    case "unique":
      return locale === "zh-CN" ? `${columnHint} 唯一` : `${columnHint} IS UNIQUE`;
    case "range": {
      const p = params as RangeParams;
      const { min, max } = p;
      if (min !== undefined && max !== undefined) {
        return locale === "zh-CN"
          ? `${columnHint} ∈ [${min}, ${max}]`
          : `${columnHint} BETWEEN ${min} AND ${max}`;
      }
      if (min !== undefined) {
        return locale === "zh-CN"
          ? `${columnHint} ≥ ${min}` : `${columnHint} >= ${min}`;
      }
      return locale === "zh-CN"
        ? `${columnHint} ≤ ${max}` : `${columnHint} <= ${max}`;
    }
    case "in_set": {
      const p = params as InSetParams;
      const list = p.values.join(", ");
      return locale === "zh-CN"
        ? `${columnHint} ∈ {${list}}` : `${columnHint} IN (${list})`;
    }
    case "regex": {
      const p = params as RegexParams;
      return locale === "zh-CN"
        ? `${columnHint} 匹配 ${p.pattern}` : `${columnHint} MATCHES ${p.pattern}`;
    }
    case "compare": {
      const p = params as CompareParams;
      const op = OP_GLYPH[p.op] ?? p.op;
      return locale === "zh-CN"
        ? `${columnHint} ${op} ${p.value}` : `${columnHint} ${p.op} ${p.value}`;
    }
    case "ref": {
      const p = params as RefParams;
      return locale === "zh-CN"
        ? `${columnHint} 参照 ${p.ref_table}.${p.ref_column}`
        : `${columnHint} REFERENCES ${p.ref_table}.${p.ref_column}`;
    }
    case "cross_column": {
      const p = params as CrossColumnParams;
      const op = OP_GLYPH[p.op] ?? p.op;
      const factor = p.factor !== undefined
        ? (locale === "zh-CN" ? ` × ${p.factor}` : ` * ${p.factor}`)
        : "";
      return locale === "zh-CN"
        ? `${p.left} ${op} ${p.right}${factor}`
        : `${p.left} ${p.op} ${p.right}${factor}`;
    }
  }
}

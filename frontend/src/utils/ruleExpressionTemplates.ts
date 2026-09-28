/**
 * 规则表达式自动匹配模板（feat-rule-batch-create，2026-09-15）
 *
 * 目标：让用户为某列选规则类型后，前端立即给出一个候选表达式（最常用模板）。
 * 复杂场景允许人工覆盖。
 *
 * 设计：
 * - 纯函数，输入 (列元信息, 规则类型, 可选 ontology 属性)，输出
 *   { expression, templateId, canAutoFill }；
 * - 模板按"列数据类型 × 规则类型"查表；
 * - 列名 hint（PO_QTY / ORDER_NO 等）让 >0 vs >=0、REGEX 等模板更精准；
 * - ontology 属性的 min_value / max_value / is_foreign_key 用于补齐变量。
 */

export type RuleTypeLiteral =
  | "COMPLETENESS"
  | "VALIDITY"
  | "UNIQUENESS"
  | "CONSISTENCY"
  | "REFERENTIAL"
  | "TIMELINESS";

export interface ColumnLike {
  /** 物理列名（用于 hint 关键字判断） */
  name: string;
  /** 列数据类型（来自 schema，例: VARCHAR2 / NUMBER / DATE / DECIMAL 等） */
  dataType: string;
}

export interface OntologyPropertyLike {
  isForeignKey?: boolean;
  /** 关联类的物理表名（REFERENTIAL 模板需要） */
  refSourceTable?: string | null;
  /** 关联类的物理列名（REFERENTIAL 模板需要） */
  refSourceColumn?: string | null;
  minValue?: number | string | null;
  maxValue?: number | string | null;
}

export interface RuleExpressionCandidate {
  /** 推荐表达式（可能为 null——表示无自动模板，需人工填） */
  expression: string | null;
  /** 命中的模板 id（用于 UI 显示「模板：T1」） */
  templateId: string | null;
  /** 是否能自动填：false 时表达式应留空并显示「无自动模板」 */
  canAutoFill: boolean;
}

/** 大类型归一化：将各家方言归并为 NUMBER / STRING / DATE / OTHER */
function normalizeDataType(dataType: string): "NUMBER" | "STRING" | "DATE" | "OTHER" {
  const dt = (dataType || "").toUpperCase();
  if (
    dt.includes("NUMBER") ||
    dt.includes("DECIMAL") ||
    dt.includes("NUMERIC") ||
    dt.includes("INT") ||
    dt.includes("FLOAT") ||
    dt.includes("DOUBLE")
  ) {
    return "NUMBER";
  }
  if (dt.includes("DATE") || dt.includes("TIMESTAMP")) {
    return "DATE";
  }
  if (
    dt.includes("VARCHAR") ||
    dt.includes("CHAR") ||
    dt.includes("TEXT") ||
    dt.includes("STRING")
  ) {
    return "STRING";
  }
  return "OTHER";
}

/** 列名 hint：返回「数量 / 单价 / 编码 / 日期」等弱语义。 */
function classifyColumnName(name: string):
  | "QTY"
  | "PRICE"
  | "DIFF"
  | "NO_CODE_ID"
  | "DATE_FUTURE"
  | "DATE_PAST"
  | "OTHER" {
  const up = name.toUpperCase();
  if (/(QTY|AMT|COUNT|NUM)/.test(up)) return "QTY";
  if (/(PRICE|PRC|AMT|UNIT_PR|RATE)/.test(up) && /(PR|PRC|PRICE|RATE)/.test(up)) return "PRICE";
  if (/DIFF/.test(up)) return "DIFF";
  if (/(NO|CODE|ID|_NO)$/.test(up)) return "NO_CODE_ID";
  if (/DATE/.test(up)) {
    // ORDER_DATE / START_DATE 等视为「应有过去日期」，EXPIRE_DATE / END_DATE 视为「应有未来日期」
    if (/(START|BIRTH|CREATE|ORDER|REGISTER)/.test(up)) return "DATE_PAST";
    return "DATE_FUTURE";
  }
  return "OTHER";
}

export function suggestRuleExpression(args: {
  column: ColumnLike;
  ruleType: RuleTypeLiteral;
  ontologyProperty?: OntologyPropertyLike | null;
}): RuleExpressionCandidate {
  const { column, ruleType, ontologyProperty } = args;
  const colType = normalizeDataType(column.dataType);
  const nameHint = classifyColumnName(column.name);

  // COMPLETENESS：与 data_quality_rule_generator.py 对齐，产出 ${col} IS NOT NULL
  // 评估器（completeness.py）不依赖 rule_expression（自己拼 COUNT(<col>)），
  // 这里只是给 UI / 审计看的可读 SQL 片段。
  if (ruleType === "COMPLETENESS") {
    return {
      expression: `${column.name} IS NOT NULL`,
      templateId: "T_COMPLETENESS_NOT_NULL",
      canAutoFill: true,
    };
  }
  // UNIQUENESS：与 data_quality_rule_generator.py 第 193 行主键分支对齐，
  // 产出 UNIQUE(<col>)。评估器（uniqueness.py）不依赖 rule_expression，
  // 这里只是给 UI / 审计看的可读 SQL 片段。
  if (ruleType === "UNIQUENESS") {
    return {
      expression: `UNIQUE(${column.name})`,
      templateId: "T_UNIQUE_COLUMN",
      canAutoFill: true,
    };
  }
  if (ruleType === "TIMELINESS") {
    // Phase 2 未实现，留空让人工填
    return { expression: null, templateId: null, canAutoFill: false };
  }

  // VALIDITY 模板
  if (ruleType === "VALIDITY") {
    if (colType === "NUMBER") {
      if (nameHint === "QTY") {
        return {
          expression: `${column.name} > 0`,
          templateId: "T_NUMBER_QTY_GT_ZERO",
          canAutoFill: true,
        };
      }
      if (nameHint === "DIFF" || nameHint === "PRICE") {
        return {
          expression: `${column.name} >= 0`,
          templateId: "T_NUMBER_NON_NEG",
          canAutoFill: true,
        };
      }
      // 兜底：本体属性给了 min/max → BETWEEN
      const minV = ontologyProperty?.minValue;
      const maxV = ontologyProperty?.maxValue;
      if (minV !== null && minV !== undefined && maxV !== null && maxV !== undefined) {
        return {
          expression: `${column.name} BETWEEN ${minV} AND ${maxV}`,
          templateId: "T_NUMBER_BETWEEN",
          canAutoFill: true,
        };
      }
      return { expression: null, templateId: null, canAutoFill: false };
    }
    if (colType === "STRING") {
      if (nameHint === "NO_CODE_ID") {
        return {
          expression: `${column.name} REGEX '^[A-Z0-9_-]+$'`,
          templateId: "T_STRING_REGEX_CODE",
          canAutoFill: true,
        };
      }
      // 兜底：本体属性给 allowed_values? 简化为 IN 提示，但不展开值（canAutoFill=false）
      return { expression: null, templateId: null, canAutoFill: false };
    }
    if (colType === "DATE") {
      if (nameHint === "DATE_PAST" || nameHint === "OTHER") {
        return {
          expression: `${column.name} <= CURRENT_TIMESTAMP`,
          templateId: "T_DATE_NOT_FUTURE",
          canAutoFill: true,
        };
      }
      if (nameHint === "DATE_FUTURE") {
        const minV = ontologyProperty?.minValue;
        if (minV !== null && minV !== undefined) {
          return {
            expression: `${column.name} >= '${minV}'`,
            templateId: "T_DATE_AFTER",
            canAutoFill: true,
          };
        }
        return {
          expression: `${column.name} >= CURRENT_TIMESTAMP`,
          templateId: "T_DATE_NOT_PAST",
          canAutoFill: true,
        };
      }
    }
    return { expression: null, templateId: null, canAutoFill: false };
  }

  // CONSISTENCY 模板（跨列谓词，本函数只处理占位）
  if (ruleType === "CONSISTENCY") {
    return { expression: null, templateId: null, canAutoFill: false };
  }

  // REFERENTIAL 模板（必须 REF <table>.<col>）
  if (ruleType === "REFERENTIAL") {
    const refTable = ontologyProperty?.refSourceTable;
    const refCol = ontologyProperty?.refSourceColumn;
    if (refTable && refCol) {
      return {
        expression: `REF ${refTable}.${refCol}`,
        templateId: "T_REF_TABLE_COL",
        canAutoFill: true,
      };
    }
    return { expression: null, templateId: null, canAutoFill: false };
  }

  return { expression: null, templateId: null, canAutoFill: false };
}
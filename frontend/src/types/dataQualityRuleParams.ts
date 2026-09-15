/** 数据质量规则配置 DTO（feat-dq-rule-params，2026-09-15）

    后端 DTO 使用 camelCase 契约（经 _Base alias_generator=to_camel 序列化），
    与前端 dataQuality.ts 保持一致。
*/

import type { RuleTypeLiteral } from "../utils/ruleExpressionTemplates";
import type { AnyRuleParams } from "../utils/ruleParamsSummary";

export interface RuleParamsReadDto {
  id: number;
  ruleCode: string;
  ruleName: string;
  ruleType: RuleTypeLiteral;
  targetTable: string;
  targetColumn: string | null;
  threshold: string;
  severity: string;
  datasourceId: number;
  ruleExpression: string | null;
  ruleParams: AnyRuleParams | null;
  configMode: "structured" | "custom";
  /** 与 data-quality 规则 tab 对齐的治理列（2026-09-15） */
  isEnabled: boolean;
  owner: string | null;
}

export interface RuleParamsCreateDto {
  ruleCode: string;
  ruleName: string;
  ruleType: RuleTypeLiteral;
  targetTable: string;
  targetColumn?: string | null;
  threshold: string;
  severity: string;
  datasourceId: number;
  ruleParams?: AnyRuleParams | null;
  ruleExpression?: string | null;
}

export type RuleParamsUpdateDto = Partial<RuleParamsCreateDto>;

export { type AnyRuleParams } from "../utils/ruleParamsSummary";

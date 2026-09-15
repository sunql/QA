/** 规则编码建议 API（feat-rule-batch-create，2026-09-15）
 *
 * 选完类后异步拿建议编码；前端只用于预览，createRule() 时按 UniqueConstraint 兜底。
 */

import { httpClient } from "./client";

const BASE = "/data-quality/rules";

export interface NextRuleCodeResponse {
  code: string;
  seq: number;
}

export async function fetchNextRuleCode(args: {
  className: string;
  date?: string; // YYYYMMDD；不传默认今天
}): Promise<NextRuleCodeResponse> {
  const params: Record<string, string> = { className: args.className };
  if (args.date) params.date = args.date;
  const res = await httpClient.get<NextRuleCodeResponse>(`${BASE}/next-code`, {
    params,
  });
  return res.data;
}
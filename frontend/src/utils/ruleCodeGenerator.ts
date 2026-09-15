/**
 * 规则编码 / 规则名生成（feat-rule-batch-create，2026-09-15）
 *
 * 规则编码格式：MU-DQ-{类名大写}-{YYYYMMDD}-{5位流水}
 * 规则名格式：{类名}-{列名}-{规则类型中文}
 */

/** 简单 ASCII 化：非 ASCII 字符替换为 ''，并把空白/标点变 '_'，再截断到 maxLen。 */
export function sanitizeClassName(name: string, maxLen = 12): string {
  if (!name) return "CLASS";
  const ascii = name
    // 非 ASCII 字符直接丢弃（业务上类名都是英文/拼音缩写）
    .replace(/[^\x00-\x7F]/g, "")
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!ascii) return "CLASS";
  return ascii.slice(0, maxLen);
}

/** YYYYMMDD 格式化（默认今天） */
export function formatYmd(date: Date = new Date()): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}${m}${d}`;
}

/** 5 位流水补零 */
export function padSeq(seq: number): string {
  return String(Math.max(1, seq)).padStart(5, "0");
}

/** 拼出规则编码候选（前端预览用，后端实际写库时按 DB 真实 MAX+1 校正） */
export function buildRuleCode(args: {
  className: string;
  seq: number;
  date?: Date;
}): string {
  const cls = sanitizeClassName(args.className, 30);
  const ymd = formatYmd(args.date);
  const seq = padSeq(args.seq);
  return `MU-DQ-${cls}-${ymd}-${seq}`;
}

/** 解析已存在编码的流水号，用于校验格式 + 提取 seq。 */
export function parseRuleCode(code: string): { className: string; date: string; seq: number } | null {
  const m = code.match(/^MU-DQ-([A-Z0-9_]+)-(\d{8})-(\d{5})$/);
  if (!m) return null;
  return {
    className: m[1],
    date: m[2],
    seq: parseInt(m[3], 10),
  };
}

/** 规则名：{类名}-{列名}-{规则类型中文}（同一规则类型下不允许重复） */
export const RULE_TYPE_CN: Record<string, string> = {
  COMPLETENESS: "完整性",
  VALIDITY: "有效性",
  UNIQUENESS: "唯一性",
  CONSISTENCY: "一致性",
  REFERENTIAL: "引用性",
  TIMELINESS: "时效性",
};

export function buildRuleName(args: {
  className: string;
  columnName: string;
  ruleType: string;
}): string {
  const cn = RULE_TYPE_CN[args.ruleType] ?? args.ruleType;
  return `${args.className}-${args.columnName}-${cn}`;
}
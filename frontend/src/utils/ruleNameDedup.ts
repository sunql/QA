/**
 * 规则名去重检查（feat-rule-batch-create，2026-09-15）
 *
 * 输入一批待创建规则的 name，返回冲突索引集合（用于 UI 标红）。
 * 全局唯一：跨规则名相同即冲突。
 */

export interface NameCarrier {
  ruleName: string;
}

export function findDuplicateNameIndices(items: NameCarrier[]): number[] {
  const seen = new Map<string, number[]>();
  items.forEach((it, idx) => {
    const list = seen.get(it.ruleName) ?? [];
    list.push(idx);
    seen.set(it.ruleName, list);
  });
  const conflictIdx: number[] = [];
  for (const indices of seen.values()) {
    if (indices.length > 1) {
      conflictIdx.push(...indices);
    }
  }
  return conflictIdx.sort((a, b) => a - b);
}
/**
 * 本体对象名的展示口径（单源）。
 *
 * 中文（默认）：别名在前 —— 「到货单（DWD_ARRIVAL_ORDER_DTL）」
 * 英文：物理名在前 —— 「DWD_ARRIVAL_ORDER_DTL (到货单)」
 * 无别名（含空串）：只显示物理名，两种语言一致。
 *
 * 本体管理页与 Wiki 链接管理页共用本函数，避免两处各写一份格式化逻辑后漂移。
 * 注意：本体数据的别名目前大量为空（class_alias 0/52、property_alias 0/3642），
 * 所以多数对象会落到「只显示物理名」这一支 —— 与本体管理页表现一致。
 */
export function ontologyObjectLabel(
  name: string,
  alias: string | null | undefined,
  locale: string,
): string {
  if (!alias) {
    return name;
  }
  const isChinese = locale.startsWith("zh");
  return isChinese ? `${alias}（${name}）` : `${name} (${alias})`;
}

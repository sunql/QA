/** 类下拉选项的 label 计算与构建，供本体管理四个 Tab 复用。
 *
 * label 规则与既有逻辑一致：有别名 → 「类名（别名）」（classOptionLabel），
 * 无别名 → 仅类名（classOptionLabelNoAlias）。
 * value 统一为 number；筛选条（FilterBar）需字符串值，调用方自行 String(...)。
 */
import type { OntologyClass } from "../../types/ontology";
import type { Vars } from "../../i18n";

/** 与 useTranslation() 返回的 t 同签名（见 src/i18n/useTranslation.ts）。 */
export type TFunc = (key: string, vars?: Vars) => string;

export function classOptionLabel(
  t: TFunc,
  className: string,
  classAlias: string | null | undefined
): string {
  return classAlias
    ? t("forms.ontology.classOptionLabel", { name: className, alias: classAlias })
    : t("forms.ontology.classOptionLabelNoAlias", { name: className });
}

export function classOptions(
  t: TFunc,
  classes: OntologyClass[]
): { label: string; value: number }[] {
  return classes.map((c) => ({
    label: classOptionLabel(t, c.className, c.classAlias),
    value: c.id,
  }));
}

/** 类下拉选项的 label 计算与构建，供本体管理各 Tab 与 Wiki 链接管理页复用。
 *
 * 文案格式的 SSOT 在 utils/ontologyLabel —— 中文「别名（物理名）」、英文「物理名 (alias)」。
 * 原先的 t("forms.ontology.classOptionLabel") 走 i18n，但那只能换括号样式、换不了顺序；
 * 中文要「中文打头」必须按语言换结构，故收敛到 helper。
 *
 * locale 必填而非默认 "zh-CN"：漏传就在 `tsc -b` 处报错，而不是静默给英文用户塞中文格式。
 */
import { ontologyObjectLabel } from "../../utils/ontologyLabel";
import type { OntologyClass } from "../../types/ontology";

export function classOptionLabel(
  className: string,
  classAlias: string | null | undefined,
  locale: string,
): string {
  return ontologyObjectLabel(className, classAlias, locale);
}

export function classOptions(
  classes: OntologyClass[],
  locale: string,
): { label: string; value: number }[] {
  return classes.map((c) => ({
    label: classOptionLabel(c.className, c.classAlias, locale),
    value: c.id,
  }));
}

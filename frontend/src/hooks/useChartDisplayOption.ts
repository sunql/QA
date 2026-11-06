/** 「这张图默认看哪一份」+ 数字口径 —— 聊天端与研究报告共用一条管线。
 *
 * 后端把「前 20 + 其他」挂在同一个 option 上（`topNOption` / `topNMeta`），
 * 这里只决定**默认看哪一份**。切换是组件本地 state：不回后端、不改 store，
 * 回放/导出不受影响。换图时复位 —— 否则上一张图的全量态会带到下一张
 * （下一张可能根本没有 Top-N）。
 *
 * **为什么要共用**：两处各写一遍时，数字格式的参数（locale）漏一处就会分叉 ——
 * 同一份报告在聊天页显示 `123,456`、研究线显示 `12.35万`。管线收在一个 hook
 * 里，新增口径（locale、新的格式化步骤）只改这里。
 */
import { useEffect, useMemo, useState } from "react";
import { applyChartNumberFormat } from "../theme/chartFormat";
import { topNMetaOf, topNOptionOf } from "../utils/chartTopN";
import type { TopNMeta } from "../utils/chartTopN";
import { useTranslation } from "../i18n";

export interface ChartDisplayOption {
  /** 当前显示的那一份（Top-N 截断视图或全量）—— 高度推断用它。 */
  displayOption: Record<string, unknown> | null | undefined;
  /** 数字口径已注入的 option（轴缩写 / tooltip 千分位）—— 画图用它。 */
  formattedOption: Record<string, unknown> | null | undefined;
  topNMeta: TopNMeta | null;
  showFullView: boolean;
  toggleShowFullView: () => void;
}

export function useChartDisplayOption(
  option: Record<string, unknown> | null | undefined
): ChartDisplayOption {
  const { locale } = useTranslation();
  const topNMeta = useMemo(() => topNMetaOf(option), [option]);
  const topNView = useMemo(() => topNOptionOf(option), [option]);
  const [showFullView, setShowFullView] = useState(false);
  useEffect(() => {
    setShowFullView(false);
  }, [option]);
  const displayOption = !showFullView && topNView ? topNView : option;
  const formattedOption = useMemo(
    () => (displayOption ? applyChartNumberFormat(displayOption, locale) : null),
    [displayOption, locale]
  );
  const toggleShowFullView = () => setShowFullView((value) => !value);
  return { displayOption, formattedOption, topNMeta, showFullView, toggleShowFullView };
}

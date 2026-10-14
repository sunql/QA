/** 通用 echarts 包装（feat-dq-evaluation-report，Phase 6）。
 *
 * 模式参考 `frontend/src/components/chat/ChartRenderer.tsx`：用 `ReactECharts` +
 * `notMerge` 防止 setOption 串台；option 用 `useMemo` 避免重渲。
 * 类型用 EChartsOption（echarts 5.x 内置），保证 option 写错会编译期报错。
 */

import { useMemo } from "react";
import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";
import { useThemeStore } from "../stores/themeStore";
import { applyChartTheme } from "../theme/chartTheme";
import { DARK_TOKEN, LIGHT_TOKEN } from "../theme/tokens";

interface EChartProps {
  option: EChartsOption;
  height?: number;
  /** 用于 jest mock 标识；测试可以指定 testId 抓节点。 */
  testId?: string;
}

export default function EChart({ option, height = 280, testId }: EChartProps) {
  const isDark = useThemeStore((state) => state.isDark);
  // 图表主题注入（与服务端发来的 chat option 同一条路径）：轴色/文字色/tooltip
  // 跟着主题走。调用方显式写在 series 上的 itemStyle.color 优先级更高，不会被覆盖
  // —— 语义色（通过/失败）本来就该由业务定，不该被主题改。
  const memoOption = useMemo(
    () => applyChartTheme(option as unknown as Record<string, unknown>, isDark ? DARK_TOKEN : LIGHT_TOKEN),
    [option, isDark]
  );
  return (
    <div data-testid={testId}>
      <ReactECharts
        option={memoOption as EChartsOption}
        style={{ height, width: "100%" }}
        notMerge
        lazyUpdate
      />
    </div>
  );
}
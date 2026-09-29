/** 通用 echarts 包装（feat-dq-evaluation-report，Phase 6）。
 *
 * 模式参考 `frontend/src/components/chat/ChartRenderer.tsx`：用 `ReactECharts` +
 * `notMerge` 防止 setOption 串台；option 用 `useMemo` 避免重渲。
 * 类型用 EChartsOption（echarts 5.x 内置），保证 option 写错会编译期报错。
 */

import { useMemo } from "react";
import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";

interface EChartProps {
  option: EChartsOption;
  height?: number;
  /** 用于 jest mock 标识；测试可以指定 testId 抓节点。 */
  testId?: string;
}

export default function EChart({ option, height = 280, testId }: EChartProps) {
  const memoOption = useMemo(() => option, [option]);
  return (
    <div data-testid={testId}>
      <ReactECharts
        option={memoOption}
        style={{ height, width: "100%" }}
        notMerge
        lazyUpdate
      />
    </div>
  );
}
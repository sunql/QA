/**
 * 把服务端的图表结构离屏渲染成 PNG（导出 PDF 用，0105「图表进最终报告」）。
 *
 * **为什么是前端截图而不是服务端渲染**：ECharts 只能在浏览器里跑。服务端要出图
 * 只有两条路 —— 装无头浏览器（后端镜像陡增）或用 matplotlib 按 11 个 kind 各写一遍
 * （第二套渲染器，与前端必然长得不一样，违反 SSOT）。让前端把**已经渲染过的同一份
 * option** 渲一遍导成图回传，是唯一既零新依赖、又与用户看到的图一致的路子。
 *
 * 三条约束：
 * - **固定亮色 token + 白底**：PDF 页面是白的。跟随暗色主题会得到「白底上的浅色
 *   图」——在屏幕上对，在纸上错。
 * - **离屏容器用完即弃**：`position: fixed` 挪到视口外 + 显式宽高（ECharts 在
 *   零尺寸容器里初始化会拿到 0×0 画布），结束后 `dispose` 并摘掉节点，不留痕迹。
 * - **任何失败都返回 null**：一张图渲不出来不该让整次导出失败 —— 后端对 null
 *   对应的 messageId 回落占位框。
 */
import * as echarts from "echarts";
import { applyChartTheme } from "../theme/chartTheme";
import { LIGHT_TOKEN } from "../theme/tokens";

/** 截图画布尺寸（2 倍像素密度由 getDataURL 的 pixelRatio 负责，这里给 CSS 尺寸）。 */
const SNAPSHOT_WIDTH = 800;
const SNAPSHOT_HEIGHT = 420;

/** PNG data URL 前缀；后端按此校验，改这里必须同步 `session_history_service`。 */
export const PNG_DATA_URL_PREFIX = "data:image/png;base64,";

/** 离屏容器：必须在文档流里（ECharts 依赖 getBoundingClientRect），但不可见。 */
function createOffscreenHost(): HTMLDivElement {
  const host = document.createElement("div");
  host.style.position = "fixed";
  host.style.left = "-99999px";
  host.style.top = "0";
  host.style.width = `${SNAPSHOT_WIDTH}px`;
  host.style.height = `${SNAPSHOT_HEIGHT}px`;
  host.style.pointerEvents = "none";
  host.setAttribute("aria-hidden", "true");
  document.body.appendChild(host);
  return host;
}

/** 等一帧，让 ECharts 把首帧画完（canvas 渲染器下 setOption 后立即出图，但不保证）。 */
function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
    requestAnimationFrame(() => resolve());
  });
}

/**
 * 渲染 + 截图；失败（option 非法 / echarts 抛错 / 无 document）返回 null。
 *
 * `chartOption` 是**服务端结构**（不含颜色），颜色在渲染前用亮色 token 补上，
 * 与 `ChartRenderer` 走同一套 `applyChartTheme`。
 */
export async function renderChartPng(
  chartOption: Record<string, unknown>
): Promise<string | null> {
  if (typeof document === "undefined") return null;
  const host = createOffscreenHost();
  let chart: echarts.ECharts | null = null;
  try {
    chart = echarts.init(host, undefined, {
      width: SNAPSHOT_WIDTH,
      height: SNAPSHOT_HEIGHT,
      renderer: "canvas",
    });
    // 关掉动画：PDF 是静态图，等动画收敛既慢又可能截到中间帧
    chart.setOption({ ...applyChartTheme(chartOption, LIGHT_TOKEN), animation: false }, true);
    await nextFrame();
    const dataUrl = chart.getDataURL({
      type: "png",
      pixelRatio: 2,
      // 白底：PNG 默认透明，透明图贴在白页上没问题，但某些阅读器的透明处理不一致
      backgroundColor: "#fff",
    });
    return dataUrl.startsWith(PNG_DATA_URL_PREFIX) ? dataUrl : null;
  } catch {
    // 故意不吞掉用户可见的错误提示：导出流程是「尽力带图」，缺图不是失败
    return null;
  } finally {
    chart?.dispose();
    host.remove();
  }
}

/** 该 kind 是否需要走截图：table/kpi 由后端原生排版（比位图清晰），不截图。 */
export function needsSnapshot(chartType: string): boolean {
  return chartType !== "table" && chartType !== "kpi";
}

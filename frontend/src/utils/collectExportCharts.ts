/**
 * 导出 PDF 前收集图表位图（0105「图表进最终报告」）。
 *
 * **以服务端的消息流为准，而不是以屏幕上的消息为准**。三条理由：
 * 1. 位图要按 `SessionMessage` 主键归属，而实时会话的前端消息**根本没有 DB id**
 *    （`dbMessageId` 从未被回填）。拿屏幕上的列表去猜 id 是在猜。
 * 2. 导出必须覆盖**已经滚出屏幕甚至已经不在当前列表里的轮次** —— 这正是当初要给
 *    `session_message` 加列的原因。
 * 3. 服务端返回的 `chartOption` 就是**当时落库的那份结构**，与历史回放画出来的图
 *    一致；用屏幕上的现值反而会让「看到的图」与「导出的图」分叉。
 *
 * 因此：GET 一次消息流 → 挑出需要截图的轮次 → 离屏渲成 PNG → 交给导出端点。
 * `table` / `kpi` 不在此列（后端原生排版，比位图清晰）。
 */
import { loadSessionMessages } from "../api/chatHistory";
import type { ExportChartImage } from "../api/chatHistory";
import { asChartOption, normalizeChartType } from "./chartContract";
import type { ChatMessageRead } from "../types/chatHistory";
import { needsSnapshot, renderChartPng } from "./chartSnapshot";

/**
 * 一次导出最多截多少张图。与后端 `_MAX_EXPORT_CHART_IMAGES`(200) 同量级但留出余量：
 * 超限后端会 422，而「超限就整份导出失败」不是我们想要的降级方式 —— 宁可少带几张
 * 图，也要把报告导出来。前端先截断，后端那道闸只作为对越权调用的防御。
 */
export const MAX_EXPORT_CHARTS = 180;

/**
 * 单个会话最多读多少条消息去挑图。
 *
 * 必须与**导出侧**的窗口对齐：后端导出只保留**最后 500 轮**（约 1000 条消息），
 * 所以这里用 `tail: true` 取**最新**的 1000 条。取最早 1000 条在长会话里与导出
 * 窗口**完全不相交** —— 结果是每张图都配不上，PDF 里全是灰占位框，且不报任何错。
 */
const MESSAGE_SCAN_LIMIT = 1000;

/**
 * 同时最多渲染几张。**不能一次并发全部**：`renderChartPng` 在 `await` 之前是同步
 * 执行的，`Promise.all` 会在一个不可中断的主线程突发里建出全部离屏容器与 ECharts
 * 实例 —— 每张画布 800×420 CSS、`pixelRatio 2` ⇒ 约 5.4 MB 后备存储，180 张就是
 * 近 1 GB 加 180 个 DOM 节点，中端机器直接卡死甚至 OOM。分块后峰值降到 4 张。
 */
const RENDER_CONCURRENCY = 4;

/** 这条消息是否需要截一张图：assistant 行 + 合法 chartType + 该 kind 走截图。 */
function isSnapshotTarget(message: ChatMessageRead): boolean {
  if (message.role !== "assistant") return false;
  const chartType = normalizeChartType(message.chartType);
  return chartType !== null && needsSnapshot(chartType);
}

/** 渲染一张；失败返回 null（调用方丢掉该张，不影响其余）。 */
async function renderOne(message: ChatMessageRead): Promise<ExportChartImage | null> {
  const chartOption = asChartOption(message.chartOption);
  if (!chartOption) return null;
  const imagePng = await renderChartPng(chartOption);
  return imagePng ? { messageId: message.id, imagePng } : null;
}

/** 分块渲染，块内并发、块间串行 —— 顺序与入参一致，并发上限为 RENDER_CONCURRENCY。 */
async function renderAll(
  targets: ChatMessageRead[]
): Promise<ExportChartImage[]> {
  const rendered: ExportChartImage[] = [];
  for (let start = 0; start < targets.length; start += RENDER_CONCURRENCY) {
    const chunk = targets.slice(start, start + RENDER_CONCURRENCY);
    const results = await Promise.all(chunk.map(renderOne));
    for (const result of results) {
      if (result) rendered.push(result);
    }
  }
  // 逐张 warning 在 180 张时就是刷屏；一条汇总既能被 grep 到，又不淹没有效日志。
  // 全量失败通常意味着渲染链整体坏了（CSP 变更、echarts 升级、主题回归），
  // 那正是最需要留下痕迹的情形。
  if (rendered.length < targets.length) {
    console.warn(
      `导出配图：${targets.length} 张中 ${targets.length - rendered.length} 张渲染失败，` +
        `这些轮次在 PDF 里回落为占位框`
    );
  }
  return rendered;
}

/**
 * 收集某会话的图表位图。
 *
 * `messageId` 非空时只处理该条（与单条问答导出对齐）。任何一张渲染失败都只是
 * 少一张图 —— 后端对该轮回落占位框，导出照常成功。
 */
export async function collectExportCharts(
  sessionId: string,
  messageId?: number
): Promise<ExportChartImage[]> {
  let messages: ChatMessageRead[];
  try {
    // tail：取最新的一批，对齐导出 PDF 的「最后 500 轮」窗口
    const resp = await loadSessionMessages(sessionId, MESSAGE_SCAN_LIMIT, {
      tail: true,
    });
    messages = resp.messages;
  } catch (error: unknown) {
    // 图是增强不是主功能：拿不到消息流就整份不带图导出，而不是让导出失败。
    // 但**不能一声不吭** —— 这条路径若被系统性触发（接口改名、权限变化），
    // 表现是「导出的 PDF 永远没有图」，不留痕迹就只能靠猜。
    console.warn("导出配图：消息流加载失败，本次导出不带图", error);
    return [];
  }

  const targets = messages
    .filter((m) => (messageId === undefined || m.id === messageId) && isSnapshotTarget(m))
    // 取**最新**的 MAX_EXPORT_CHARTS 张：宁可少带最早的几张，也要让用户
    // 眼前这批（也是导出 PDF 里最后那批）有图。
    .slice(-MAX_EXPORT_CHARTS);

  return renderAll(targets);
}

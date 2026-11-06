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
import { chartHeightOf } from "./chartLayout";
import { defaultChartOption } from "./chartTopN";
import { asReportSections } from "./reportSections";
import type { ChatMessageRead } from "../types/chatHistory";
import { needsSnapshot, renderChartPng } from "./chartSnapshot";

/**
 * 一次导出最多截多少张图（绝对条数上限）。与后端 `_MAX_EXPORT_CHART_IMAGES`(200)
 * 同量级但留出余量。**真正的约束通常是像素预算** `MAX_EXPORT_PIXELS` —— 图表高度
 * 随类别数增长后，几十张 960 高图就能顶到上限。
 */
export const MAX_EXPORT_CHARTS = 180;

/** 截图位图的像素宽：800 CSS × `pixelRatio` 2。 */
const SNAPSHOT_PIXEL_WIDTH = 1600;

/** `getDataURL` 的像素密度：图片像素高 = CSS 高度 × 2。 */
const SNAPSHOT_PIXEL_RATIO = 2;

/**
 * 单次导出的累计像素预算。后端硬上限 40M（`_MAX_EXPORT_TOTAL_IMAGE_PIXELS`），
 * 超了整份导出 422；前端取 90% 留余量，宁可少带最早的几张也要让报告导出来。
 */
export const MAX_EXPORT_PIXELS = 36_000_000;

/**
 * 单张位图**解码后**的字节上限，与后端 `MAX_EXPORT_IMAGE_BYTES`（2 MiB）对齐。
 *
 * 为什么前端也要卡：后端在**解析阶段**按同一上界拒绝整个请求（422），超一张
 * 就是「导出失败」而不是「少一张图」。正常产物（1600 宽 × ≤1920 高）远到不了
 * 2 MiB，所以这里先丢图，让报告照常导出来。
 */
export const MAX_EXPORT_IMAGE_BYTES = 2 * 1024 * 1024;

/**
 * 单次导出**全部**位图解码后的字节预算，与后端 `MAX_EXPORT_TOTAL_IMAGE_BYTES`
 * （20 MiB）对齐并留 20% 余量。
 *
 * 为什么像素预算之外还要这一维：矢量化很弱的图（散点、多折线）单张像素不超但
 * PNG 很大，35 张 600 KB 的图仍在 36M 像素以内，解码字节却已越过 20 MiB ——
 * 请求发出后端 422，整份导出作废。字节数只有渲染完才知道，所以在渲染后裁剪。
 */
export const MAX_EXPORT_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024;

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
 * 实例 —— 每张画布 800 CSS 宽 × 动态高度（最高 960）、`pixelRatio 2` 的后备存储，
 * 几十张一次建出就会卡死甚至 OOM。分块后峰值降到 4 张。
 */
const RENDER_CONCURRENCY = 4;

/**
 * 一张待截图的图。`stepIndex` 缺省 = 该轮顶层图（0105 行为）；多步报告的正文图
 * 按步骤归属 —— PDF 会把它排进对应那一节的标题下（2026-10-31 D1）。
 */
interface ExportTarget {
  messageId: number;
  stepIndex?: number;
  chartOption: Record<string, unknown>;
}

/**
 * `stepsSnapshot` → `stepIndex → chartOption`。快照是落库的原始 JSON，按系统边界
 * 逐项收窄：图型必须白名单认识、且该图型走截图（`table` / `kpi` 后端原生排版）。
 */
function stepChartOptionsOf(
  message: ChatMessageRead
): Map<number, Record<string, unknown>> {
  const options = new Map<number, Record<string, unknown>>();
  if (!Array.isArray(message.stepsSnapshot)) return options;
  for (const raw of message.stepsSnapshot) {
    if (raw === null || typeof raw !== "object") continue;
    const step = raw as Record<string, unknown>;
    const stepIndex = step.stepIndex;
    if (typeof stepIndex !== "number" || !Number.isInteger(stepIndex)) continue;
    const chartType = normalizeChartType(step.chartType);
    const chartOption = asChartOption(step.chartOption);
    if (chartType === null || chartOption === null || !needsSnapshot(chartType)) {
      continue;
    }
    options.set(stepIndex, chartOption);
  }
  return options;
}

/**
 * 这条消息要截哪些图。
 *
 * 有分节 chart 块 ⇒ 按块逐张（**跳过该轮顶层图**，否则 PDF 正文里同一张图会
 * 出现两次）；分节块指向快照里没有的步骤时跳过该块 —— 后端对这种块回落占位框，
 * 不会因为少一张图就导出失败。无分节块 ⇒ 沿用顶层图（老消息 / 单步轮次）。
 */
function targetsOf(message: ChatMessageRead): ExportTarget[] {
  if (message.role !== "assistant") return [];
  const chartBlocks = (asReportSections(message.reportSections) ?? []).filter(
    (block) => block.kind === "chart"
  );
  if (chartBlocks.length > 0) {
    const stepOptions = stepChartOptionsOf(message);
    return chartBlocks.flatMap((block) => {
      if (block.kind !== "chart") return [];
      const chartOption = stepOptions.get(block.stepIndex);
      return chartOption
        ? [{ messageId: message.id, stepIndex: block.stepIndex, chartOption }]
        : [];
    });
  }

  const chartType = normalizeChartType(message.chartType);
  const chartOption = asChartOption(message.chartOption);
  if (chartType === null || chartOption === null || !needsSnapshot(chartType)) {
    return [];
  }
  return [{ messageId: message.id, chartOption }];
}

/**
 * 要截图的 option：**与屏幕上默认展示的一致**（有 Top-N 视图就用截断视图）。
 * 导出的 PDF 是给人看的成品，画出来的不该是用户没见过的全量长条图；
 * 高度/像素预算也按同一份算，否则预算与实际位图对不上。
 */
function snapshotOptionOf(target: ExportTarget): Record<string, unknown> {
  return defaultChartOption(target.chartOption);
}

/** data URL 里 base64 段的**解码后**字节数（估算即可：这是防御性闸门，不是校验）。 */
function decodedByteLength(imagePng: string): number {
  const commaIndex = imagePng.indexOf(",");
  const base64Length = (commaIndex === -1 ? imagePng : imagePng.slice(commaIndex + 1)).length;
  return Math.floor((base64Length * 3) / 4);
}

/** 渲染一张；失败、抛异常或超过单张上限返回 null（调用方丢掉该张，不影响其余）。 */
async function renderOne(target: ExportTarget): Promise<ExportChartImage | null> {
  let imagePng: string | null;
  try {
    imagePng = await renderChartPng(snapshotOptionOf(target));
  } catch (error: unknown) {
    // 渲染链内部异常（如离屏容器的 dispose 失败）不能顺着 Promise.all 把整份导出
    // 打成失败 —— 「一张图失败 = 少一张图」是这条路径的契约。
    console.warn(`导出配图：messageId=${target.messageId} 渲染异常，已跳过`, error);
    return null;
  }
  if (!imagePng) return null;
  if (decodedByteLength(imagePng) > MAX_EXPORT_IMAGE_BYTES) return null;
  return {
    messageId: target.messageId,
    ...(target.stepIndex === undefined ? {} : { stepIndex: target.stepIndex }),
    imagePng,
  };
}

/** 分块渲染，块内并发、块间串行 —— 顺序与入参一致，并发上限为 RENDER_CONCURRENCY。 */
async function renderAll(
  targets: ExportTarget[]
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
      `导出配图：${targets.length} 张中 ${targets.length - rendered.length} 张未能导出` +
        `（渲染失败或超过单张 ${MAX_EXPORT_IMAGE_BYTES} 字节上限），这些轮次在 PDF 里回落为占位框`
    );
  }
  return rendered;
}

/** 一张图上传后的像素数：1600 宽 × 2H 高（与 `chartSnapshot` 的 800 CSS × 2 对齐）。 */
function pixelCostOf(target: ExportTarget): number {
  return (
    SNAPSHOT_PIXEL_WIDTH * SNAPSHOT_PIXEL_RATIO * chartHeightOf(snapshotOptionOf(target))
  );
}

/**
 * 从最新往旧累加像素，超预算即停 —— 丢掉的是**最早**的消息，保留用户眼前这批
 * （也是导出 PDF 里最后那批）。返回按时间正序，交给渲染层。
 */
function trimToPixelBudget(targets: ExportTarget[]): ExportTarget[] {
  const kept: ExportTarget[] = [];
  let totalPixels = 0;
  for (let index = targets.length - 1; index >= 0; index -= 1) {
    const cost = pixelCostOf(targets[index]);
    if (totalPixels + cost > MAX_EXPORT_PIXELS) break;
    totalPixels += cost;
    kept.push(targets[index]);
  }
  if (kept.length < targets.length) {
    console.warn(
      `导出配图：像素预算 ${MAX_EXPORT_PIXELS} 已用尽，丢弃最早的 ${
        targets.length - kept.length
      } 张`
    );
  }
  return kept.reverse();
}

/**
 * 从最新往旧累加**解码后**字节，超预算即停 —— 与像素预算同一策略：丢最早的一批，
 * 保住用户眼前这批。渲染完才知道字节数，所以在渲染后裁剪。
 */
function trimToByteBudget(images: ExportChartImage[]): ExportChartImage[] {
  const kept: ExportChartImage[] = [];
  let totalBytes = 0;
  for (let index = images.length - 1; index >= 0; index -= 1) {
    const size = decodedByteLength(images[index].imagePng);
    if (totalBytes + size > MAX_EXPORT_TOTAL_IMAGE_BYTES) break;
    totalBytes += size;
    kept.push(images[index]);
  }
  if (kept.length < images.length) {
    console.warn(
      `导出配图：总字节预算 ${MAX_EXPORT_TOTAL_IMAGE_BYTES} 已用尽，丢弃最早的 ${
        images.length - kept.length
      } 张`
    );
  }
  return kept.reverse();
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

  const targets = trimToPixelBudget(
    messages
      .filter((m) => messageId === undefined || m.id === messageId)
      // 一条多步消息可以出多张分节图，所以先摊平再限条数
      .flatMap(targetsOf)
      // 先取**最新**的 MAX_EXPORT_CHARTS 张（绝对条数上限），再按像素预算裁剪：
      // 宁可少带最早的几张，也要让用户眼前这批（也是导出 PDF 里最后那批）有图。
      .slice(-MAX_EXPORT_CHARTS)
  );

  return trimToByteBudget(await renderAll(targets));
}

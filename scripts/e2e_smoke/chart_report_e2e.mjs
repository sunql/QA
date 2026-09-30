#!/usr/bin/env node
/**
 * 端到端冒烟：图表进「最终报告」（0105）—— 真实链路，不 mock。
 *
 * 验证的是**部署后的容器**（nginx :5173 → backend :8000 → 真 PG + 真 LLM），
 * 而不是测试替身。断言的依据尽量取**网络层**（比 DOM 稳），DOM 只做一处可见性兜底：
 *
 *   1. UI 登录拿到 Bearer（AUTH_MODE=real，stub 头走不通）
 *   2. 发一个问题 → 抓 /chat/stream 的 SSE 原文：
 *      **`chart` 帧必须排在最后一个 `token` 帧之后、`done` 之前**（Part A 契约：
 *      最终回答带图；不是只在分析计划卡里）
 *   3. 断言的锚点：`done` 帧里的 `steps` 数量（=1 单步 / >1 多步），
 *      多步时顶层 chart 走的就是 Part A 新逻辑
 *   4. 点「导出 PDF」→ 抓两个请求：
 *      - 请求体 `charts[].imagePng` 必须以 `data:image/png;base64,` 开头
 *        （证明前端**真的渲染出了 PNG**，而不是空数组走占位框）
 *      - 响应是 `%PDF-` 且含 `/Subtype /Image`（证明图**真的嵌进了 PDF**）
 *   5. 刷新页面（不自动回放）→ 展开历史面板 → 点「当前」会话载回 → 从历史载回同一会话 → 最终回答里仍有图（0105 持久化生效）
 *   6. 反向守卫：`/messages?tail=true` 的响应里 assistant 行必须带 chartType
 *
 * 用法（凭据只从环境变量来，脚本里不落任何口令）：
 *   cd frontend && E2E_USER=admin E2E_PASSWORD='<口令>' \
 *     node ../scripts/e2e_smoke/chart_report_e2e.mjs
 * 可选：E2E_BASE_URL（默认 http://localhost:5173）、E2E_QUESTION。
 */

import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";

const OUT_DIR = "/tmp/e2e_chart_report";
mkdirSync(OUT_DIR, { recursive: true });

const BASE_URL = process.env.E2E_BASE_URL ?? "http://localhost:5173";
const USERNAME = process.env.E2E_USER ?? "admin";
const QUESTION = process.env.E2E_QUESTION ?? "各采购组织的收货数量";

// 口令**没有默认值**：这是真实 auth 的线上栈，写死一个口令等于把凭据提交进仓库。
// 缺了就fail fast，别让脚本用一个猜的口令去撞登录接口。
const PASSWORD = process.env.E2E_PASSWORD;
if (!PASSWORD) {
  console.error("缺少 E2E_PASSWORD —— 用法：E2E_USER=admin E2E_PASSWORD='<口令>' node ...");
  process.exit(1);
}

const failures = [];

function step(name, fn) {
  return (async () => {
    process.stdout.write(`▶ ${name} ... `);
    try {
      const out = await fn();
      console.log("✅", out ?? "");
      return true;
    } catch (e) {
      failures.push(`${name}: ${e.message ?? e}`);
      console.log("❌", e.message ?? e);
      throw e;
    }
  })();
}

/**
 * 轮询等待探针被填上。
 *
 * 必须轮询，不能拿到 response 就直接断言：`page.on("response")` 在**响应头**到达时
 * 触发，而我还要 `await resp.body()` 才能拿到 PDF 字节 —— 那个 await 是异步的，
 * 断言会跑在它前面，于是看到「没抓到」。这不是应用的问题，是探针的时序问题。
 */
async function waitFor(getter, label, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const v = getter();
    if (v) return v;
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`等 ${label} 超时（${timeoutMs}ms）`);
}

/** SSE 原文 → 帧数组（与后端 test_chat_stream_api._parseFrames 同口径）。 */
function parseFrames(text) {
  const frames = [];
  for (const block of text.split("\n\n")) {
    if (!block.trim()) continue;
    let event = null;
    let data = null;
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7);
      else if (line.startsWith("data: ")) {
        try {
          data = JSON.parse(line.slice(6));
        } catch {
          data = null;
        }
      }
    }
    frames.push({ event, data });
  }
  return frames;
}

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  acceptDownloads: true,
});
const page = await ctx.newPage();

// ── 网络探针 ─────────────────────────────────────────────────────────
let streamText = null; // /chat/stream 的 SSE 原文
let exportRequestBody = null; // 导出 POST 的 body
let exportRequestHeaders = null; // 导出 POST 的请求头（鉴定 Bearer）
let exportResponse = null; // 导出的 PDF 字节 + 状态
let messagesTailOk = null; // /messages?tail=true 的响应
let messagesLoaded = null; // 任意一次 /messages 响应（点历史项载回时）

page.on("response", async (resp) => {
  const url = resp.url();
  try {
    if (url.includes("/chat/stream") && resp.status() === 200) {
      streamText = await resp.text();
    }
    if (url.includes("/export.pdf")) {
      const req = resp.request();
      exportRequestBody = req.postDataJSON?.() ?? null;
      exportRequestHeaders = req.headers();
      const buf = await resp.body();
      exportResponse = { status: resp.status(), bytes: buf };
    }
    if (url.includes("/messages?") && resp.status() === 200) {
      const body = await resp.json();
      messagesLoaded = body;
      if (url.includes("tail=true")) messagesTailOk = body;
    }
  } catch {
    /* 响应体读取失败不影响主流程；断言处会表现为缺失 */
  }
});

try {
  await step("1. UI 登录（真实 auth）", async () => {
    await page.goto(`${BASE_URL}/login`, { waitUntil: "networkidle" });
    await page.locator('input[autocomplete="username"]').fill(USERNAME);
    await page.locator('input[autocomplete="current-password"]').fill(PASSWORD);
    await page.locator('button[type="submit"]').click();
    await page.waitForResponse(
      (r) => r.url().includes("/api/v1/auth/login") && r.status() === 200,
      { timeout: 15_000 }
    );
    await page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 10_000 });
    return `已登录，URL=${page.url()}`;
  });

  await step("2. 打开对话页 + 选数据源", async () => {
    await page.goto(`${BASE_URL}/chat`, { waitUntil: "networkidle" });
    await page.screenshot({ path: `${OUT_DIR}/01_chat_page.png`, fullPage: true });
    // 数据源下拉（placeholder「选择数据源」）：不选就发不出去
    const select = page.locator(".ant-select", { hasText: "选择数据源" }).first();
    if ((await select.count()) > 0) {
      await select.click();
      const first = page.locator(".ant-select-dropdown:visible .ant-select-item-option").first();
      await first.waitFor({ timeout: 10_000 });
      const label = (await first.innerText()).trim();
      await first.click();
      return `已选数据源：${label}`;
    }
    return "数据源已有默认值（未出现空下拉）";
  });

  await step("3. 提问 → 抓 /chat/stream 原文", async () => {
    await page.locator('textarea[placeholder^="输入自然语言问题"]').fill(QUESTION);
    await page.locator('button:has-text("发送")').click();
    await page.waitForResponse((r) => r.url().includes("/chat/stream"), { timeout: 180_000 });
    // SSE 是长连接：`waitForLoadState("networkidle")` 在这里**不可靠**（流没关就永远
    // 不 idle）。改成轮询响应体里出现 done 帧 —— 那才是「这一轮说完了」的判据。
    await waitFor(
      () => (streamText?.includes("event: done") ? streamText : null),
      "done 帧",
      180_000
    );
    return `SSE ${streamText.length} 字节`;
  });

  let stepCount = 0;
  await step("4. 断言 chart 帧的位置符合该路径的契约", async () => {
    const frames = parseFrames(streamText);
    const events = frames.map((f) => f.event);
    // 诊断信息先落盘：断言失败时最需要的就是这份序列
    writeFileSync(`${OUT_DIR}/sse_events.txt`, events.join("\n"));
    const idxDone = events.indexOf("done");
    if (idxDone < 0) throw new Error("没有 done 帧");
    const done = frames[idxDone].data ?? {};
    // done 帧带 steps：>1 = 多步（顶层图走 Part A 新逻辑），=1 = 单步（既有路径）
    stepCount = Array.isArray(done.steps) ? done.steps.length : 0;

    const chartIdxs = events.reduce((acc, e, i) => (e === "chart" ? [...acc, i] : acc), []);
    if (chartIdxs.length === 0) {
      throw new Error(`没有 chart 帧；步骤数=${stepCount}，序列=${events.join(",")}`);
    }
    const lastChart = chartIdxs[chartIdxs.length - 1];
    const chartFrame = frames[lastChart].data ?? {};
    if (!chartFrame.chartType) throw new Error("chart 帧没有 chartType");

    if (stepCount > 1) {
      // Part A 契约：多步的**顶层**图必须排在最后一个 token 之后、done 之前 ——
      // 「图出现在最终回答里」，而不是只在分析计划卡里。
      const lastToken = events.lastIndexOf("token");
      if (!(lastChart > lastToken)) {
        throw new Error(
          `多步(${stepCount}) 的顶层 chart 帧(${lastChart}) 没排在最后 token(${lastToken}) 之后`
        );
      }
      if (!(lastChart < idxDone)) throw new Error(`chart 帧(${lastChart}) 没排在 done 之前`);
    } else {
      // 单步既有路径（早于本变更）：chart → token… → done，序列与
      // `test_chat_stream_api` 钉的一致。这里只要求它在 done 之前。
      if (!(lastChart < idxDone)) throw new Error(`chart 帧(${lastChart}) 没排在 done 之前`);
    }
    return `chartType=${chartFrame.chartType}, 数据行=${(chartFrame.data ?? []).length}, 步骤数=${stepCount}, chart 帧位置=${lastChart}（token 共 ${events.filter((e) => e === "token").length} 个）`;
  });

  await step("5. 最终回答里出现了图（DOM 兜底）", async () => {
    // ReactECharts 渲染出 canvas；KPI/表格不算（那不是「图」）。
    // 这条断言之所以**够强**：`MultiStepPlanCard` 用 antd Collapse 且没设
    // `defaultActiveKey` ⇒ 折叠态下 antd **不渲染**子节点，计划卡里那些「每步图」
    // 根本不在 DOM 里。所以页面上出现 canvas，就是最终回答那张（Part A 的目标）。
    await page.waitForSelector("canvas", { timeout: 30_000 });
    const canvases = await page.locator("canvas").count();
    const planPanelsExpanded = await page
      .locator(".ant-collapse-item-active")
      .count();
    await page.screenshot({ path: `${OUT_DIR}/02_answer_with_chart.png`, fullPage: true });
    return `页面 canvas 数 = ${canvases}（展开中的计划卡 = ${planPanelsExpanded}）`;
  });

  await step("6. 点「导出 PDF」→ 前端确实渲了 PNG 且后端确实嵌进 PDF", async () => {
    await page.locator('button[aria-label="导出当前会话为 PDF"]').click();
    // 截图 + 生成 PDF 需要时间
    await page.waitForResponse((r) => r.url().includes("/export.pdf"), { timeout: 180_000 });
    // 头部已到 ≠ 探针已填（见 waitFor 的注释）
    await waitFor(() => exportRequestBody && exportResponse, "导出请求体/响应体", 120_000);
    if (!exportRequestBody) throw new Error("导出请求体没抓到");
    // 裸 axios 不走 httpClient 拦截器 ⇒ Bearer 必须由 authHeaders() 手工注入。
    // 少了它 sessions router（router 级鉴权）直接 403「请先登录」，且这缺陷在
    // 单测里看不见（mock 的 defaults 曾虚构出 X-User-Id）。
    const auth = String(exportRequestHeaders?.authorization ?? "");
    if (!auth.startsWith("Bearer ")) {
      throw new Error(
        `导出请求没带 Bearer（authorization=${JSON.stringify(auth)}）⇒ 会 403`
      );
    }
    const charts = exportRequestBody.charts ?? [];
    if (charts.length === 0) {
      throw new Error(
        `导出的 charts 为空 ⇒ 前端没截到图（会回落占位框）。body=${JSON.stringify(
          exportRequestBody
        ).slice(0, 200)}`
      );
    }
    const bad = charts.filter((c) => !String(c.imagePng ?? "").startsWith("data:image/png;base64,"));
    if (bad.length > 0) throw new Error(`${bad.length} 张图的 imagePng 前缀不对`);
    // 顺带钉一条：每张截图都必须挂在某个消息 id 上（后端会拿它校验 session 归属）
    const noId = charts.filter((c) => typeof c.messageId !== "number");
    if (noId.length > 0) throw new Error(`${noId.length} 张图缺 messageId`);
    if (!exportResponse) throw new Error("导出响应没抓到");
    if (exportResponse.status !== 200) throw new Error(`导出返回 ${exportResponse.status}`);
    const pdf = exportResponse.bytes;
    if (!pdf.subarray(0, 5).toString("latin1").startsWith("%PDF-")) {
      throw new Error("响应不是 PDF");
    }
    if (!pdf.includes(Buffer.from("/Subtype /Image"))) {
      throw new Error("PDF 里没有图像对象 —— 图没嵌进去（回落成占位框了）");
    }
    writeFileSync(`${OUT_DIR}/export.pdf`, pdf);
    const kb = Math.round(pdf.length / 1024);
    return `${charts.length} 张截图 → PDF ${kb} KB，含 /Subtype /Image`;
  });

  await step("7. 刷新 → 从历史面板点回该会话 → 图仍在（0105 持久化）", async () => {
    // 刷新**不会**自动回放历史：store 只从 localStorage 恢复 sessionId，而
    // `loadSessionMessages` 仅在点击历史项时调用 —— 这是既有设计，不是本变更引入的。
    // 所以走真实用户路径：刷新 → 展开历史面板 → 点最新那条（刚问完，排第一）。
    await page.reload({ waitUntil: "networkidle" });
    const toggle = page.locator('button[aria-label="展开历史"]');
    if ((await toggle.count()) > 0) await toggle.first().click();
    const firstRow = page.locator(".ant-list-item").first();
    await firstRow.waitFor({ timeout: 30_000 });
    await page.screenshot({ path: `${OUT_DIR}/03_history_panel.png`, fullPage: true });
    await firstRow.click();
    await page.waitForSelector("canvas", { timeout: 60_000 });
    await page.screenshot({ path: `${OUT_DIR}/04_after_reload.png`, fullPage: true });

    // 把 DOM 上的 canvas 钉到**落库字段**上：载回用的是 /messages（不是导出那条
    // ?tail=true），响应里 assistant 行必须自带 chartType/chartOption。
    const msgs = await waitFor(
      () => messagesLoaded?.messages?.length ? messagesLoaded : null,
      "点历史项后的 /messages 响应",
      30_000
    );
    const assistant = msgs.messages.filter((m) => m.role === "assistant");
    const withChart = assistant.filter((m) => m.chartType);
    if (withChart.length === 0) {
      throw new Error(
        `画布出来了但 /messages 里 assistant 行没有 chartType ⇒ 图不是从落库字段渲染的。样例=${JSON.stringify(assistant[0]).slice(0, 200)}`
      );
    }
    return `载回 ${msgs.messages.length} 条消息，其中 ${withChart.length} 条带 chartType=${withChart[0].chartType}，canvas 已渲染`;
  });

  await step("8. 反向守卫：/messages?tail=true 的 assistant 行带 chartType", async () => {
    await waitFor(() => messagesTailOk, "/messages?tail=true 响应", 30_000).catch(() => {
      throw new Error("没抓到 /messages?tail=true 响应（导出时前端会发）");
    });
    const msgs = messagesTailOk.messages ?? [];
    const assistant = msgs.filter((m) => m.role === "assistant");
    if (assistant.length === 0) throw new Error("响应里没有 assistant 行");
    const withChart = assistant.filter((m) => m.chartType);
    if (withChart.length === 0) {
      throw new Error(
        `assistant 行都没有 chartType ⇒ 图没落库。样例=${JSON.stringify(assistant[0]).slice(0, 200)}`
      );
    }
    return `${assistant.length} 条 assistant，其中 ${withChart.length} 条带 chartType=${withChart[0].chartType}`;
  });

  console.log("\n=== 小结 ===");
  console.log(`  问题：${QUESTION}`);
  console.log(`  步骤数：${stepCount}（1=单步；>1=多步，顶层图走 Part A 新逻辑）`);
  console.log(`  产物：${OUT_DIR}/（export.pdf、SSE 事件序列、3 张截图）`);
  console.log(`\n✅ 8 步全过（${failures.length} 失败）`);
} catch (e) {
  await page
    .screenshot({ path: `${OUT_DIR}/FAIL_${Date.now()}.png`, fullPage: true })
    .catch(() => {});
  console.error(`\n❌ 冒烟失败：${e.message ?? e}`);
  if (failures.length > 0) console.error(`   已记录的失败：\n     - ${failures.join("\n     - ")}`);
  process.exitCode = 1;
} finally {
  await browser.close();
}

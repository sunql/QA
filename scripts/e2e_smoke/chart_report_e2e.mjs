#!/usr/bin/env node
/**
 * 端到端冒烟：可视化输出策略（图/表/图+表 + 判断依据，0107）—— 真实链路，不 mock。
 *
 * 验证的是**部署后的容器**（nginx :5173 → backend :8000 → 真 PG + 真 LLM），
 * 而不是测试替身。断言的依据尽量取**网络层**（比 DOM 稳），DOM 只做可见性兜底：
 *
 *   1. UI 登录拿到 Bearer（AUTH_MODE=real，stub 头走不通）
 *   2. 发一个问题 → 抓 /chat/stream 的 SSE 原文。单步 vs 多步的判据是**有没有 chart 事件**：
 *      - 单步：chart 事件必发（图 + 折叠数据表 tableOption + 判断依据 visualRationale 三件套）
 *      - 多步（0107 反转，原 0105 的顶层继承图取消）：**不再发 chart 事件** —— 顶层
 *        最终回答是纯文字 + `done` 帧里的 `SUMMARY_TEXT_ONLY` 依据；每个数据步的
 *        图/表/依据走各自的 `step_result`
 *   3. DOM 兜底：单步 canvas + 折叠「数据表」面板；多步无 canvas + 汇总依据行可见
 *   4. 点「导出 PDF」：单步回传 PNG 且嵌进 PDF；多步是纯文字 PDF（无图是**预期**）
 *   5. 刷新 → 会话恢复：单步带图（chartType 落库）；多步带 SUMMARY_TEXT_ONLY 依据落库
 *   6. 反向守卫：多步顶层**不再**出现 chart 事件/字段（防「改完还发」），汇总依据
 *      **必须**可见（防「去图连依据也丢了」）
 *
 * 用法（凭据只从环境变量来，脚本里不落任何口令）：
 *   cd frontend && E2E_USER=admin E2E_PASSWORD='<口令>' \
 *     node ../scripts/e2e_smoke/chart_report_e2e.mjs
 * 可选：E2E_BASE_URL（默认 http://localhost:5173）、E2E_QUESTION（默认单步问题；
 * 多步分支需把 E2E_QUESTION 换成能让 planner 拆成 ≥2 个数据步的问法）。
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
let messagesLoaded = null; // 最近一次 /messages 响应
let messagesUrl = null; // 最近一次 /messages 的 URL（鉴定 tail=true）
// /messages 响应计数：刷新前导出那次也会发 /messages?tail=true，光看「messagesLoaded 非空」
// 分不清抓到的是刷新前那次还是刷新后自动回放那次。用序号划一道分界线。
let messagesSeq = 0;

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
      messagesUrl = url;
      messagesSeq += 1;
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
  let isMultiStep = false;
  await step("4. 断言 SSE 事件序列符合路径契约（0107：多步顶层无图）", async () => {
    const frames = parseFrames(streamText);
    const events = frames.map((f) => f.event);
    // 诊断信息先落盘：断言失败时最需要的就是这份序列
    writeFileSync(`${OUT_DIR}/sse_events.txt`, events.join("\n"));
    const idxDone = events.indexOf("done");
    if (idxDone < 0) throw new Error("没有 done 帧");
    const done = frames[idxDone].data ?? {};
    // done 帧带 steps 时是多步汇总（数据步列表）；单步 done 帧不带 steps。
    stepCount = Array.isArray(done.steps) ? done.steps.length : 0;

    // 单步 vs 多步的判据换成「有没有 chart 事件」—— 比 done.steps 稳：
    // 单步流必发一次 chart 事件（图/表/依据三件套都随它下发）；0107 起多步
    // **不再**发 chart 事件（顶层最终回答无图），各数据步的图走 step_result。
    const chartIdxs = events.reduce((acc, e, i) => (e === "chart" ? [...acc, i] : acc), []);
    isMultiStep = chartIdxs.length === 0;

    if (isMultiStep) {
      // 反向守卫 2（防「去图连依据也丢了」）：汇总步必须带 SUMMARY_TEXT_ONLY 依据。
      const vr = done.visualRationale ?? null;
      if (!vr || vr.code !== "SUMMARY_TEXT_ONLY") {
        throw new Error(`多步 done 帧缺 SUMMARY_TEXT_ONLY 依据；actual=${JSON.stringify(vr)}`);
      }
      // 每个数据步的图走 step_result 事件（summary 与 degrade 两个终点都逐数据步 yield
      // step_result；而 done.steps 只在 summary 终点有、degrade 终点没有 —— 别只认 done.steps）。
      const stepResultData = frames
        .filter((f) => f.event === "step_result")
        .map((f) => f.data)
        .filter((d) => d != null);
      const chartedSteps = stepResultData.filter((d) => d.chartType);
      if (chartedSteps.length === 0) {
        throw new Error(
          `多步各数据步的 step_result 都没有 chartType；样例=${JSON.stringify(stepResultData[0]).slice(0, 400)}`
        );
      }
      return `多步：顶层 0 个 chart 事件（反向守卫过），${chartedSteps.length}/${stepResultData.length} 个 step_result 带图，汇总依据=${vr.code}`;
    }

    // 单步三件套：chart 事件在 done 之前，且带 tableOption + visualRationale。
    if (chartIdxs.length === 0) throw new Error(`单步没有 chart 帧；序列=${events.join(",")}`);
    const lastChart = chartIdxs[chartIdxs.length - 1];
    if (!(lastChart < idxDone)) throw new Error(`chart 帧(${lastChart}) 没排在 done 之前`);
    const chartFrame = frames[lastChart].data ?? {};
    if (!chartFrame.chartType) throw new Error("chart 帧没有 chartType");
    const ck = chartFrame.chartType;
    // 图形类必须带 tableOption（图+表）；table/kpi 不附第二份表（表已在 chartOption /
    // 单值卡没有表），此时 tableOption 合法为 null。
    if (ck !== "table" && ck !== "kpi" && !chartFrame.tableOption) {
      throw new Error(`单步图(${ck}) 缺 tableOption ⇒ 图+表三件套没给全`);
    }
    if (!chartFrame.visualRationale || typeof chartFrame.visualRationale.code !== "string") {
      throw new Error("单步 chart 帧缺 visualRationale ⇒ 判断依据没下发");
    }
    return `单步 chartType=${ck}, 数据行=${(chartFrame.data ?? []).length}, tableOption=${chartFrame.tableOption ? "有" : "无(仅表/KPI)"}, rationale=${chartFrame.visualRationale.code}`;
  });

  await step("5. 视觉产物 DOM 兜底（单步 canvas+折叠表 / 多步无图+汇总依据）", async () => {
    if (isMultiStep) {
      // 多步顶层无图 ⇒ 页面上不应有 canvas（各数据步的图在 MultiStepPlanCard 的
      // 折叠 Collapse 里，antd 折叠态不渲染子节点）。汇总依据以次要色一行渲染在答案下方。
      await page.waitForSelector("text=各步骤图表见上方", { timeout: 30_000 });
      const canvases = await page.locator("canvas").count();
      if (canvases > 0) {
        throw new Error(`多步顶层不应渲染图，页面却有 ${canvases} 个 canvas`);
      }
      await page.screenshot({ path: `${OUT_DIR}/02_multistep_summary.png`, fullPage: true });
      return `汇总依据「各步骤图表见上方」可见；canvas 数 = ${canvases}（应为 0）`;
    }
    // 单步：canvas（图）+ 折叠数据表（Collapse label「数据表」）。
    await page.waitForSelector("canvas", { timeout: 30_000 });
    const canvases = await page.locator("canvas").count();
    const dataTablePanels = await page.getByText("数据表", { exact: true }).count();
    if (dataTablePanels === 0) {
      throw new Error("单步图下方没有「数据表」折叠面板 ⇒ tableOption 没渲染");
    }
    await page.screenshot({ path: `${OUT_DIR}/02_answer_with_chart.png`, fullPage: true });
    return `页面 canvas 数 = ${canvases}，数据表折叠面板 = ${dataTablePanels}`;
  });

  await step("6. 点「导出 PDF」→ 单步嵌图 / 多步纯文字", async () => {
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
    if (!exportResponse) throw new Error("导出响应没抓到");
    if (exportResponse.status !== 200) throw new Error(`导出返回 ${exportResponse.status}`);
    const pdf = exportResponse.bytes;
    if (!pdf.subarray(0, 5).toString("latin1").startsWith("%PDF-")) {
      throw new Error("响应不是 PDF");
    }
    const charts = exportRequestBody.charts ?? [];
    writeFileSync(`${OUT_DIR}/export.pdf`, pdf);
    if (isMultiStep) {
      // 0107 反转：多步顶层无图 ⇒ 汇总消息 chartType=null ⇒ collectExportCharts 找不到
      // 可截屏目标，charts 为空是**预期**（不是「图没截到」）。PDF 仍是合法纯文字文档。
      if (charts.length !== 0) {
        throw new Error(`多步导出本应无图，却回了 ${charts.length} 张截图`);
      }
      return `多步纯文字 PDF ${Math.round(pdf.length / 1024)} KB（charts=0，无图是预期）`;
    }
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
    if (!pdf.includes(Buffer.from("/Subtype /Image"))) {
      throw new Error("PDF 里没有图像对象 —— 图没嵌进去（回落成占位框了）");
    }
    const kb = Math.round(pdf.length / 1024);
    return `${charts.length} 张截图 → PDF ${kb} KB，含 /Subtype /Image`;
  });

  await step("7. 刷新 → 不点任何东西 → 上次会话自动回来（会话恢复 + 0105/0107 落库）", async () => {
    // 刷新前先从 localStorage 读恢复指针：它是「刷新后该回哪个会话」的唯一依据。
    // 发送路径不写它，这里就会是 null —— 那正是本变更要修的缺陷（旧行为恒为 null）。
    const pointer = await page.evaluate(() =>
      window.localStorage.getItem("qa:chat:lastSessionId:chat")
    );
    if (!pointer) {
      throw new Error(
        "刷新前 localStorage 里没有 qa:chat:lastSessionId:chat ⇒ 发送路径没写恢复指针"
      );
    }
    const seqBeforeReload = messagesSeq;

    // 刷新后**一次都不点**：不展开历史面板、不点任何一行。上次的会话要自己回来。
    await page.reload({ waitUntil: "networkidle" });
    if (isMultiStep) {
      // 多步：刷新后顶层无图（不等 canvas），等汇总依据行出现即可。
      await page.waitForSelector("text=各步骤图表见上方", { timeout: 60_000 });
    } else {
      await page.waitForSelector("canvas", { timeout: 60_000 });
    }
    await page.screenshot({ path: `${OUT_DIR}/04_after_reload.png`, fullPage: true });

    // 只看**刷新之后**新产生的那次 /messages（序号划界），并且必须按指针去回放最新的 200 条。
    const msgs = await waitFor(
      () => (messagesSeq > seqBeforeReload ? messagesLoaded : null),
      "刷新后自动回放的 /messages 响应",
      30_000
    );
    if (!messagesUrl.includes("tail=true")) {
      throw new Error(
        `自动回放没有取最新窗口（URL 缺 tail=true）⇒ 长会话会恢复成开头。URL=${messagesUrl}`
      );
    }
    if (!decodeURIComponent(messagesUrl).includes(pointer)) {
      throw new Error(
        `自动回放的不是指针指向的会话：指针=${pointer} 请求=${messagesUrl}`
      );
    }

    const assistant = msgs.messages.filter((m) => m.role === "assistant");
    if (isMultiStep) {
      // 反向守卫：多步汇总消息落库后 chartType 为 null（顶层无图），但 visualRationale
      // 必须落库（SUMMARY_TEXT_ONLY）—— 依据不能只活在实时响应里。
      const withSummaryRationale = assistant.filter(
        (m) => m.visualRationale && m.visualRationale.code === "SUMMARY_TEXT_ONLY"
      );
      if (withSummaryRationale.length === 0) {
        throw new Error(
          `多步汇总依据没落库 ⇒ /messages 里没有 SUMMARY_TEXT_ONLY。样例=${JSON.stringify(assistant[0]).slice(0, 200)}`
        );
      }
      return `无操作恢复 ${pointer}：${msgs.messages.length} 条，其中 ${withSummaryRationale.length} 条带 SUMMARY_TEXT_ONLY 依据（顶层无图）`;
    }
    // 把 DOM 上的 canvas 钉到**落库字段**上：会话恢复走的是 /messages，响应里
    // assistant 行必须自带 chartType/chartOption —— 图是落库的，不是内存里残留的。
    const withChart = assistant.filter((m) => m.chartType);
    if (withChart.length === 0) {
      throw new Error(
        `画布出来了但 /messages 里 assistant 行没有 chartType ⇒ 图不是从落库字段渲染的。样例=${JSON.stringify(assistant[0]).slice(0, 200)}`
      );
    }
    return `无操作恢复会话 ${pointer}：${msgs.messages.length} 条消息，其中 ${withChart.length} 条带 chartType=${withChart[0].chartType}，canvas 已渲染`;
  });

  await step("8. 反向守卫：/messages?tail=true 的 assistant 行负载", async () => {
    await waitFor(() => messagesTailOk, "/messages?tail=true 响应", 30_000).catch(() => {
      throw new Error("没抓到 /messages?tail=true 响应（导出时前端会发）");
    });
    const msgs = messagesTailOk.messages ?? [];
    const assistant = msgs.filter((m) => m.role === "assistant");
    if (assistant.length === 0) throw new Error("响应里没有 assistant 行");
    if (isMultiStep) {
      const withSummaryRationale = assistant.filter(
        (m) => m.visualRationale && m.visualRationale.code === "SUMMARY_TEXT_ONLY"
      );
      if (withSummaryRationale.length === 0) {
        throw new Error(
          `多步 assistant 行没有 SUMMARY_TEXT_ONLY 依据 ⇒ 没落库。样例=${JSON.stringify(assistant[0]).slice(0, 200)}`
        );
      }
      return `${assistant.length} 条 assistant，其中 ${withSummaryRationale.length} 条带 SUMMARY_TEXT_ONLY（顶层无图）`;
    }
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
  console.log(
    `  形态：${isMultiStep ? `多步（${stepCount} 个数据步，顶层无图 + SUMMARY_TEXT_ONLY 依据）` : "单步（图 + 折叠数据表 + 依据三件套）"}`
  );
  console.log(`  产物：${OUT_DIR}/（export.pdf、SSE 事件序列、截图）`);
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

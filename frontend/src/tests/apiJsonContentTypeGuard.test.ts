// 反向守卫：`src/` 下所有**裸 fetch + JSON body** 的调用都必须声明 Content-Type。
//
// 为什么需要它（2026-11-16 生产事故复盘）：
//   裸 `fetch(url, { method: "POST", body: JSON.stringify(obj) })` 不设 Content-Type 时，
//   浏览器实际发送 `Content-Type: text/plain;charset=UTF-8`（Chromium 实测）；
//   FastAPI 只在 content-type 是 `application/json`（或 `application/*+json`）时才把
//   body 当 JSON 解析 ⇒ body 被跳过 ⇒ POST/PATCH 一律 422
//   （"Input should be a valid dictionary or object to extract fields from"）。
//
//   而本仓库的 api 层单测**全部 mock 了 fetch**，只断言调用参数 ⇒ 漏写 Content-Type 时
//   测试依然全绿，只有生产会炸（`adminWikiLinks.ts` 就这样炸过一次）。
//   ⇒ 这里做一次源码级扫描，把这类漏写挡在 CI 之前。
//
// 规则（刻意保守，只抓能静态判定的写法，避免误报）：
//   - headers 写成 `authHeaders()`（无参）⇒ 违规
//   - headers 写成 `authHeaders({ ... })` 但对象里没有 "Content-Type" ⇒ 违规
//   - headers 传的是别的变量（无法静态判定内容）⇒ 跳过，不误报
//   - FormData / 二进制上传 ⇒ 不在扫描范围（本规则只看 `body: JSON.stringify`）

import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { existsSync } from "node:fs";

// 本文件位于 src/tests/ ⇒ 上一级就是 src/。
// 用 import.meta.dirname 定位（不依赖 cwd）：CI 里 vitest 的 root 可能是仓库根。
// 扫全 src/（不只 api/）而不是只扫 api/：这个坑任何裸 fetch 调用都可能踩。
const HERE = (import.meta as unknown as { dirname?: string }).dirname ?? process.cwd();
const SRC_DIR = existsSync(join(HERE, "..", "api"))
  ? resolve(HERE, "..")
  : resolve(process.cwd(), "src");

export interface Violation {
  line: number;
  snippet: string;
}

/**
 * 扫描一段源码，找出「带 JSON body 却没声明 Content-Type」的调用点。
 * 纯函数：既用于扫真实文件，也用于用合成源码验证守卫本身有效。
 */
export function findViolationsInSource(src: string): Violation[] {
  const violations: Violation[] = [];
  let idx = -1;
  while ((idx = src.indexOf("body: JSON.stringify", idx + 1)) !== -1) {
    const line = src.slice(0, idx).split("\n").length;
    // 往前找离 body 最近的 headers 引用点
    const headerAnchor = src.lastIndexOf("headers", idx);
    if (headerAnchor === -1) continue;
    const between = src.slice(headerAnchor, idx);
    // headers 传的是别的变量 ⇒ 静态判不出内容，跳过（不误报）
    if (!between.includes("authHeaders(")) continue;
    // 取出 authHeaders(...) 的实参
    const open = src.indexOf("(", headerAnchor + between.indexOf("authHeaders("));
    const close = src.indexOf(")", open);
    const args = src.slice(open + 1, close).trim();
    const hasContentType = args.includes("Content-Type");
    if (args !== "" && hasContentType) continue;
    const lineStart = src.lastIndexOf("\n", idx - 1) + 1;
    const lineEnd = src.indexOf("\n", idx);
    violations.push({ line, snippet: src.slice(lineStart, lineEnd).trim() });
  }
  return violations;
}

function listSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      out.push(...listSourceFiles(p));
    } else if (
      (name.endsWith(".ts") || name.endsWith(".tsx")) &&
      !name.endsWith(".d.ts") &&
      !name.includes(".test.") &&
      !name.includes(".spec.") &&
      name !== "__tests__"
    ) {
      out.push(p);
    }
  }
  return out;
}

describe("反向守卫：裸 fetch 的 JSON body 必须声明 Content-Type", () => {
  it("src/ 下没有「JSON body + 缺 Content-Type」的调用", () => {
    const violations: string[] = [];
    for (const file of listSourceFiles(SRC_DIR)) {
      for (const v of findViolationsInSource(readFileSync(file, "utf8"))) {
        violations.push(`${file}:${v.line} → ${v.snippet}`);
      }
    }
    expect(
      violations,
      `以下调用带 JSON body 却没有 Content-Type（浏览器会发 text/plain ⇒ 后端 422）：\n${violations.join("\n")}`,
    ).toEqual([]);
  });

  it("守卫本身有效：无参 authHeaders + JSON body 被判违规", () => {
    const fake = [
      'const r = await fetch("/x", {',
      '  method: "POST",',
      "  headers: authHeaders(),",
      "  body: JSON.stringify({ a: 1 }),",
      "});",
    ].join("\n");
    const found = findViolationsInSource(fake);
    expect(found.length).toBe(1);
    expect(found[0].line).toBe(4);
  });

  it("守卫本身有效：声明了 Content-Type 的写法不误报", () => {
    const fake = [
      'const r = await fetch("/x", {',
      '  method: "POST",',
      '  headers: authHeaders({ "Content-Type": "application/json" }),',
      "  body: JSON.stringify({ a: 1 }),",
      "});",
    ].join("\n");
    expect(findViolationsInSource(fake)).toEqual([]);
  });

  it("守卫本身有效：headers 传变量时跳过（不误报）", () => {
    const fake = [
      'const headers = authHeaders({ "Content-Type": "application/json" });',
      'const r = await fetch("/x", {',
      '  method: "POST",',
      "  headers,",
      "  body: JSON.stringify({ a: 1 }),",
      "});",
    ].join("\n");
    expect(findViolationsInSource(fake)).toEqual([]);
  });
});

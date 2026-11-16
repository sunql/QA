// 反向守卫：**裸 fetch + JSON body** 的调用必须声明 `Content-Type: application/json`。
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
// 判定口径（实现与本文件头一致，不多不少）：
//   对每个 `fetch(...)` 调用，取它的 init 对象，按**顶层成员**解析：
//     - body 成员的值以 `JSON.stringify` 开头 → 该调用在范围内；
//     - headers 成员**不存在** ⇒ 违规（浏览器仍会发 text/plain）；
//     - headers 值是对象字面量 ⇒ 其中没有 content-type 键 ⇒ 违规；
//     - headers 值是函数调用（如 `authHeaders(...)`）⇒ 实参里没有 content-type 键 ⇒ 违规；
//     - headers 值是**变量** ⇒ 静态判不出内容 ⇒ 跳过，不误报。
//   content-type 键按 **HTTP 头名大小写不敏感** 匹配，且要求是「键」而非字符串值里的文本。
//
// 已知边界（刻意取舍，不是缺陷）：
//   - headers 传变量（`const h = authHeaders(...); ... headers,`）静态判不出 ⇒ 跳过。
//     这是仓库里 chat.ts 的主流写法，本守卫覆盖不到。
//   - init 对象整体传变量（`fetch(url, init)`）⇒ 跳过。
//   - 扫描范围仅 `frontend/src`：**不要扩到 e2e/**，那里是 Playwright 的 route.fulfill
//     （不是 fetch 请求，且已显式带 contentType: "application/json"），扩了立刻假阳性。

import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { existsSync } from "node:fs";

// 本文件位于 src/tests/ ⇒ 上一级就是 src/。
// 用 import.meta.dirname 定位（不依赖 cwd）：CI 里 vitest 的 root 可能是仓库根。
const HERE = (import.meta as unknown as { dirname?: string }).dirname ?? process.cwd();
const SRC_DIR = existsSync(join(HERE, "..", "api"))
  ? resolve(HERE, "..")
  : resolve(process.cwd(), "src");

export interface Violation {
  line: number;
}

/** 顶层成员：key + 值的区间（相对 objText 的下标）。 */
interface Member {
  key: string;
  valueStart: number;
  valueEnd: number;
}

/** 跳过字符串字面量（含转义），返回结束后的下标。 */
function skipString(src: string, start: number): number {
  const quote = src[start];
  let i = start + 1;
  while (i < src.length) {
    if (src[i] === "\\") {
      i += 2;
      continue;
    }
    if (src[i] === quote) return i + 1;
    i++;
  }
  return i;
}

/**
 * 跳过「非代码」片段：字符串字面量、行注释、块注释。
 * 注释里出现 `headers:` / `body:` / 逗号都会骗过朴素的扫描器（本仓库的 init 对象里
 * 就带着解释性注释），所以扫描器必须把注释整段跳过。
 */
function skipNonCode(src: string, start: number): number {
  const ch = src[start];
  if (ch === '"' || ch === "'" || ch === "`") return skipString(src, start);
  if (ch === "/" && src[start + 1] === "/") {
    const nl = src.indexOf("\n", start);
    return nl === -1 ? src.length : nl + 1;
  }
  if (ch === "/" && src[start + 1] === "*") {
    const end = src.indexOf("*/", start + 2);
    return end === -1 ? src.length : end + 2;
  }
  return start;
}

/**
 * 找到 fetch 调用的 init 对象起点（`{`）。
 * 必须**跳过字符串与模板字符串**：`fetch(`${BASE}/${id}`, {` 里 `${...}` 里的 `{`
 * 不是 init 对象；也必须只在 fetch 参数列表的**顶层**找 `{`（`fetch(getUrl({a:1}), {`
 * 的内层 `{` 不是 init 对象）。找不到 ⇒ -1（init 是变量，判不出）。
 */
function findInitObjectStart(src: string, from: number): number {
  let i = from;
  let depth = 0;
  while (i < src.length) {
    const ch = src[i];
    if (ch === '"' || ch === "'" || ch === "`" || ch === "/") {
      const next = skipNonCode(src, i);
      if (next !== i) {
        i = next;
        continue;
      }
    }
    if (ch === "(") {
      depth++;
      i++;
      continue;
    }
    if (ch === ")") {
      if (depth === 0) return -1; // fetch 的参数列表结束
      depth--;
      i++;
      continue;
    }
    if (ch === "{" && depth === 0) return i;
    i++;
  }
  return -1;
}

/** 括号配平：返回与 src[start] 匹配的闭合括号下标；配不上返回 -1。 */
function matchBracket(src: string, start: number): number {
  const open = src[start];
  const close = open === "{" ? "}" : open === "(" ? ")" : open === "[" ? "]" : "";
  if (!close) return -1;
  let depth = 0;
  let i = start;
  while (i < src.length) {
    const ch = src[i];
    if (ch === '"' || ch === "'" || ch === "`" || ch === "/") {
      const next = skipNonCode(src, i);
      if (next !== i) {
        i = next;
        continue;
      }
    }
    if (ch === open) {
      depth++;
    } else if (ch === close) {
      depth--;
      if (depth === 0) return i;
    }
    i++;
  }
  return -1;
}

/** 扫描 init 对象的**顶层**成员（深度 1），跳过嵌套结构与字符串。 */
function scanTopLevelMembers(objText: string): Member[] {
  const members: Member[] = [];
  const n = objText.length;
  let i = 1; // 跳过起始的 '{'
  while (i < n) {
    // 跳过空白、逗号与注释
    for (;;) {
      while (i < n && /[\s,]/.test(objText[i])) i++;
      if (i < n && objText[i] === "/") {
        const next = skipNonCode(objText, i);
        if (next !== i) {
          i = next;
          continue;
        }
      }
      break;
    }
    if (i >= n || objText[i] === "}") break;

    // 读 key：字符串字面量 或 标识符
    let key = "";
    const keyStart = i;
    if (objText[i] === '"' || objText[i] === "'" || objText[i] === "`") {
      const end = skipString(objText, i);
      key = objText.slice(i + 1, end - 1);
      i = end;
    } else {
      while (i < n && /[A-Za-z0-9_$]/.test(objText[i])) {
        key += objText[i];
        i++;
      }
    }
    const keyEnd = i;
    while (i < n && /\s/.test(objText[i])) i++;

    if (objText[i] !== ":") {
      if (key && (objText[i] === "," || objText[i] === "}")) {
        // ES6 简写属性：`headers,` ⇒ 值就是同名变量 ⇒ 静态判不出
        members.push({ key, valueStart: keyStart, valueEnd: keyEnd });
        i = keyEnd;
        continue;
      }
      // 不是 key:value 形态（展开运算符等）⇒ 跳到下一个顶层逗号
      i = skipToTopLevelComma(objText, i);
      continue;
    }
    i++; // 跳过 ':'
    while (i < n && /\s/.test(objText[i])) i++;
    const valueStart = i;

    // 扫描值的结束位置：深度 0 时的 ',' 或 '}'
    let depth = 0;
    let valueEnd = n;
    while (i < n) {
      const ch = objText[i];
      if (ch === '"' || ch === "'" || ch === "`" || ch === "/") {
        const next = skipNonCode(objText, i);
        if (next !== i) {
          i = next;
          continue;
        }
      }
      if (ch === "{" || ch === "(" || ch === "[") {
        depth++;
        i++;
        continue;
      }
      if (ch === "}" || ch === ")" || ch === "]") {
        if (depth === 0) {
          valueEnd = i;
          break;
        }
        depth--;
        i++;
        continue;
      }
      if (ch === "," && depth === 0) {
        valueEnd = i;
        break;
      }
      i++;
    }
    members.push({ key, valueStart, valueEnd });
    i = valueEnd;
  }
  return members;
}

function skipToTopLevelComma(src: string, start: number): number {
  let depth = 0;
  let i = start;
  while (i < src.length) {
    const ch = src[i];
    if (ch === '"' || ch === "'" || ch === "`" || ch === "/") {
      const next = skipNonCode(src, i);
      if (next !== i) {
        i = next;
        continue;
      }
    }
    if (ch === "{" || ch === "(" || ch === "[") {
      depth++;
      i++;
      continue;
    }
    if (ch === "}" || ch === ")" || ch === "]") {
      if (depth === 0) return i;
      depth--;
      i++;
      continue;
    }
    if (ch === "," && depth === 0) return i + 1;
    i++;
  }
  return i;
}

/**
 * content-type 是否作为**键**出现。
 * - 大小写不敏感（HTTP 头名本身大小写不敏感）
 * - 必须是键位（前面是 `{` / `,` / 空白，后面紧跟 `:`），避免把字符串值里的
 *   "Content-Type: application/json" 误当成键
 */
function hasContentTypeKey(text: string): boolean {
  return /(?:^|[{,\s])(["'`]?)content-type\1\s*:/i.test(text);
}

/**
 * 取 headers 成员的值文本。
 * 返回 `null` 表示**静态判不出**（变量 / 整体表达式）⇒ 调用方应跳过，不误报。
 */
function headersValueText(objText: string, member: Member): string | null {
  const raw = objText.slice(member.valueStart, member.valueEnd).trim();
  if (raw.startsWith("{")) return raw;
  if (/^[A-Za-z0-9_$.]+\s*\(/.test(raw)) {
    const open = objText.indexOf("(", member.valueStart);
    const close = matchBracket(objText, open);
    if (close === -1) return null;
    return objText.slice(open + 1, close);
  }
  if (/^[A-Za-z0-9_$]+$/.test(raw)) return null; // 变量 ⇒ 判不出
  return raw;
}

/**
 * 扫描一段源码，找出「带 JSON body 却没声明 Content-Type」的 fetch 调用。
 * 纯函数：既用于扫真实文件，也用于用合成源码验证守卫本身有效。
 */
export function findViolationsInSource(src: string): Violation[] {
  const violations: Violation[] = [];
  let idx = -1;
  while ((idx = src.indexOf("body: JSON.stringify", idx + 1)) !== -1) {
    const line = src.slice(0, idx).split("\n").length;

    // 1) 定位包含该 body 的 fetch 调用（向前找最近的 fetch(）
    const callStart = src.lastIndexOf("fetch(", idx);
    if (callStart === -1) continue; // 不是 fetch 调用（如 axios）

    // 2) 取该 fetch 的 init 对象
    const objStart = findInitObjectStart(src, callStart + "fetch(".length);
    if (objStart === -1) continue; // init 是变量 ⇒ 判不出
    const objEnd = matchBracket(src, objStart);
    if (objEnd === -1) continue;
    const objText = src.slice(objStart, objEnd + 1);

    // 3) 确认这个 body 确实是该 init 对象的顶层成员（避免误配到别的对象）
    const members = scanTopLevelMembers(objText);
    const markerPos = src.indexOf("JSON.stringify", idx) - objStart;
    const bodyMember = members.find(
      (m) =>
        m.key === "body" &&
        objText.slice(m.valueStart, m.valueEnd).trim().startsWith("JSON.stringify") &&
        m.valueStart <= markerPos &&
        markerPos <= m.valueEnd,
    );
    if (!bodyMember) continue;

    // 4) 判定 headers
    const headerMember = members.find((m) => m.key === "headers");
    if (!headerMember) {
      // 完全没有 headers ⇒ 浏览器仍发 text/plain ⇒ 违规
      violations.push({ line });
      continue;
    }
    const headerText = headersValueText(objText, headerMember);
    if (headerText === null) continue; // headers 传变量 ⇒ 判不出，跳过
    if (hasContentTypeKey(headerText)) continue;
    violations.push({ line });
  }
  return violations;
}

function listSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const name = entry.name;
    // 跳过符号链接：statSync/读取会跟随到工作区之外的文件
    if (entry.isSymbolicLink()) continue;
    const p = join(dir, name);
    if (entry.isDirectory()) {
      // 测试目录整体不扫（与 vitest coverage 的 src/tests/** 排除口径一致）
      if (name === "__tests__" || name === "tests") continue;
      out.push(...listSourceFiles(p));
    } else if (
      (name.endsWith(".ts") || name.endsWith(".tsx")) &&
      !name.endsWith(".d.ts") &&
      !name.includes(".test.") &&
      !name.includes(".spec.") &&
      !name.endsWith(".setup.ts")
    ) {
      out.push(p);
    }
  }
  return out;
}

describe("反向守卫：裸 fetch 的 JSON body 必须声明 Content-Type", () => {
  it("src/ 下没有「JSON body + 缺 Content-Type」的调用", () => {
    const files = listSourceFiles(SRC_DIR);
    // 防空扫描假绿：扫了 0 个文件时下面的断言同样会绿
    expect(files.length).toBeGreaterThan(0);

    const violations: string[] = [];
    for (const file of files) {
      for (const v of findViolationsInSource(readFileSync(file, "utf8"))) {
        violations.push(`${file}:${v.line}`);
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

  it("守卫本身有效：完全没有 headers 键 ⇒ 判违规（浏览器仍发 text/plain）", () => {
    const fake = [
      'const r = await fetch("/x", {',
      '  method: "POST",',
      "  body: JSON.stringify({ a: 1 }),",
      "});",
    ].join("\n");
    expect(findViolationsInSource(fake).length).toBe(1);
  });

  it("守卫本身有效：手写 header 对象缺 content-type ⇒ 判违规", () => {
    const fake = [
      'const r = await fetch("/x", {',
      '  method: "POST",',
      '  headers: { "X-Tenant-Id": tenantId },',
      "  body: JSON.stringify({ a: 1 }),",
      "});",
    ].join("\n");
    expect(findViolationsInSource(fake).length).toBe(1);
  });

  it("守卫本身有效：body 写在 headers 之前（属性顺序）仍判违规", () => {
    const fake = [
      'const r = await fetch("/x", {',
      "  body: JSON.stringify({ a: 1 }),",
      '  method: "POST",',
      "  headers: authHeaders(),",
      "});",
    ].join("\n");
    expect(findViolationsInSource(fake).length).toBe(1);
  });

  it("守卫本身有效：content-type 小写写法视为合规（HTTP 头名大小写不敏感）", () => {
    const fake = [
      'const r = await fetch("/x", {',
      '  method: "POST",',
      '  headers: authHeaders({ "content-type": "application/json" }),',
      "  body: JSON.stringify({ a: 1 }),",
      "});",
    ].join("\n");
    expect(findViolationsInSource(fake)).toEqual([]);
  });

  it("守卫本身有效：字符串值里的 Content-Type 文本不算键（不误判为合规）", () => {
    const fake = [
      'const r = await fetch("/x", {',
      '  method: "POST",',
      '  headers: authHeaders({ "X-Note": "Content-Type: application/json" }),',
      "  body: JSON.stringify({ a: 1 }),",
      "});",
    ].join("\n");
    expect(findViolationsInSource(fake).length).toBe(1);
  });

  it("守卫本身有效：非 JSON body（FormData）不在扫描范围", () => {
    const fake = [
      'const r = await fetch("/x", {',
      '  method: "POST",',
      "  body: formData,",
      "});",
    ].join("\n");
    expect(findViolationsInSource(fake)).toEqual([]);
  });
});

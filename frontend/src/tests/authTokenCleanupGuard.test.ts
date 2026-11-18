// 守卫：401 清理路径**不得**自己操作 localStorage 的裸 "token" 键。
//
// 背景（2026-11-18，fix/auth-401-persist-cleanup）：
//   生产日志出现 `JWT 已过期: Signature has expired` + 401（GET /api/v1/messages/unread-count）。
//   取证结论：访问令牌 TTL 24 小时且**没有 refresh 机制** ⇒ 令牌到期是**设计内**的
//   fail-closed 行为，不是故障（见 backend/app/dependencies.py 与 config.jwtTtlSeconds）。
//
//   但顺带查出一处真问题：`api/client.ts` 里有一行 removeItem，作用在 localStorage 的
//   键 "token" 上，而**全仓没有任何代码写过那个键** —— 唯一写 token 的是 zustand persist，
//   键名 `qa-system-auth`（见 `stores/authStore.ts` 的 persist name）。
//   ⇒ 那行删的是一个不存在的键 = 死代码；
//   更糟的是配套测试 `api/__tests__/client-interceptor.test.ts` 也用这个假键造数据、
//   并断言「token 已从 localStorage 移除」⇒ 这条断言**证明不了任何事**
//   （真正该保证的「persist 键里的过期 token 被清掉」从未被断言）。
//
// 本守卫把「不得再出现裸 "token" 键操作」钉死，并同时钉住正确的清理路径仍在：
//   清理的唯一入口 = `useAuthStore.setState({ token: null, ... })`，
//   由 zustand persist 统一落盘（partialize 覆盖 token 字段 ⇒ 持久层里的 token 变 null）。
//
// 为什么用 TS AST 而不是正则扫源码：
//   正则会把**注释里**的示例代码也算进去（改注释就误报），也会漏掉变量形式的键；
//   AST 只看真实语法节点 ⇒ 注释免疫、判定精确。

import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join, resolve } from "node:path";
import ts from "typescript";

/** 本文件位于 src/tests/ ⇒ 上一级就是 src/。用 import.meta.dirname 定位（不依赖 cwd）。 */
const HERE = (import.meta as unknown as { dirname?: string }).dirname ?? process.cwd();
const CLIENT_PATH = existsSync(join(HERE, "..", "api", "client.ts"))
  ? resolve(HERE, "..", "api", "client.ts")
  : resolve(process.cwd(), "src", "api", "client.ts");

const CLIENT_SOURCE = readFileSync(CLIENT_PATH, "utf-8");

const SOURCE_FILE = ts.createSourceFile(
  CLIENT_PATH,
  CLIENT_SOURCE,
  ts.ScriptTarget.Latest,
  /* setParentNodes */ true
);

/** 递归收集所有节点（AST 里没有内建 walker 的父节点遍历，这里手写一个）。 */
function collectNodes(node: ts.Node): ts.Node[] {
  const nodes: ts.Node[] = [node];
  node.forEachChild((child) => {
    nodes.push(...collectNodes(child));
  });
  return nodes;
}

const ALL_NODES = collectNodes(SOURCE_FILE);

/** `localStorage` 这个标识符（不含 window.localStorage 的成员名重复计数问题）。 */
function isLocalStorageIdentifier(node: ts.Node): boolean {
  return ts.isIdentifier(node) && node.text === "localStorage";
}

/** localStorage.<op>("字面量键") 的调用点 —— 一律视为违规（要用持久层就用 store 的键常量）。 */
function findLiteralKeyLocalStorageCalls(): string[] {
  const hits: string[] = [];
  for (const node of ALL_NODES) {
    if (!ts.isCallExpression(node)) continue;
    const callee = node.expression;
    if (!ts.isPropertyAccessExpression(callee)) continue;
    if (!isLocalStorageIdentifier(callee.expression)) continue;
    if (!["removeItem", "getItem", "setItem"].includes(callee.name.text)) continue;
    const arg = node.arguments[0];
    if (arg && ts.isStringLiteral(arg)) {
      hits.push(`${callee.name.text}("${arg.text}")`);
    }
  }
  return hits;
}

/** `localStorage["token"]` 这种下标访问。 */
function findTokenElementAccess(): string[] {
  const hits: string[] = [];
  for (const node of ALL_NODES) {
    if (!ts.isElementAccessExpression(node)) continue;
    if (!isLocalStorageIdentifier(node.expression)) continue;
    const arg = node.argumentExpression;
    if (arg && ts.isStringLiteral(arg) && arg.text === "token") {
      hits.push(`localStorage["${arg.text}"]`);
    }
  }
  return hits;
}

/** 真实语法节点里是否还有 `useAuthStore.setState(...)` 且把 token 置 null。 */
function hasAuthStoreTokenCleanup(): boolean {
  for (const node of ALL_NODES) {
    if (!ts.isCallExpression(node)) continue;
    const callee = node.expression;
    if (!ts.isPropertyAccessExpression(callee)) continue;
    if (callee.name.text !== "setState") continue;
    if (!ts.isIdentifier(callee.expression)) continue;
    if (callee.expression.text !== "useAuthStore") continue;
    const arg = node.arguments[0];
    if (!arg || !ts.isObjectLiteralExpression(arg)) continue;
    for (const prop of arg.properties) {
      if (!ts.isPropertyAssignment(prop)) continue;
      if (!ts.isIdentifier(prop.name) || prop.name.text !== "token") continue;
      if (prop.initializer.kind === ts.SyntaxKind.NullKeyword) return true;
    }
  }
  return false;
}

describe("守卫：401 清理路径不走裸 localStorage 键", () => {
  it("client.ts 存在且非空（防空扫描假绿）", () => {
    expect(existsSync(CLIENT_PATH)).toBe(true);
    expect(CLIENT_SOURCE.length).toBeGreaterThan(0);
  });

  it("client.ts 不得对 localStorage 使用字面量键", () => {
    // 正确做法：只经由 authStore（zustand persist 落盘），client.ts 不自己碰 localStorage。
    expect(findLiteralKeyLocalStorageCalls()).toEqual([]);
  });

  it("client.ts 不得出现裸 \"token\" 键（点号或下标访问）", () => {
    expect(findTokenElementAccess()).toEqual([]);
  });

  it("client.ts 仍然清理 authStore 的 token（正向守卫：别把死代码删成什么都不清）", () => {
    expect(hasAuthStoreTokenCleanup()).toBe(true);
  });
});

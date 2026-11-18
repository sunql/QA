// 守卫：401 清理路径**不得**自己操作 localStorage（任何形式）。
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
// 本守卫把「清理只能经由 authStore」钉死，并同时钉住正确的清理路径仍在：
//   清理的唯一入口 = `useAuthStore.setState({ token: null, ... })`，
//   由 zustand persist 统一落盘（partialize 覆盖 token 字段 ⇒ 持久层里的 token 变 null）。
//
// 为什么用 TS AST 而不是正则扫源码：
//   正则会把**注释里**的示例代码也算进去（改注释就误报），也会漏掉变量形式的键；
//   AST 只看真实语法节点 ⇒ 注释免疫、判定精确。
//
// 2026-11-18 评审补强（首版守卫有 4 处可绕过，全部已封）：
//   ① 只认裸 `localStorage` 标识符 ⇒ `window.localStorage.removeItem("token")` 逃逸；
//      而 `window.localStorage` 恰是本仓主流写法 ⇒ 守卫会给出虚假保证。
//   ② 只查下标访问 `localStorage["token"]` ⇒ 点号写法 `localStorage.token` 逃逸。
//   ③ 正向断言只验「文件里存在 setState({token:null})」⇒ 把它挪进 **fulfilled 拦截器**
//      （每次成功响应都登出）守卫照样全绿。
//   ④ 手写 AST walker 依赖 `forEachChild` 回调「必须返回 undefined」的隐含契约 ⇒
//      一次「顺手改成简洁箭头」就把 AST 截断成 4 个节点 ⇒ 整套断言退化成空集恒真。
//   现版本的封法：① 覆盖 window/globalThis 前缀；② 点号 + 下标都查；
//   ③ 正向断言**锚定在 401 判定分支之内**；④ 改用内建 `ts.forEachChild` 且回调
//      类型标注为 `(n: ts.Node) => void`（TypeScript 层面禁止返回数字）
//      + 采集器自检用例（防空扫描假绿）。

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

/**
 * 递归收集所有节点。
 *
 * 用内建 `ts.forEachChild`（不依赖手写 walker 对回调返回值的隐含契约），
 * 且回调类型显式标注为 `(n: ts.Node) => void` ⇒ TypeScript 会直接拒绝
 * 「把回调改成简洁箭头、返回 push 的数字」这种写法 —— 那会让 TS 的 visitNodes
 * 把返回值当成「停止遍历兄弟节点」的信号，把 AST 截断成几行，整套断言退化成空集恒真
 * （2026-11-18 评审实测：709 节点 → 4 节点，违规代码仍全绿）。
 */
function collectNodes(node: ts.Node): ts.Node[] {
  const nodes: ts.Node[] = [];
  const visit = (n: ts.Node): void => {
    nodes.push(n);
    ts.forEachChild(n, visit);
  };
  visit(node);
  return nodes;
}

const ALL_NODES = collectNodes(SOURCE_FILE);

/** 是不是「localStorage 这个存储对象」的引用（裸标识符 / window. / globalThis. / self. 前缀）。 */
function isLocalStorageTarget(node: ts.Node): boolean {
  if (ts.isIdentifier(node)) return node.text === "localStorage";
  if (ts.isPropertyAccessExpression(node) && node.name.text === "localStorage") {
    const owner = node.expression;
    return (
      ts.isIdentifier(owner) &&
      (owner.text === "window" || owner.text === "globalThis" || owner.text === "self")
    );
  }
  return false;
}

/** client.ts 里一切对 localStorage 的方法调用 —— 一律违规。 */
function findLocalStorageCalls(): string[] {
  const hits: string[] = [];
  for (const node of ALL_NODES) {
    if (!ts.isCallExpression(node)) continue;
    const callee = node.expression;
    if (!ts.isPropertyAccessExpression(callee)) continue;
    if (!isLocalStorageTarget(callee.expression)) continue;
    hits.push(callee.getText(SOURCE_FILE));
  }
  return hits;
}

/** `localStorage["token"]`（下标）与 `localStorage.token`（点号）两种键访问 —— 一律违规。 */
function findLocalStorageKeyAccess(): string[] {
  const hits: string[] = [];
  for (const node of ALL_NODES) {
    if (ts.isElementAccessExpression(node) && isLocalStorageTarget(node.expression)) {
      hits.push(node.getText(SOURCE_FILE));
      continue;
    }
    if (
      ts.isPropertyAccessExpression(node) &&
      node.name.text === "token" &&
      isLocalStorageTarget(node.expression)
    ) {
      hits.push(node.getText(SOURCE_FILE));
    }
  }
  return hits;
}

/** if 条件里是否有 `status === 401`（左右颠倒也算）。 */
function is401Guard(condition: ts.Expression): boolean {
  const isStatus = (e: ts.Expression): boolean => ts.isIdentifier(e) && e.text === "status";
  const is401 = (e: ts.Expression): boolean => ts.isNumericLiteral(e) && e.text === "401";
  return collectNodes(condition).some((node) => {
    if (!ts.isBinaryExpression(node)) return false;
    if (node.operatorToken.kind !== ts.SyntaxKind.EqualsEqualsEqualsToken) return false;
    return (
      (isStatus(node.left) && is401(node.right)) || (is401(node.left) && isStatus(node.right))
    );
  });
}

/** 这个调用是不是 `useAuthStore.setState({ token: null, ... })`。 */
function isAuthStoreTokenCleanupCall(node: ts.Node): boolean {
  if (!ts.isCallExpression(node)) return false;
  const callee = node.expression;
  if (!ts.isPropertyAccessExpression(callee)) return false;
  if (callee.name.text !== "setState") return false;
  if (!ts.isIdentifier(callee.expression) || callee.expression.text !== "useAuthStore") return false;
  const arg = node.arguments[0];
  if (!arg || !ts.isObjectLiteralExpression(arg)) return false;
  return arg.properties.some(
    (prop) =>
      ts.isPropertyAssignment(prop) &&
      ts.isIdentifier(prop.name) &&
      prop.name.text === "token" &&
      prop.initializer.kind === ts.SyntaxKind.NullKeyword
  );
}

/**
 * 正向守卫：把 token 置空的 setState 必须**锚在 401 判定分支之内**。
 *
 * 只验「文件里存在 setState({token:null})」是不够的 —— 把它挪到 fulfilled 拦截器里
 * （每次成功响应都登出）或挪出 401 分支，守卫都会照样全绿。
 */
function hasAuthStoreTokenCleanupIn401Branch(): boolean {
  for (const node of ALL_NODES) {
    if (!ts.isIfStatement(node)) continue;
    // TS AST 里 if 的条件挂在 `.expression` 上（没有 `.condition` 属性）。
    if (!is401Guard(node.expression)) continue;
    if (collectNodes(node.thenStatement).some(isAuthStoreTokenCleanupCall)) return true;
  }
  return false;
}

describe("守卫：401 清理路径不自己碰 localStorage", () => {
  it("client.ts 存在且非空（防空扫描假绿）", () => {
    expect(existsSync(CLIENT_PATH)).toBe(true);
    expect(CLIENT_SOURCE.length).toBeGreaterThan(0);
  });

  it("采集器能走到 401 判定那层（采集器自检：防空扫描假绿）", () => {
    // 采集器一旦瞎了（walker 截断 / AST 解析失败），下面的「必须为空」断言全是恒真。
    // 判据用「能采到 401 的 if」而不是固定节点数 —— 它要求的正是本守卫要保护的那层深度。
    expect(ALL_NODES.length).toBeGreaterThan(50);
    expect(ALL_NODES.some((n) => ts.isIfStatement(n) && is401Guard(n.expression))).toBe(true);
  });

  it("client.ts 不得调用 localStorage 的任何方法（含 window.localStorage 前缀）", () => {
    // 正确做法：只经由 authStore（zustand persist 落盘），client.ts 不自己碰 localStorage。
    // 兜底（persist 写盘失败时删 persist 键）也放在 authStore 里 —— 持久层的所有者在那儿。
    expect(findLocalStorageCalls()).toEqual([]);
  });

  it("client.ts 不得出现 localStorage 的键访问（下标或点号，含 window. 前缀）", () => {
    expect(findLocalStorageKeyAccess()).toEqual([]);
  });

  it("client.ts 在 401 分支里仍然清空 authStore 的 token（正向守卫：别删成什么都不清）", () => {
    expect(hasAuthStoreTokenCleanupIn401Branch()).toBe(true);
  });
});

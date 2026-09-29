import "@testing-library/jest-dom";
import { createElement } from "react";
import { vi } from "vitest";

// 强制加载 i18n.ts 触发 i18n.use(initReactI18next).init({resources: {zh-CN, en-US}, ...})
// —— 大量页面（AdminToolsPage/AdminMenusPage/...）直接 `import { useTranslation }
// from "react-i18next"`，不经 ../i18n wrapper。如果测试只 import 那些页面，
// i18n.ts 顶层 init 永远不跑 → t(key) 返回 key 自身。这里副作用加载确保
// resources 进 i18next instance + setI18n(instance) 被调用。
import "../i18n";

// react-i18next 默认 mock：useTranslation 直接走真实 i18next 实例（已被 ../i18n 初始化
// 注入 zh-CN/en-US 资源），所以 t(key) 会返回真实中文文案 —— 这样既保证 i18n.ts
// 顶层 i18n.use(initReactI18next).init(...) 不报「wrong module」，也保证现有测试断言
// 真实中文文案不挂。
// 注意：真实 initReactI18next 是对象 { type: '3rdParty', init(i){} }，i18n.ts 直接
// i18n.use(initReactI18next) 传它本身；不能用 () => ({type:'3rdParty'}) 函数替身，
// 否则函数没有 .type，触发 i18next「You are passing a wrong module!」。
//
// 但 init(i){} 不能空实现 —— 必须把 instance 注入到 react-i18next 的全局 i18n
// 引用里（setI18n），否则 useTranslation/useI18nextTranslation 拿不到资源返回 key。
// 这里偷懒通过 i18next 自身的 react 模块设引用；react-i18next 提供 setI18n 导出。
vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>();
  return {
    ...actual,
    initReactI18next: {
      type: "3rdParty",
      init(instance: import("i18next").i18n) {
        // 委托真实 init 副作用：把 instance 注入 react-i18next 内部状态，
        // useTranslation 才能读到资源。直接 import 真实 setI18n 避免依赖。
        const reactI18next = actual as unknown as {
          setI18n?: (i: import("i18next").i18n) => void;
        };
        reactI18next.setI18n?.(instance);
      },
    },
    Trans: ({ children }: { children?: React.ReactNode }) => children,
  };
});

// 不 mock ../i18n —— i18n.ts 顶层 i18n.use(...).init(...) 会通过被 mock 的
// initReactI18next（返回 {type:"3rdParty"}）安全通过 init，资源完整加载。
// 这样 useTranslation/useI18nextTranslation 走真实 i18next 实例返回翻译文本，
// 现有断言真实文案的测试（RoutingMetricsPage、useTablePagination 等）直接复用。

// Monaco Editor mock（Phase 8）：jsdom 中加载 monaco 会触发 worker 请求，
// 用占位 div 代替；与 echarts-for-react mock 同模式。
// setup.ts 是 .ts 文件不能直接使用 JSX，使用 React.createElement 构造元素。
vi.mock("@monaco-editor/react", () => ({
  default: (props: { value?: string }) =>
    createElement("div", { "data-testid": "monaco-mock" }, props.value ?? ""),
}));

// jsdom 环境补丁：matchMedia（Ant Design 组件依赖）
if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

// ResizeObserver 补丁（ECharts / Ant Design 部分组件依赖）
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
window.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;

// scrollIntoView 补丁（jsdom 未实现；MessageList 自动滚动依赖）
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// getComputedStyle 桩：只覆盖 jsdom 的"假实现"（抛 Not implemented warning），
// 让 rc-util 的 getScrollBarSize 拿到一个真实对象（返回 0 scrollbar），
// 从而 useScrollLocker 不打断 antd Modal 的 Portal 挂载。
// 仅当真实实现抛错时介入，避免影响其他用 getComputedStyle 测真样式的场景。
const _realGetComputedStyle = window.getComputedStyle;
window.getComputedStyle = function getComputedStyleShim(
  elt: Element,
  pseudoElt?: string | null,
): CSSStyleDeclaration {
  try {
    return _realGetComputedStyle.call(window, elt, pseudoElt);
  } catch {
    // jsdom 对伪元素（::before/::scrollbar 等）抛 "Not implemented" — Modal 渲染时
    // useScrollLocker → getScrollBarSize 会撞上。返回零滚动条占位对象。
    const stub: Record<string, string> = {};
    const proxy = new Proxy(stub as unknown as CSSStyleDeclaration, {
      get(_target, prop: string) {
        return prop in stub ? stub[prop] : "";
      },
    });
    return proxy;
  }
} as typeof window.getComputedStyle;

// localStorage 补丁（jsdom 环境不提供；themeStore 暗色模式持久化依赖，5.8）
// 内存版实现，语义与 Web Storage 一致；测试间通过 clear() 复位。
const storage: Record<string, string> = {};
const localStorageMock: Storage = {
  getItem: (key: string): string | null => (key in storage ? storage[key] : null),
  setItem: (key: string, value: string): void => {
    storage[key] = String(value);
  },
  removeItem: (key: string): void => {
    delete storage[key];
  },
  clear: (): void => {
    Object.keys(storage).forEach((key) => {
      delete storage[key];
    });
  },
  key: (index: number): string | null => Object.keys(storage)[index] ?? null,
  get length(): number {
    return Object.keys(storage).length;
  },
};
Object.defineProperty(window, "localStorage", { value: localStorageMock, writable: true });
Object.defineProperty(globalThis, "localStorage", { value: localStorageMock, writable: true });

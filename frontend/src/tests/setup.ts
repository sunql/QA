import "@testing-library/jest-dom";
import { createElement } from "react";
import { vi } from "vitest";

// react-i18next 默认 mock：useTranslation 返回 key 自身（测试只断言 key 出现即可）。
// initReactI18next 必须在 mock 中返回合法的「3rdParty 模块」对象 —— 否则 stores/api 里
// 间接 import ../i18n → ./i18n.ts → i18n.use(initReactI18next) 会抛「wrong module」。
// 任何返回 { type } 的对象都会被 i18next 接受；这里返回 type:"3rdParty" 占位即可。
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
  initReactI18next: () => ({ type: "3rdParty" }),
  Trans: ({ children }: { children?: React.ReactNode }) => children,
}));

// 同步 mock ../i18n 模块：完全替换为 stub，**不调用 importOriginal**，否则 i18n.ts 模块顶层
// 的 i18n.use(...).init(...) 会先于 mock 工厂执行，触发「wrong module」。
// 组件用 useTranslation hook 走 react-i18next（已 mock），api/stores 用 i18n.t
// 在测试里也只需返回 key 字符串即可。
vi.mock("../i18n", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
  zhCN: {},
  enUS: {},
  i18n: { t: (k: string) => k, use: () => undefined, init: () => undefined },
}));

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

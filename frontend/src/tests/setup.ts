import "@testing-library/jest-dom";
import { createElement } from "react";
import { vi } from "vitest";
// 初始化 react-i18next 全局实例，确保所有组件测试在 i18n 就绪后运行
import "../i18n";

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

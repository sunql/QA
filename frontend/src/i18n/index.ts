export { useTranslation } from "./useTranslation";
export { zhCN } from "./zh-CN";
export { enUS } from "./en-US";
export type { NestedKeyOf, Vars } from "./types";
// 导出全局 i18n 实例，供非 React 模块（api/stores）使用
export { default as i18n } from "./i18n";

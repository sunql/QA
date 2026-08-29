/** react-i18next 全局初始化。

Resources 直接来源于现有 zh-CN.ts（inline），暂不使用 http-backend。
待未来加入 en-US 时可改用 i18next-http-backend 异步加载 JSON 文件。
*/
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import { zhCN } from "./zh-CN";
import { enUS } from "./en-US";

i18n.use(initReactI18next).init({
  resources: {
    "zh-CN": { translation: zhCN },
    "en-US": { translation: enUS },
  },
  lng: "zh-CN",
  fallbackLng: "zh-CN",
  interpolation: {
    // 保持与原有自定义 hook 一致的占位符语法：{name}
    prefix: "{",
    suffix: "}",
    // React JSX 已做 XSS 防护，这里不需要二次转义
    escapeValue: false,
  },
  // 缺 key 时返回 key 本身（与原有 hook 一致）
  returnNull: false,
  returnEmptyString: false,
  missingKeyHandler: (_lngs, _ns, key) => {
    if (import.meta.env.DEV) {
      // eslint-disable-next-line no-console
      console.warn(`[i18n] missing key: ${key}`);
    }
  },
});

export default i18n;

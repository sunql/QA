/** 翻译 hook：封装 react-i18next，对外保持原有签名不变。

调用方无需改动：
- t(key, vars?) 返回 string
- locale 固定为 "zh-CN"
- {name} 占位符语法不变
- 缺 key 返回 key 本身
- 缺变量保留 {name} 字面量
*/
import { useCallback } from "react";
import { useTranslation as useI18nextTranslation } from "react-i18next";
import type { Vars } from "./types";

export function useTranslation(): {
  t: (key: string, vars?: Vars) => string;
  locale: "zh-CN";
} {
  const { t: i18nT, i18n } = useI18nextTranslation("translation", {
    useSuspense: false,
  });

  // useCallback 保证 t 引用稳定（i18nT 引用在语言切换前不变）。
  // 页面普遍把依赖 t 的回调放进 useEffect 依赖（如 AgentRegistryPage 的
  // refresh），不稳定的 t 会触发无限请求循环（ERR_INSUFFICIENT_RESOURCES）。
  const t = useCallback(
    (key: string, vars?: Vars): string => {
      // react-i18next 缺 key 时返回 undefined / key，回退到 key 与原有行为一致
      const result = i18nT(key, vars ?? {}) as string;
      return result === undefined ? key : result;
    },
    [i18nT],
  );

  return { t, locale: (i18n.language ?? "zh-CN") as "zh-CN" };
}

import { useEffect } from "react";
import type { ReactNode } from "react";
import { App as AntdApp, ConfigProvider, theme } from "antd";
import zhCN from "antd/locale/zh_CN";
import { useThemeStore } from "../../stores/themeStore";
import { DARK_TOKEN, LIGHT_TOKEN } from "../../theme/tokens";
import { applyCssVarsToRoot, removeCssVarsFromRoot } from "../../theme/cssVariables";

interface ThemedRootProps {
  children: ReactNode;
}

// 主题根容器（5.8 / Phase E）：按暗色模式状态切换 Ant Design algorithm，
// 并把对应 ThemeToken 序列化为 :root CSS 变量供 ECharts / inline style /
// 业务自定义组件消费（保持单色源 — tokens.ts 同时驱动 antd ConfigProvider
// 与 cssVariables 模块）。
// 与 main.tsx 中的 ConfigProvider 逻辑保持单点（DRY），便于测试直接渲染。
// AntdApp 包裹：让子组件可以通过 App.useApp() 获取 message/notification/modal
// 实例，避免静态函数无法消费动态主题的警告。
export default function ThemedRoot({ children }: ThemedRootProps) {
  const isDark = useThemeStore((s) => s.isDark);
  const token = isDark ? DARK_TOKEN : LIGHT_TOKEN;

  useEffect(() => {
    applyCssVarsToRoot(token);
    return () => {
      removeCssVarsFromRoot();
    };
  }, [token]);

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: { colorPrimary: token.colorPrimary, borderRadius: token.borderRadius },
        algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
      }}
    >
      <AntdApp>{children}</AntdApp>
    </ConfigProvider>
  );
}

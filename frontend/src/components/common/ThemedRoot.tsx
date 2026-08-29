import type { ReactNode } from "react";
import { ConfigProvider, theme } from "antd";
import zhCN from "antd/locale/zh_CN";
import { useThemeStore } from "../../stores/themeStore";

interface ThemedRootProps {
  children: ReactNode;
}

// 主题根容器（5.8）：按暗色模式状态切换 Ant Design algorithm，
// 使所有组件（Layout/Table/Input 等）随主题自动适配。
// 与 main.tsx 中的 ConfigProvider 逻辑保持单点（DRY），便于测试直接渲染。
export default function ThemedRoot({ children }: ThemedRootProps) {
  const isDark = useThemeStore((s) => s.isDark);
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: { colorPrimary: "#1677ff" },
        algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
      }}
    >
      {children}
    </ConfigProvider>
  );
}

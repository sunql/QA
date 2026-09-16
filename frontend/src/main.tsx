import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
// dayjs 插件必须在 antd 之前注册，否则 DatePicker 打开时
// DatePanel → getWeekStartDate → dayjs().weekday() 报
// `clone.weekday is not a function`（antd 5 + dayjs 默认只挂核心方法）。
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import localeData from "dayjs/plugin/localeData";
import weekday from "dayjs/plugin/weekday";
import App from "./App";
import ThemedRoot from "./components/common/ThemedRoot";
import "./index.css";
// 初始化 react-i18next（触发 i18n.ts 中的同步 init，绑定全局实例）
import "./i18n";

dayjs.locale("zh-cn");
dayjs.extend(weekday);
dayjs.extend(localeData);

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <ThemedRoot>
      <BrowserRouter
        // react-router 6.30 起 BrowserRouter 仅支持这两个 flag，其余 v7_* 行为
        // 已成默认并从 FutureConfig 类型移除——保留会触发 TS2353 阻断 npm run build
        future={{
          v7_startTransition: true,
          v7_relativeSplatPath: true,
        }}
      >
        <App />
      </BrowserRouter>
    </ThemedRoot>
  </React.StrictMode>
);

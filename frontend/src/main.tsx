import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import ThemedRoot from "./components/common/ThemedRoot";
import "./index.css";
// 初始化 react-i18next（触发 i18n.ts 中的同步 init，绑定全局实例）
import "./i18n";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <ThemedRoot>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ThemedRoot>
  </React.StrictMode>
);

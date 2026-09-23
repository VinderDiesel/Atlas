/**
 * SPA 入口（Phase 1：ThemeProvider 包裹 ConfigProvider + 双主题 + BrowserRouter）。
 *
 * P2 引入 react-router-dom（0018 决策 ① 依赖登记表，P2 批次登记）：路由表见
 * App.tsx（/ask、/sessions、/governance/:section）。AntD `App` 提供 message 的
 * 上下文实例（角色切换提示用）——避免静态函数脱离 ConfigProvider 的告警。
 *
 * Phase 1：ConfigProvider 下沉到 ThemeProvider 内部（含 locale + theme algorithm）；
 * main.tsx 只负责 ThemeProvider 包裹 + global.css 引入。
 */
import { App as AntdApp } from "antd";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import "antd/dist/reset.css";
import "./styles/global.css";

import App from "./App";
import ThemeProvider from "./theme/ThemeProvider";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("index.html 缺少 #root 挂载点");
}

createRoot(container).render(
  <StrictMode>
    <ThemeProvider>
      <AntdApp>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AntdApp>
    </ThemeProvider>
  </StrictMode>,
);

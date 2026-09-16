/**
 * SPA 入口（P2：ConfigProvider 中文 locale + AntD App 上下文 + BrowserRouter）。
 *
 * P2 引入 react-router-dom（0018 决策 ① 依赖登记表，P2 批次登记）：路由表见
 * App.tsx（/ask、/sessions、/governance/:section）。AntD `App` 提供 message 的
 * 上下文实例（角色切换提示用）——避免静态函数脱离 ConfigProvider 的告警。
 */
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import "antd/dist/reset.css";

import App from "./App";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("index.html 缺少 #root 挂载点");
}

createRoot(container).render(
  <StrictMode>
    <ConfigProvider locale={zhCN}>
      <AntdApp>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AntdApp>
    </ConfigProvider>
  </StrictMode>,
);

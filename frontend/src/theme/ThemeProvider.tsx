/**
 * 主题提供者——深色/浅色切换 + localStorage 持久化 + AntD ConfigProvider。
 *
 * 默认深色（与推广视觉一致）。在 <html> 上同步 data-theme 属性供 CSS 变量消费，
 * 同步 color-scheme 供浏览器原生 UI（滚动条、表单控件）适配。
 *
 * Phase 1 基础设施；ThemeProvider 必须包裹在 AntD App 之外（ConfigProvider 在此处）。
 */
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { createContext, useCallback, useContext, useEffect, useState } from "react";

import { darkTheme, lightTheme } from "./tokens";

export type ThemeMode = "dark" | "light";

interface ThemeContextValue {
  mode: ThemeMode;
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  mode: "dark",
  toggleTheme: () => {},
});

const STORAGE_KEY = "atlas-theme";

/** 消费主题上下文（mode + toggleTheme）。 */
export function useThemeMode(): ThemeContextValue {
  return useContext(ThemeContext);
}

interface Props {
  children: React.ReactNode;
}

export default function ThemeProvider({ children }: Props) {
  const [mode, setMode] = useState<ThemeMode>(() => {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === "light" || stored === "dark" ? stored : "dark";
  });

  // 同步 <html> 属性：data-theme 供 CSS 变量消费，color-scheme 供浏览器原生 UI
  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute("data-theme", mode);
    root.style.colorScheme = mode;
    // 动态更新 theme-color meta（移动端浏览器状态栏颜色）
    const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    if (meta) {
      meta.content = mode === "dark" ? "#0c1220" : "#f8fafc";
    }
  }, [mode]);

  const toggleTheme = useCallback(() => {
    setMode((prev) => {
      const next = prev === "dark" ? "light" : "dark";
      localStorage.setItem(STORAGE_KEY, next);
      return next;
    });
  }, []);

  const themeConfig = mode === "dark" ? darkTheme : lightTheme;

  return (
    <ThemeContext.Provider value={{ mode, toggleTheme }}>
      <ConfigProvider locale={zhCN} theme={themeConfig}>
        {children}
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}

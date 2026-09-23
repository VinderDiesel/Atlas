/**
 * 设计令牌（Design Tokens）——品牌色 + 深色/浅色 AntD 5.x ThemeConfig。
 *
 * 品牌色与推广视觉体系统一（#0c1220 / #6ee7f5 / #f5b56e）。
 * 字体：UI = Outfit（几何感、现代），Code = JetBrains Mono（等宽、连字）。
 *
 * Phase 1 基础设施；Phase 2 的 global.css 消费相同色值作为 CSS 自定义属性。
 */
import { theme } from "antd";
import type { ThemeConfig } from "antd";

// ── 品牌色 ──────────────────────────────────────────────

/** 深色主题品牌色（与推广视觉体系统一）。 */
export const BRAND = {
  bg: "#0c1220",
  surface: "#131d2e",
  border: "#1e2d42",
  primary: "#6ee7f5",
  accent: "#f5b56e",
  text: "#e2e8f0",
  muted: "#8b98a9",
  success: "#34d399",
  danger: "#f87171",
  codeBg: "#0a0f1a",
} as const;

/** 浅色主题品牌色（对比度调整以满足 WCAG AA）。 */
export const BRAND_LIGHT = {
  bg: "#f8fafc",
  surface: "#ffffff",
  border: "#e2e8f0",
  primary: "#0891b2",
  accent: "#d97706",
  text: "#1e293b",
  muted: "#64748b",
  success: "#059669",
  danger: "#dc2626",
  codeBg: "#f1f5f9",
} as const;

// ── 字体栈 ──────────────────────────────────────────────

export const FONT_UI =
  '"Outfit", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif';
export const FONT_CODE =
  '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace';

/** 内容最大宽度（global.css .atlas-page 消费）。 */
export const CONTENT_MAX = "1200px";

// ── 深色主题 ────────────────────────────────────────────

export const darkTheme: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorPrimary: BRAND.primary,
    colorBgContainer: BRAND.surface,
    colorBgLayout: BRAND.bg,
    colorBorder: BRAND.border,
    colorText: BRAND.text,
    colorTextSecondary: BRAND.muted,
    colorSuccess: BRAND.success,
    colorError: BRAND.danger,
    colorWarning: BRAND.accent,
    borderRadius: 6,
    fontFamily: FONT_UI,
    fontFamilyCode: FONT_CODE,
  },
  components: {
    Layout: {
      headerBg: BRAND.surface,
      bodyBg: BRAND.bg,
      headerColor: BRAND.text,
    },
    Menu: {
      darkItemBg: "transparent",
      darkItemColor: BRAND.text,
      darkItemSelectedColor: BRAND.primary,
      darkItemHoverColor: BRAND.primary,
      darkItemSelectedBg: "rgba(110, 231, 245, 0.08)",
    },
    Button: {
      primaryColor: BRAND.bg,
    },
  },
};

// ── 浅色主题 ────────────────────────────────────────────

export const lightTheme: ThemeConfig = {
  algorithm: theme.defaultAlgorithm,
  token: {
    colorPrimary: BRAND_LIGHT.primary,
    colorBgContainer: BRAND_LIGHT.surface,
    colorBgLayout: BRAND_LIGHT.bg,
    colorBorder: BRAND_LIGHT.border,
    colorText: BRAND_LIGHT.text,
    colorTextSecondary: BRAND_LIGHT.muted,
    colorSuccess: BRAND_LIGHT.success,
    colorError: BRAND_LIGHT.danger,
    colorWarning: BRAND_LIGHT.accent,
    borderRadius: 6,
    fontFamily: FONT_UI,
    fontFamilyCode: FONT_CODE,
  },
  components: {
    Layout: {
      headerBg: BRAND_LIGHT.surface,
      bodyBg: BRAND_LIGHT.bg,
      headerColor: BRAND_LIGHT.text,
    },
    Menu: {
      itemBg: "transparent",
      itemColor: BRAND_LIGHT.text,
      itemSelectedColor: BRAND_LIGHT.primary,
      itemHoverColor: BRAND_LIGHT.primary,
      itemSelectedBg: "rgba(8, 145, 178, 0.08)",
    },
    Button: {
      primaryColor: "#ffffff",
    },
  },
};

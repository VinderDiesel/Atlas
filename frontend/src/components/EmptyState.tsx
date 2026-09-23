/**
 * 统一空状态组件——未认证、无数据、无结果等场景共用。
 *
 * 替代原来散落在各面板的 Text type="secondary" / Alert 提示，
 * 提供一致的视觉语言：图标 + 标题 + 说明 + 可选操作按钮。
 */
import { Button, Space, Typography } from "antd";
import type { ReactNode } from "react";

const { Text, Title } = Typography;

type IconType = "lock" | "inbox" | "search" | "empty";

interface Props {
  /** 图标类型（决定展示哪个 AntD 图标）。 */
  icon?: IconType;
  /** 主标题（如"尚未登录"）。 */
  title: string;
  /** 说明文字（如"请先在右上角选择角色开始使用"）。 */
  description?: ReactNode;
  /** 可选操作按钮。 */
  action?: {
    label: string;
    onClick: () => void;
  };
}

/** 内联 SVG 图标（不引入额外依赖，主题色跟随 CSS 变量）。 */
function EmptyIcon({ type }: { type: IconType }) {
  const color = "var(--atlas-muted)";
  const size = 48;

  switch (type) {
    case "lock":
      return (
        <svg width={size} height={size} viewBox="0 0 48 48" fill="none">
          <rect x="14" y="22" width="20" height="16" rx="2" stroke={color} strokeWidth="2" />
          <path d="M18 22V16a6 6 0 0 1 12 0v6" stroke={color} strokeWidth="2" strokeLinecap="round" />
          <circle cx="24" cy="31" r="2" fill={color} />
        </svg>
      );
    case "inbox":
      return (
        <svg width={size} height={size} viewBox="0 0 48 48" fill="none">
          <path d="M6 14h36v22a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V14z" stroke={color} strokeWidth="2" />
          <path d="M6 14l6-6h24l6 6" stroke={color} strokeWidth="2" strokeLinejoin="round" />
          <path d="M18 22h12" stroke={color} strokeWidth="2" strokeLinecap="round" />
        </svg>
      );
    case "search":
      return (
        <svg width={size} height={size} viewBox="0 0 48 48" fill="none">
          <circle cx="20" cy="20" r="12" stroke={color} strokeWidth="2" />
          <path d="M29 29l11 11" stroke={color} strokeWidth="2" strokeLinecap="round" />
        </svg>
      );
    case "empty":
    default:
      return (
        <svg width={size} height={size} viewBox="0 0 48 48" fill="none">
          <rect x="8" y="10" width="32" height="28" rx="2" stroke={color} strokeWidth="2" />
          <path d="M8 18h32" stroke={color} strokeWidth="2" />
          <path d="M16 26h16" stroke={color} strokeWidth="2" strokeLinecap="round" />
          <path d="M16 32h10" stroke={color} strokeWidth="2" strokeLinecap="round" />
        </svg>
      );
  }
}

export default function EmptyState({
  icon = "empty",
  title,
  description,
  action,
}: Props) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: "48px 24px",
        textAlign: "center",
      }}
    >
      <EmptyIcon type={icon} />
      <Space direction="vertical" size={8} style={{ marginTop: 16 }}>
        <Title level={5} style={{ margin: 0, color: "var(--atlas-text)" }}>
          {title}
        </Title>
        {description !== undefined && (
          <Text type="secondary" style={{ maxWidth: 400, display: "block" }}>
            {description}
          </Text>
        )}
        {action !== undefined && (
          <Button type="primary" onClick={action.onClick} style={{ marginTop: 8 }}>
            {action.label}
          </Button>
        )}
      </Space>
    </div>
  );
}

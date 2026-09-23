/**
 * 命令块（可复制）——P2 角色切换器的「make token」引导与回退通道共用件。
 *
 * 用 AntD Typography.Paragraph 的 copyable（不直接依赖 navigator.clipboard——
 * 后者在非安全上下文不可用，且无需自写降级）；展示文本与复制文本恒同源，
 * 本组件不改写命令内容（命令由 state/role.ts 的 makeTokenCommand 生成）。
 */
import { Typography } from "antd";

interface Props {
  /** 完整命令原文（复制与展示同为该串，不改写）。 */
  command: string;
}

export default function CopyBlock({ command }: Props) {
  return (
    <Typography.Paragraph
      copyable={{ text: command, tooltips: ["复制命令", "已复制"] }}
      style={{ marginBottom: 0 }}
    >
      <code style={{ userSelect: "all", wordBreak: "break-all", touchAction: "manipulation" }}>{command}</code>
    </Typography.Paragraph>
  );
}

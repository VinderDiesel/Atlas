/**
 * 确定性徽标（Determinism Badge）——编译器路径（青色）vs LLM 路径（琥珀色）。
 *
 * 编译器是"已验证"路径，LLM 是"候选"路径。视觉区分帮助用户理解答案的可信度。
 */
import { Tag } from "antd";

interface Props {
  path: "compiler" | "llm";
}

export default function DeterminismBadge({ path }: Props) {
  if (path === "compiler") {
    return (
      <Tag className="atlas-badge atlas-badge--compiler" color="cyan">
        已编译
      </Tag>
    );
  }
  return (
    <Tag className="atlas-badge atlas-badge--llm" color="orange">
      已生成
    </Tag>
  );
}

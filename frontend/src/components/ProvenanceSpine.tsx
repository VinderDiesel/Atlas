/**
 * 溯源脊（Provenance Spine）——答案区左侧垂直链，显示处理步骤。
 *
 * 视觉：2px 宽青色垂直线 + 节点圆点（8px）。节点依次出现（staggered reveal）。
 * 这是 Atlas 区别于 ChatBI 的独特签名——把"信任/可追溯"变成视觉元素。
 */
import type { ReactNode } from "react";

interface Props {
  children: ReactNode;
}

export default function ProvenanceSpine({ children }: Props) {
  return (
    <div className="atlas-spine">
      <div className="atlas-spine__line" aria-hidden="true" />
      <div className="atlas-spine__content">{children}</div>
    </div>
  );
}

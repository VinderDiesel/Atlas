/**
 * 快照封印（Snapshot Seal）——答案区右上角的"印章"样式徽标。
 *
 * 视觉：圆角矩形，边框 2px，内部显示 `snapshot: abc1234`。
 * 中文语境里"盖章"= 权威背书。未绑定时显示灰色"未绑定"。
 */
import { Tag } from "antd";
import { shortSha } from "../lib/sha";

interface Props {
  snapshotSha: string | null;
  boundToHead: boolean;
}

export default function SnapshotSeal({ snapshotSha, boundToHead }: Props) {
  if (snapshotSha === null) {
    return (
      <Tag className="atlas-seal atlas-seal--unbound" color="default">
        <span className="atlas-seal__label">snapshot</span>
        <span className="atlas-seal__value atlas-mono">未绑定</span>
      </Tag>
    );
  }

  const color = boundToHead ? "cyan" : "blue";
  return (
    <Tag className="atlas-seal" color={color}>
      <span className="atlas-seal__label">snapshot</span>
      <span className="atlas-seal__value atlas-mono">{shortSha(snapshotSha)}</span>
    </Tag>
  );
}

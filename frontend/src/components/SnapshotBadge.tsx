/**
 * 顶栏快照徽标（0018 ⑦「绑定快照 sha 常驻可见」；设计页 §3.4 徽标 4 态表）。
 *
 * 判定与文案全部来自 lib/sha.ts 的纯函数（判别逻辑不在组件里——改文案先改 lib
 * 并同步其 vitest 断言）。fetch 失败时显示错误原文（零遥测口径：不吞错）。
 */
import { Tag, Tooltip } from "antd";

import type { HealthPayload } from "../api/types";
import { snapshotBadge } from "../lib/sha";

const SEVERITY_COLOR = { info: "blue", warning: "orange", error: "red" } as const;

interface Props {
  health: HealthPayload | null;
  /** /api/v1/health 请求失败时的错误原文；非 null 时优先于 health 展示。 */
  error: string | null;
}

export default function SnapshotBadge({ health, error }: Props) {
  if (error !== null) {
    return (
      <Tag color="red" title={error}>
        快照不可用：{error}
      </Tag>
    );
  }
  if (health === null) {
    return <Tag>快照状态加载中…</Tag>;
  }
  const badge = snapshotBadge(health);
  return (
    <Tooltip
      title={`head_sha=${health.head_sha ?? "null"} · snapshot_tables=${
        health.snapshot_tables ?? "null"
      } · boot_id=${health.boot_id}`}
    >
      <Tag color={SEVERITY_COLOR[badge.severity]}>{badge.text}</Tag>
    </Tooltip>
  );
}

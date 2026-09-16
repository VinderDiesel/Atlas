/**
 * state/session.ts 纯函数断言：会话 id 必须满足契约（AskBody.session_id ≤ 64）；
 * 会话记录（P2）：切角色必换 session_id（§3.5 约束 1 的机器证据）。
 */
import { describe, expect, it } from "vitest";

import { newSessionId, recordTurns, rotateSession, startSessionLog } from "../state/session";

describe("state/session.ts", () => {
  it("会话 id 为 UUID v4 形态且不超过契约上限 64 字符", () => {
    const id = newSessionId();
    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
    expect(id.length).toBeLessThanOrEqual(64);
  });

  it("两次生成不相同（无碰撞的会话键）", () => {
    expect(newSessionId()).not.toBe(newSessionId());
  });

  it("startSessionLog：未认证启动的 role 为 null（如实显示「未认证」，不猜测）", () => {
    const entry = startSessionLog(null, 1000);
    expect(entry.role).toBeNull();
    expect(entry.turnsSeen).toBe(0);
    expect(entry.startedAt).toBe(1000);
  });

  it("rotateSession：切角色必换 session_id，旧条目保留在本标签页累积里", () => {
    const first = startSessionLog(null, 1000);
    const { log, current } = rotateSession([first], "branch_manager", 2000);
    expect(current.id).not.toBe(first.id);
    expect(log).toHaveLength(2);
    expect(log.map((entry) => entry.role)).toEqual([null, "branch_manager"]);
    expect(log[0]).toBe(first); // 旧条目原样保留（不修改、不删除）
  });

  it("recordTurns：只更新命中会话且取 max（乱序响应不得把已见轮数改小）", () => {
    const first = startSessionLog(null, 1000);
    const second = { ...startSessionLog("hq_admin", 2000), turnsSeen: 3 };
    const unchanged = recordTurns([first, second], second.id, 2); // 2 < 3 → 不变
    expect(unchanged[1].turnsSeen).toBe(3);
    expect(unchanged[0].turnsSeen).toBe(0); // 非命中会话不动
    const advanced = recordTurns(unchanged, second.id, 5);
    expect(advanced[1].turnsSeen).toBe(5);
    expect(advanced[0]).toBe(first); // 未命中的条目保持引用（不无谓重建）
  });
});

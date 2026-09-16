/**
 * 会话 id 生成与「本标签页累积」的会话记录（内存态；设计页 §3.5 约束 1 的
 * 「切角色即换会话」语义载体）。
 *
 * P1 工作台挂载时生成一个会话 id 并复用（多轮续接，ADR-0020 决策 ④）；
 * P2 起由 App 持有 id 与记录：切角色即 rotate 新会话（旧条目留在
 * `SessionLogEntry[]` 里），SessionPanel 消费该数组。
 *
 * 不落任何持久化——会话状态在服务端 checkpoint，凭据与 id 都在客户端内存
 * （§3.5 约束 3 的刻意不对称）。跨重启/跨刷新的会话列表**无数据源**，
 * 该限制写死在 SessionPanel 的横幅与 README KL 条目里（不得删改）。
 *
 * 本文件不 import React；函数均为纯函数（可被 vitest 直接断言）。
 */

/** 新会话 id：UUID v4（36 字符，满足契约 session_id ≤ 64 的上限）。 */
export function newSessionId(): string {
  return crypto.randomUUID();
}

/** 会话记录：本标签页内创建过的每个会话一行（id / 激活时角色 / 时刻 / 已知轮数）。 */
export interface SessionLogEntry {
  id: string;
  /** 激活该会话时的角色名；未认证启动为 null（如实显示「未认证」，不猜测）。 */
  role: string | null;
  /** 创建时刻（epoch ms；格式化留给展示层，本模块不碰时区）。 */
  startedAt: number;
  /** 工作台响应回传的最大 turns_in_session（0 = 尚无轮次）。 */
  turnsSeen: number;
}

/** 开启一条新会话记录（App 初始化与每次切角色时调用）。 */
export function startSessionLog(role: string | null, now: number): SessionLogEntry {
  return { id: newSessionId(), role, startedAt: now, turnsSeen: 0 };
}

/**
 * 切角色/重激活：追加新会话并返回当前条目（约束 1——换身份必须换
 * `session_id`，且在界面上显式提示，调用方负责 message）。
 */
export function rotateSession(
  log: readonly SessionLogEntry[],
  role: string | null,
  now: number,
): { log: SessionLogEntry[]; current: SessionLogEntry } {
  const current = startSessionLog(role, now);
  return { log: [...log, current], current };
}

/**
 * 记录一次工作台响应：仅命中当前会话且轮数更大时更新（取 max——乱序响应
 * 与多标签并存时不得把已见轮数改小）。
 */
export function recordTurns(
  log: readonly SessionLogEntry[],
  sessionId: string,
  turns: number,
): SessionLogEntry[] {
  return log.map((entry) =>
    entry.id === sessionId && turns > entry.turnsSeen ? { ...entry, turnsSeen: turns } : entry,
  );
}

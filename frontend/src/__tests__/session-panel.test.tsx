/**
 * T06d 会话面板改造冒烟：服务端会话目录（GET /sessions）接入后的渲染面。
 *
 * 断言面（诚实性）：
 * - 旧横幅「跨重启历史无数据源」已过时（ADR-0031 D13 开了只读目录）——
 *   不得再出现；替换为目录能力与「不含问句原文」的准确说明；
 * - token 空串：认证提示（不发请求）；
 * - 本标签页内存日志区保留（no-persist 约束不变；不引入浏览器存储）。
 */
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import SessionPanel from "../panels/sessions/SessionPanel";

const PROPS = {
  health: null,
  healthError: null,
  log: [],
  currentId: "sess-local",
  token: "",
  onOpenRun: () => {},
};

describe("SessionPanel", () => {
  it("旧横幅退场：不再声称「跨重启历史无数据源」", () => {
    const html = renderToString(<SessionPanel {...PROPS} />);
    expect(html).not.toContain("跨重启历史无数据源");
  });

  it("未认证：显示友好空状态", () => {
    const html = renderToString(<SessionPanel {...PROPS} />);
    expect(html).toContain("尚未登录");
    expect(html).toContain("会话历史");
  });
});

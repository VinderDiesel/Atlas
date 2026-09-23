/**
 * 入口壳冒烟断言：renderToString(<App/>) 能出真实 HTML。
 *
 * 存在理由（P0b 工程边界）：tsc 与 vite build 都不会暴露「JSX 被转成 classic
 * 运行时而 React 不在作用域」这类**运行时**断裂——build 会成功，浏览器才炸。
 * 本测试让真实 JSX 走 vitest 的同一条 esbuild+tsconfig 转译链并执行，
 * 转译链一断 `make ui-check` 立即红。
 * P2 起 App 消费路由上下文（useLocation/useNavigate）——必须包 MemoryRouter
 * （与 main.tsx 的 BrowserRouter 同契约）；入口用 /ask 直达工作台路由。
 * 注意：这是冒烟不是渲染快照（0018 判据 9/10 禁止快照式断言）。
 */
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import App from "../App";

describe("App（入口壳冒烟）", () => {
  it("renderToString 输出包含标题文本", () => {
    expect(
      renderToString(
        <MemoryRouter initialEntries={["/ask"]}>
          <App />
        </MemoryRouter>,
      ),
    ).toContain("Atlas 控制台");
  });

  it("/runs 路由渲染运行历史（未认证态如实提示）", () => {
    const html = renderToString(
      <MemoryRouter initialEntries={["/runs"]}>
        <App />
      </MemoryRouter>,
    );
    expect(html).toContain("运行历史");
    expect(html).toContain("尚未登录");
  });

  it("/setup 路由渲染数据源+语义模型两面板（未认证态如实提示）", () => {
    const html = renderToString(
      <MemoryRouter initialEntries={["/setup"]}>
        <App />
      </MemoryRouter>,
    );
    expect(html).toContain("数据源");
    expect(html).toContain("连接数据源");
    expect(html).toContain("语义模型");
    expect(html).toContain("尚未登录");
  });
});

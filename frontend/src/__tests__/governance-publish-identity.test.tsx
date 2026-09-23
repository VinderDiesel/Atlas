/**
 * T08c 治理页发布身份只读断言（`panels/governance/PublishIdentitySection.tsx`；
 * ADR-0031 D04「governance 显示发布身份」）。
 *
 * 断言面：
 * - 发布身份 = 部署当前绑定的活动发布制品身份（release_id 是**内容身份**，不是
 *   可移动的 latest——D04）；active_release_id=null 如实显示「未绑定发布」；
 * - 渲染只取自固定键（数据夹带的 host/password 等未知键不得出现在输出）；
 * - 加载是显式用户动作（治理页挂载 8 条是 ADR-0022 决策 ⑥ 限流推导的前提，
 *   本区块不改变该口径）；SSR 初始态不触发任何请求；
 * - 布局接入：已认证分支（token 非空）渲染发布身份区块（删掉接入行本测试即红）。
 */
import { renderToString } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { DeploymentRecord } from "../api/types";
import GovernanceLayout from "../panels/governance/GovernanceLayout";
import PublishIdentitySection, {
  PublishIdentityRows,
} from "../panels/governance/PublishIdentitySection";

const ACTIVE: DeploymentRecord = {
  deployment_id: "finance-live",
  scope: "finance",
  source_id: "doris-primary",
  active_release_id: "d".repeat(64),
  revision: 4,
  created_by: { issuer: "atlas-local", subject: "operator" },
  created_at: "2026-09-22T12:10:00+08:00",
  updated_at: "2026-09-22T13:25:00+08:00",
};

const DRAFT_BOUND: DeploymentRecord = {
  ...ACTIVE,
  deployment_id: "finance-wizard",
  active_release_id: null,
  revision: 1,
};

describe("PublishIdentityRows（只读渲染；release_id 是内容身份）", () => {
  it("活动发布：显示 release_id 全文与指针版本；draft：如实显示未绑定", () => {
    const html = renderToString(<PublishIdentityRows items={[ACTIVE, DRAFT_BOUND]} />);
    expect(html).toContain("finance-live");
    expect(html).toContain("d".repeat(64));
    expect(html).toContain("finance-wizard");
    expect(html).toContain("未绑定发布");
  });

  it("数据夹带 host/password 时输出仍不含（UI 不渲染未知键）", () => {
    const tainted = {
      ...ACTIVE,
      host: "10.9.8.7",
      password: "sup3r-secret-pw",
    } as unknown as DeploymentRecord;
    const html = renderToString(<PublishIdentityRows items={[tainted]} />);
    expect(html).not.toContain("10.9.8.7");
    expect(html).not.toContain("sup3r-secret-pw");
  });
});

describe("PublishIdentitySection（SSR 初始态）", () => {
  it("渲染标题与显式加载说明；未点击不展示部署数据", () => {
    const html = renderToString(<PublishIdentitySection token="T" />);
    expect(html).toContain("发布身份");
    expect(html).toContain("加载");
    expect(html).not.toContain("d".repeat(64));
  });
});

describe("GovernanceLayout 接入（已认证分支）", () => {
  it("渲染发布身份区块；挂载 8 条在 useEffect 内，SSR 不发请求", () => {
    const html = renderToString(
      <MemoryRouter initialEntries={["/governance/models"]}>
        <Routes>
          <Route
            path="/governance/:section"
            element={<GovernanceLayout token="T" domain="finance" onDomainChange={() => {}} />}
          />
        </Routes>
      </MemoryRouter>,
    );
    expect(html).toContain("发布身份");
    expect(html).toContain("加载发布身份");
  });
});

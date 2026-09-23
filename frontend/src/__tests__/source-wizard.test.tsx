/**
 * T07d 数据源接入向导断言（`panels/setup/SourceWizard.tsx`；ADR-0031 D03/D13）。
 *
 * 断言面（诚实性优先）：
 * - 表单校验只接受环境变量引用（env:<NAME>）——明文凭据/DSN 在提交前被拒
 *   （向导不是把秘密送进请求然后再让后端 422 的通道）；
 * - toSourceBody：多行文本 → 三段表名数组（trim/去空行，不猜不美化）；
 * - SourceRowsSection / ProbeResultSection：**只渲染已知键**——数据里夹带的
 *   隐藏字段（host/password）不得出现在输出（UI 不泄漏连接串，工作卡退出门）；
 * - 探测阻塞展示中文原因（9 类阻塞理由全覆盖），成功展示能力声明 +
 *   reproducible=False 的如实标注（历史结论不当作本次事实）；
 * - 部署区：active_release_id=null → 「draft（未绑定发布）」。
 */
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type {
  DeploymentRecord,
  ProbeResult,
  SourceRevisionRecord,
} from "../api/types";
import SourceWizard, {
  DeploymentRowsSection,
  ProbeResultSection,
  SourceRowsSection,
  blockedReasonLabel,
  parseLines,
  toSourceBody,
  validateSourceForm,
  type SourceFormValues,
} from "../panels/setup/SourceWizard";

const SOURCE: SourceRevisionRecord = {
  source_id: "doris-primary",
  version: 2,
  revision: "rev-2026-09-22-2",
  connector_kind: "doris",
  secret_ref: "env:ATLAS_TEST_DORIS",
  allowed_catalogs: ["atlas"],
  allowed_tables: ["atlas.dwd.fact_trades", "atlas.dwd.dim_account"],
  timezone: "+08:00",
  tls_policy: "disabled",
  query_budget: 10000,
  created_by: { issuer: "atlas-local", subject: "operator" },
  created_at: "2026-09-22T12:00:00+08:00",
  last_probe: {
    probe_id: "probe-1",
    status: "blocked",
    blocked_reason: "target_not_allowlisted",
    observed_at: "2026-09-22T12:05:00+08:00",
  },
};

const PROBE_OK: ProbeResult = {
  probe_id: "probe-2",
  source_id: "doris-primary",
  version: 2,
  status: "ok",
  blocked_reason: null,
  observed_at: "2026-09-22T12:06:00+08:00",
  engine_version: "4.1.0-test",
  schema_digest: "a".repeat(64),
  capabilities: {
    dialect: "doris",
    read_only: true,
    metadata_probe: true,
    cancel_query: true,
    snapshot_read: true,
    consistent_analysis: false,
  },
  reproducible: false,
};

const DRAFT: DeploymentRecord = {
  deployment_id: "finance-live",
  scope: "finance",
  source_id: "doris-primary",
  active_release_id: null,
  revision: 1,
  created_by: { issuer: "atlas-local", subject: "operator" },
  created_at: "2026-09-22T12:10:00+08:00",
  updated_at: "2026-09-22T12:10:00+08:00",
};

const FORM: SourceFormValues = {
  sourceId: "doris-primary",
  revision: "rev-2026-09-22-3",
  secretRef: "env:ATLAS_TEST_DORIS",
  allowedCatalogsText: "atlas",
  allowedTablesText: "atlas.dwd.fact_trades\natlas.dwd.dim_account",
  timezone: "+08:00",
  tlsPolicy: "required",
  queryBudget: 10000,
};

describe("parseLines / validateSourceForm（向导只收环境引用）", () => {
  it("parseLines：trim、去空行、去重复之外的原文（不静默改写）", () => {
    expect(parseLines("  a \n\n b\t\n")).toEqual(["a", "b"]);
    expect(parseLines("")).toEqual([]);
  });

  it("合法表单：零错误", () => {
    expect(validateSourceForm(FORM)).toEqual([]);
  });

  it("secretRef 非 env: 引用 → 拒（明文密码/DSN 不得上路）", () => {
    expect(
      validateSourceForm({ ...FORM, secretRef: "password=sup3r-secret-pw" }).length,
    ).toBeGreaterThan(0);
    expect(validateSourceForm({ ...FORM, secretRef: "env:1BAD" }).length).toBeGreaterThan(0);
  });

  it("表名必须三段且首段 ∈ 目录；越界/非法被拒", () => {
    expect(
      validateSourceForm({ ...FORM, allowedTablesText: "fact_trades" }).length,
    ).toBeGreaterThan(0);
    expect(
      validateSourceForm({ ...FORM, allowedTablesText: "other.dwd.fact_trades" }).length,
    ).toBeGreaterThan(0);
    expect(validateSourceForm({ ...FORM, allowedCatalogsText: "" }).length).toBeGreaterThan(0);
  });

  it("timezone 必须显式偏移；sourceId 必须对象名形态", () => {
    expect(validateSourceForm({ ...FORM, timezone: "UTC" }).length).toBeGreaterThan(0);
    expect(validateSourceForm({ ...FORM, sourceId: "含 空格" }).length).toBeGreaterThan(0);
  });

  it("toSourceBody：多行文本 → 数组；字段名为后端 snake_case", () => {
    expect(toSourceBody(FORM)).toEqual({
      source_id: "doris-primary",
      revision: "rev-2026-09-22-3",
      connector_kind: "doris",
      secret_ref: "env:ATLAS_TEST_DORIS",
      allowed_catalogs: ["atlas"],
      allowed_tables: ["atlas.dwd.fact_trades", "atlas.dwd.dim_account"],
      timezone: "+08:00",
      tls_policy: "required",
      query_budget: 10000,
    });
  });
});

describe("blockedReasonLabel（9 类阻塞理由全覆盖）", () => {
  it("每个理由都有中文标签；null → 空串", () => {
    const reasons = [
      "credential_missing",
      "target_forbidden",
      "target_not_allowlisted",
      "tls_error",
      "table_out_of_whitelist",
      "read_only_unconfirmed",
      "metadata_missing",
      "credential_rejected",
      "connect_failed",
    ] as const;
    for (const reason of reasons) {
      expect(blockedReasonLabel(reason)).not.toBe("");
    }
    expect(blockedReasonLabel(null)).toBe("");
  });
});

describe("SourceRowsSection（只渲染已知键；不泄漏隐藏字段）", () => {
  it("渲染源行与探测摘要；secret_ref 引用名可回显", () => {
    const html = renderToString(
      <SourceRowsSection items={[SOURCE]} onProbe={() => {}} probing={null} />,
    );
    expect(html).toContain("doris-primary");
    expect(html).toContain("env:ATLAS_TEST_DORIS");
    expect(html).toContain("不在允许列表");
  });

  it("数据夹带 host/password 时输出仍不含（UI 不渲染未知键）", () => {
    const tainted = {
      ...SOURCE,
      host: "10.9.8.7",
      password: "sup3r-secret-pw",
    } as unknown as SourceRevisionRecord;
    const html = renderToString(
      <SourceRowsSection items={[tainted]} onProbe={() => {}} probing={null} />,
    );
    expect(html).not.toContain("10.9.8.7");
    expect(html).not.toContain("sup3r-secret-pw");
  });
});

describe("ProbeResultSection（能力/错误展示）", () => {
  it("ok：能力声明逐项 + engine_version + reproducible=False 如实标注", () => {
    const html = renderToString(<ProbeResultSection result={PROBE_OK} />);
    expect(html).toContain("4.1.0-test");
    expect(html).toContain("只读");
    expect(html).toContain("doris");
    // 未证实的能力必须如实显示为否（consistent_analysis=false）
    expect(html).toContain("未证实");
    expect(html).toContain("False");
    expect(html).not.toContain("True");
  });

  it("blocked：中文原因 + 不建连说明；不渲染连接串", () => {
    const blocked: ProbeResult = {
      ...PROBE_OK,
      status: "blocked",
      blocked_reason: "tls_error",
      engine_version: null,
      schema_digest: null,
    };
    const tainted = { ...blocked, host: "10.9.8.7", dsn: "mysql://u:p@h:1/x" } as unknown as ProbeResult;
    const html = renderToString(<ProbeResultSection result={tainted} />);
    expect(html).toContain("TLS");
    expect(html).not.toContain("10.9.8.7");
    expect(html).not.toContain("mysql://");
  });
});

describe("DeploymentRowsSection（draft 语义）", () => {
  it("active_release_id=null → draft（未绑定发布）；有值 → 显示 release_id", () => {
    const draftHtml = renderToString(<DeploymentRowsSection items={[DRAFT]} />);
    expect(draftHtml).toContain("finance-live");
    expect(draftHtml).toContain("draft（未绑定发布）");

    const activeHtml = renderToString(
      <DeploymentRowsSection items={[{ ...DRAFT, active_release_id: "b".repeat(64) }]} />,
    );
    expect(activeHtml).toContain("b".repeat(64));
  });
});

describe("SourceWizard（SSR 初始态）", () => {
  it("未认证：认证提示 + 不渲染管理器（token 空串不发请求）", () => {
    const html = renderToString(<SourceWizard token="" />);
    expect(html).toContain("数据源");
    expect(html).toContain("尚未登录");
  });
});

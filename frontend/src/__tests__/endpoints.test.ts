/**
 * endpoints.ts 自洽断言（vitest）——0022 判据 9 的 TS 侧辅助防线。
 *
 * 跨语言主断言在 Python 侧（tests/test_api_contract_v2.py）：读
 * frontend/src/api/endpoints.ts 正则提取路径字面量，与后端
 * `app.openapi()["paths"]` **双向相等**（43 条，0026 起 17、④a 增 /analyze/stream、
 * ADR-0031 D13 增 auth 4、T05c 增 /runs 提交与视图 2、T05d 增 /feedback、
 * T06a 增 sessions 2 与 artifact 1、T06b 增 /runs/{run_id}/events、
 * T07a 增 /manage/sources、T07b 增 /manage/sources/{source_id}/probes、
 * T07c 增 /manage/deployments 列/创建与详情 2、T08a 增 /manage/drafts 列/创建与详情 2、
 * T08a-s2 增 drafts 校验/审核/补丁导出 3、T08b 增 releases imports 1 与
 * 部署发布/回退 2 与发布列/详情 2）。
 * 本文件只做仓内自洽检查，
 * 让 `npm run test`（ui-check 第二步）在不跑 Python 的情况下先一步变红：
 * - 条数 43 与 Python 侧 EXPECTED_PATHS 同口径（变更须两处同步，留红是有意的；
 *   T05 收口时本文件条数断言未随 endpoints.ts 同步，T06 开头修正并补跑 vitest）；
 * - 去重、前缀纪律（除根探针外全部以 /api/v1/ 起始）、钻取占位符形态。
 */
import { describe, expect, it } from "vitest";

import { API, PROBE_HEALTH } from "../api/endpoints";

const PREFIXED: readonly string[] = Object.values(API);
const ALL: readonly string[] = [...PREFIXED, PROBE_HEALTH];

describe("endpoints.ts（0022 判据 9 的 TS 侧）", () => {
  it("共 44 条：43 条 /api/v1 前缀路径 + 1 条根探针（T13 增 diagnostics）", () => {
    expect(PREFIXED).toHaveLength(43);
    expect(ALL).toHaveLength(44);
  });

  it("无重复路径", () => {
    expect(new Set(ALL).size).toBe(ALL.length);
  });

  it("除根探针外全部以 /api/v1/ 起始", () => {
    for (const path of PREFIXED) {
      expect(path.startsWith("/api/v1/")).toBe(true);
    }
  });

  it("根探针为精确值 /health（前端不调用的集合相等占位）", () => {
    expect(PROBE_HEALTH).toBe("/health");
  });

  it("2 条钻取路径保留 {item} / {name} 占位符形态", () => {
    expect(API.governanceValueDetail.endsWith("/{item}")).toBe(true);
    expect(API.governanceReportDetail.endsWith("/{name}")).toBe(true);
  });
});

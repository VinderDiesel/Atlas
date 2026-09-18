/**
 * 治理面板只读不变量的静态断言（ADR-0028 决策 ③；B3 收口判据的机器证据）。
 *
 * 扫描 `panels/governance/` 下全部运行时代码（12 个 .tsx 文件），断言**零写控件**：
 * - 无 HTTP 写方法（PUT/POST/DELETE/PATCH）调用；
 * - 无写 API 客户端函数（putJson/postJson/deleteJson/patchJson）；
 * - 无 antd 写控件（Form/Input/InputNumber/Switch）；
 * - 无 `editable` 属性（Table 列编辑、Descriptions 内联编辑等）。
 *
 * 模式仿 `no-persist.test.ts`：`import.meta.glob` 读源码原文 + 正则匹配调用/JSX
 * 形态（而非裸词），注释中的「只读」「不写」等说明不误报。
 *
 * 局限如实注明：不覆盖运行期间接写手段（如通过 props 透传外部写回调），
 * 该面由「治理端点一律 Bearer + 服务端只读」的纵深防御覆盖（ADR-0022）。
 */
import { describe, expect, it } from "vitest";

const GOV_SOURCES = import.meta.glob("../panels/governance/**/*.{ts,tsx}", {
  eager: true,
  query: "?raw",
  import: "default",
}) as Record<string, string>;

const FORBIDDEN: RegExp[] = [
  // HTTP 写方法（fetch 形态）
  /method\s*:\s*["']PUT["']/,
  /method\s*:\s*["']POST["']/,
  /method\s*:\s*["']DELETE["']/,
  /method\s*:\s*["']PATCH["']/,
  // 写 API 客户端调用（与 getJson 对应的写变体）
  /\b(putJson|postJson|deleteJson|patchJson)\s*\(/,
  // antd 写控件 JSX 形态
  /<Form\b/,
  /<Input\b/,
  /<InputNumber\b/,
  /<Switch\b/,
  // editable 属性（Table 列编辑 / Descriptions 内联编辑）
  /\beditable\s*[=:]\s*[{t]/,
];

describe("治理面板只读不变量（ADR-0028 决策 ③ 的静态断言）", () => {
  it("治理面板运行时代码无 PUT/POST/写控件/editable", () => {
    const files = Object.entries(GOV_SOURCES);
    expect(files.length).toBeGreaterThanOrEqual(10); // 扫描面非空防线（当前 12 文件）
    const violations: string[] = [];
    for (const [path, text] of files) {
      for (const pattern of FORBIDDEN) {
        if (pattern.test(text)) {
          violations.push(`${path} 命中 ${pattern.source}`);
        }
      }
    }
    expect(violations).toEqual([]);
  });
});

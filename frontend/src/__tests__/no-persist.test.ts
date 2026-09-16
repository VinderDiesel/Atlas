/**
 * 「token 只存内存」的静态断言（§3.5 约束 3；P2 收口判据的机器证据）。
 *
 * 扫描 src/ 下**运行时代码**（不含 __tests__/）对浏览器存储的写入形态：一旦
 * 出现 localStorage / sessionStorage / cookie / IndexedDB 的调用或赋值即红。
 * 只匹配调用/赋值形态（而非裸词）——注释中「不落 localStorage」等说明不误报。
 *
 * 用 `import.meta.glob`（vite/vitest 原生）读源码原文，避免为测试引入
 * @types/node 依赖。局限如实注明：不覆盖运行期间接手段（第三方包自写存储），
 * 该面由「零遥测 + 直接依赖仅 5 个」的代码审阅覆盖（0018 落地注记 P1 批次）。
 */
import { describe, expect, it } from "vitest";

const RAW_SOURCES = import.meta.glob("../**/*.{ts,tsx}", {
  eager: true,
  query: "?raw",
  import: "default",
}) as Record<string, string>;

const FORBIDDEN: RegExp[] = [
  /localStorage\.(getItem|setItem|removeItem|clear|key)\s*\(/,
  /sessionStorage\.(getItem|setItem|removeItem|clear|key)\s*\(/,
  /\blocalStorage\s*\[/,
  /\bsessionStorage\s*\[/,
  /document\.cookie\s*=/,
  /\bindexedDB\b/,
];

describe("token 只存内存（§3.5 约束 3 的静态断言）", () => {
  it("运行时代码无 localStorage/sessionStorage/cookie/IndexedDB 的调用或赋值", () => {
    const runtime = Object.entries(RAW_SOURCES).filter(([path]) => !path.includes("__tests__"));
    expect(runtime.length).toBeGreaterThanOrEqual(10); // 扫描面非空防线
    const violations: string[] = [];
    for (const [path, text] of runtime) {
      for (const pattern of FORBIDDEN) {
        if (pattern.test(text)) {
          violations.push(`${path} 命中 ${pattern.source}`);
        }
      }
    }
    expect(violations).toEqual([]);
  });
});

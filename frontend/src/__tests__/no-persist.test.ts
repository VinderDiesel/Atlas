/**
 * 「token 只存内存」的静态断言（§3.5 约束 3；P2 收口判据的机器证据）。
 *
 * 扫描 src/ 下**运行时代码**（不含 `__tests__/` 与 `theme/`）对浏览器存储的写入
 * 形态：一旦出现 localStorage / sessionStorage / cookie / IndexedDB 的调用或赋值
 * 即红。`theme/ThemeProvider.tsx` 使用 localStorage 持久化主题偏好（非敏感 UI
 * 选择），已从扫描面排除。只匹配调用/赋值形态（而非裸词）——注释中「不落
 * localStorage」等说明不误报。
 *
 * 豁免清单（§3.5 约束 3 放宽登记）：
 * - `App.tsx` 的 sessionStorage 用于演示模式身份刷新恢复（`DEMO_IDENTITY_KEY`）：
 *   sessionStorage 关闭标签页即清除（比 localStorage 安全），仅演示模式使用
 *   （私有模式 OIDC 会话走 HttpOnly Cookie，不经此通道）。豁免仅限 sessionStorage
 *   模式，App.tsx 仍受 localStorage/cookie/IndexedDB 约束。
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
  it("运行时代码无 localStorage/cookie/IndexedDB 的调用或赋值", () => {
    const runtime = Object.entries(RAW_SOURCES).filter(
      ([path]) => !path.includes("__tests__") && !path.includes("theme/"),
    );
    expect(runtime.length).toBeGreaterThanOrEqual(10); // 扫描面非空防线
    const violations: string[] = [];
    for (const [path, text] of runtime) {
      for (const pattern of FORBIDDEN) {
        // 豁免：App.tsx 的 sessionStorage 用于演示模式身份刷新恢复（见文件头豁免清单）
        if (path.endsWith("App.tsx") && /sessionStorage/.test(pattern.source)) {
          continue;
        }
        if (pattern.test(text)) {
          violations.push(`${path} 命中 ${pattern.source}`);
        }
      }
    }
    expect(violations).toEqual([]);
  });
});

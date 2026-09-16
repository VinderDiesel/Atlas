/**
 * Vite 配置（dev proxy / 双档 target；ADR-0018 决策 ③）。
 *
 * ⚠️ 禁止把 target 写成 `localhost`（实测 2026-09-14，勿"顺手改正"）：
 * `localhost` 优先解析 IPv6 `::1`，而本机 `::1:8000` 是**他人项目**
 * （ccdp-ontology-api），uvicorn 绑的是 IPv4 `127.0.0.1:8000`——代理写 localhost
 * 会把请求送进另一个项目，症状是满屏 404 且极难归因。必须硬写 127.0.0.1。
 *
 * 双档成对切换（只改一个变量会造成前缀与目标不匹配）：
 * - 默认档（不设变量）：TARGET http://127.0.0.1:8000 + BASE /api/v1
 *   —— `make serve-dev`（宿主机新代码）；
 * - 对照档：ATLAS_API_TARGET=http://127.0.0.1:8010 + ATLAS_API_BASE=
 *   —— atlas-api 容器（旧代码），空前缀，仅旧契约对照用；转发时剥掉 /api/v1。
 * 变量从仓根 .env 读（loadEnv 第二参 ".."：npm 脚本 cwd=frontend，.. 即仓根；
 * 空前缀与未设置必须区分，故用 ?? 而非 ||）。
 *
 * dev 签发中间件（P2；0018 推翻条件第 2 条已裁定的 dev-only 便利）：
 * `POST /__dev/sign {role, context}` → spawn `uv run --env-file .env python -c`
 * 调 `serving.auth.sign_token`——与 `make token` 目标**逐字同构**的调用形态
 * （唯一签发实现、唯一密钥路径：密钥只经 .env → uv → 子进程，本文件不读、
 * 不落、不转发任何密钥）。`apply: "serve"` 仅 vite dev；出现多用户/真实鉴权
 * 需求时本中间件必须废除（0018 推翻条件原文），届时前端只剩「make token +
 * 粘贴」通道（调用侧 api/devsign.ts 已按「不可用」归一处理）。
 */
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, loadEnv, type Plugin } from "vite";

const DEFAULT_TARGET = "http://127.0.0.1:8000";
const DEFAULT_BASE = "/api/v1";

export default defineConfig(({ mode }) => {
  // 前缀 ""= 读取全部变量：ATLAS_API_* 不带 VITE_ 前缀，默认前缀扫不到
  const env = loadEnv(mode, "..", "");
  const target = env.ATLAS_API_TARGET ?? DEFAULT_TARGET;
  const base = env.ATLAS_API_BASE ?? DEFAULT_BASE;
  return {
    plugins: [devSignPlugin()],
    server: {
      proxy: {
        "/api/v1": {
          target,
          changeOrigin: true,
          // BASE 是代理侧前缀替换的唯一事实源：/api/v1/ask → BASE/ask
          // （默认档 BASE=/api/v1 → 原样；对照档 BASE="" → /ask）
          rewrite: (path) => path.replace(/^\/api\/v1/, base),
        },
      },
    },
  };
});

// ---------------------------------------------------------------------------
// dev 签发中间件（dev-only；文件头有完整理由）
// ---------------------------------------------------------------------------

/** vite.config.ts 在 frontend/ 下，import.meta.url 的上级即仓根。 */
const REPO_ROOT = fileURLToPath(new URL("..", import.meta.url));

/** 与 `make token` 目标逐字同构的签发脚本（json.loads → sign_token → print）。 */
const SIGN_SCRIPT =
  "import json, sys; from serving.auth import sign_token; " +
  "print(sign_token(sys.argv[1], json.loads(sys.argv[2])))";

const SIGN_TIMEOUT_MS = 20_000;

/** context 白名单：claims 只允许标量与列表（拒嵌套对象——sign_token 的契约面）。 */
function validContext(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  return Object.values(value).every(
    (v) =>
      typeof v === "string" ||
      typeof v === "number" ||
      (Array.isArray(v) && v.every((x) => typeof x === "string" || typeof x === "number")),
  );
}

/**
 * dev 签发中间件（仅 `apply: "serve"`）。
 *
 * 响应契约：200 `{token}` / 400 参数非法或签发失败（detail = stderr 原文）/
 * 405 非 POST / 501 无法启动 uv（不可用态，调用侧按「降级为 make 命令」处理）。
 * 密钥不经本进程：`uv run --env-file .env` 由 uv 读 .env 注入子进程环境。
 */
function devSignPlugin(): Plugin {
  return {
    name: "atlas-dev-sign",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/__dev/sign", (req, res) => {
        let done = false;
        const send = (status: number, body: unknown): void => {
          if (done) {
            return;
          }
          done = true;
          res.statusCode = status;
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify(body));
        };
        if (req.method !== "POST") {
          send(405, { detail: "只接受 POST" });
          return;
        }
        let raw = "";
        req.on("data", (chunk: Buffer) => {
          raw += chunk.toString();
        });
        req.on("end", () => {
          let parsed: unknown;
          try {
            parsed = JSON.parse(raw);
          } catch {
            send(400, { detail: "请求体不是 JSON" });
            return;
          }
          const body = parsed as { role?: unknown; context?: unknown };
          if (typeof body.role !== "string" || body.role.trim() === "") {
            send(400, { detail: "role 必须是非空字符串" });
            return;
          }
          const context = body.context ?? {};
          if (!validContext(context)) {
            send(400, {
              detail: "context 只允许 string|number 标量或它们的数组（claims 契约）",
            });
            return;
          }
          const child = spawn(
            "uv",
            [
              "run",
              "--env-file",
              path.join(REPO_ROOT, ".env"),
              "python",
              "-c",
              SIGN_SCRIPT,
              body.role,
              JSON.stringify(context),
            ],
            { cwd: REPO_ROOT, timeout: SIGN_TIMEOUT_MS },
          );
          let out = "";
          let err = "";
          child.stdout.on("data", (chunk: Buffer) => {
            out += chunk.toString();
          });
          child.stderr.on("data", (chunk: Buffer) => {
            err += chunk.toString();
          });
          child.on("error", (e) => {
            send(501, { detail: `无法启动 uv：${e.message}（uv 需在 PATH；或改用 make token）` });
          });
          child.on("close", (code) => {
            if (code === 0 && out.trim() !== "") {
              send(200, { token: out.trim() });
            } else {
              send(400, { detail: err.trim() || `签发进程退出码 ${code}` });
            }
          });
        });
      });
    },
  };
}

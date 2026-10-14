/**
 * 根组件（P2：路由布局 + 全局单源状态）。
 *
 * 顶栏：标题、SnapshotBadge（/api/v1/health 公开面）、导航（工作台 /
 * 接入 / 治理）、
 * SessionEntry（ADR-0031 D02/D13 认证区：探测 /auth/session 决定形态——演示模式
 * 才是 RoleSwitcher；私有模式为登录入口 /配置阻塞 /身份+退出，token 只存内存）。
 *
 * 全局单源（P2 起，四条）：
 * - `domain`：工作台 model / 顶栏角色清单过滤 / 治理页域选择器共用一个域；
 * - `sessionState {id, log}`：当前会话 id + 本标签页累积的会话日志。
 *   激活**不同身份**即轮换 session_id 并 message 提示（§3.5 约束 1；
 *   首激活静默，同身份重粘贴/重签发不轮换）；
 * - `identity`：token + 解码身份（不验签，仅展示——校验在服务端）；
 * - `queryHistory`：查询历史（localStorage 持久化；ADR-0028 裁定 B 的客户端补位，
 *   跨导航/跨刷新保留，AskWorkbench 消费并展示）。
 *
 * 路由表：`/` → `/ask`；`/ask` 工作台（`key={sessionState.id}`：切角色强制重挂载，
 * 清空上一会话的展示态）；
 * `/setup` 接入页两面板（T07d 数据源接入向导 + T08c 语义草稿与发布：草稿→
 * 校验→审核→patch→Git 制品导入→CAS 发布/回退）；
 * `/governance` → `/governance/models`；`/governance/:section`（8 子页）；`*` 未知路径说明。
 */
import { Alert, App as AntdApp, Layout, Menu, Typography } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router-dom";

import { getJson } from "./api/client";
import { devSign } from "./api/devsign";
import { API } from "./api/endpoints";
import type { HealthPayload, ModelDomain } from "./api/types";
import SnapshotBadge from "./components/SnapshotBadge";
import SessionEntry from "./panels/auth/SessionEntry";
import GovernanceLayout from "./panels/governance/GovernanceLayout";
import RunPanel from "./panels/runs/RunPanel";
import SetupPipeline from "./panels/setup/SetupPipeline";
import AskWorkbench from "./panels/workbench/AskWorkbench";
import { ROLE_SWITCH_NOTICE, identityKey, type ActiveIdentity } from "./state/role";
import {
  appendHistory,
  clearHistory,
  loadHistory,
  removeHistoryEntry,
  saveHistory,
  type QueryHistoryEntry,
} from "./state/query-history";
import {
  recordTurns,
  rotateSession,
  startSessionLog,
  type SessionLogEntry,
} from "./state/session";
import ThemeSwitcher from "./theme/ThemeSwitcher";

/**
 * 演示模式身份持久化键（localStorage）。
 *
 * §3.5 约束 3 放宽：演示模式（OIDC 未配置）下允许刷新保留身份——localStorage
 * 关闭标签页后仍保留（本地开发便利优先），仅存 token + 解码身份（不验签）。
 * 私有模式不使用此键（OIDC 会话走 HttpOnly Cookie，不经此通道）。
 */
const DEMO_IDENTITY_KEY = "atlas_demo_identity";

/** 自动续签提前量（秒）：token 到期前 5 分钟触发续签。 */
const AUTO_RENEW_MARGIN_SECONDS = 300;
/** 自动续签检查间隔（毫秒）：每 60 秒检查一次。 */
const AUTO_RENEW_INTERVAL_MS = 60_000;

const NAV_ITEMS = [
  { key: "/ask", label: "工作台" },
  { key: "/runs", label: "运行" },
  { key: "/setup", label: "数据" },
  { key: "/governance", label: "治理" },
];

/** `/runs` 路由包装（ADR-0031 D13 运行历史）：解析 `?run=` 深链选中。
 * `useSearchParams` 必须在 Router 上下文内——放 App 层，RunPanel 只收 props。 */
function RunRoute({ token }: { token: string }) {
  const [params] = useSearchParams();
  return <RunPanel token={token} initialRunId={params.get("run") ?? undefined} />;
}

interface SessionState {
  id: string;
  log: SessionLogEntry[];
}

export default function App() {
  const { message } = AntdApp.useApp();
  const location = useLocation();
  const navigate = useNavigate();
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [identity, setIdentity] = useState<ActiveIdentity | null>(() => {
    // 演示模式刷新恢复：localStorage 持久化（关闭标签页后仍保留）
    try {
      const stored = localStorage.getItem(DEMO_IDENTITY_KEY);
      if (stored) {
        return JSON.parse(stored) as ActiveIdentity;
      }
    } catch {
      // 存储损坏或不可用：静默回落未认证
    }
    return null;
  });
  const [domain, setDomain] = useState<ModelDomain>("finance");
  // 初始会话 role=null（未认证启动）：首激活轮换时不提示（无「旧会话」可弃）
  const [sessionState, setSessionState] = useState<SessionState>(() => {
    const first = startSessionLog(null, Date.now());
    return { id: first.id, log: [first] };
  });
  // 查询历史（localStorage 持久化；ADR-0028 裁定 B 的客户端补位）
  const [queryHistory, setQueryHistory] = useState<QueryHistoryEntry[]>(() => loadHistory());

  useEffect(() => {
    getJson<HealthPayload>(API.health)
      .then(setHealth)
      .catch((e: unknown) => setHealthError(e instanceof Error ? e.message : String(e)));
  }, []);

  /** 激活身份：身份变化即轮换 session_id 并提示；同身份重激活不轮换（§3.5 约束 1）。 */
  function activateIdentity(next: ActiveIdentity): void {
    const prev = identity;
    setIdentity(next);
    // 演示模式持久化：刷新/关闭标签页后自动恢复（localStorage 持久化）
    try {
      localStorage.setItem(DEMO_IDENTITY_KEY, JSON.stringify(next));
    } catch {
      // 存储不可用：不阻塞激活
    }
    if (
      prev !== null &&
      identityKey(prev.role, prev.claims) === identityKey(next.role, next.claims)
    ) {
      return;
    }
    setSessionState((prevState) => {
      const { log, current } = rotateSession(prevState.log, next.role, Date.now());
      return { id: current.id, log };
    });
    if (prev !== null) {
      void message.info(ROLE_SWITCH_NOTICE);
    }
  }

  /** 成功回合回报：只更新轮数更大的条目；无变化保持原引用（防多余渲染）。 */
  function handleTurnSeen(sessionId: string, turns: number): void {
    setSessionState((prevState) => {
      const log = recordTurns(prevState.log, sessionId, turns);
      const changed = log.some((entry, index) => entry !== prevState.log[index]);
      return changed ? { ...prevState, log } : prevState;
    });
  }

  /** 记录一条查询历史（AskWorkbench 每次成功回合调用；localStorage 持久化）。 */
  function handleRecordHistory(entry: QueryHistoryEntry): void {
    setQueryHistory((prev) => {
      const next = appendHistory(prev, entry);
      saveHistory(next);
      return next;
    });
  }

  /** 删除单条历史（AskWorkbench 历史区调用）。 */
  function handleDeleteHistory(id: string): void {
    setQueryHistory((prev) => {
      const next = removeHistoryEntry(prev, id);
      saveHistory(next);
      return next;
    });
  }

  /** 清空全部历史（AskWorkbench 历史区调用）。 */
  function handleClearHistory(): void {
    setQueryHistory(() => {
      saveHistory([]);
      return [];
    });
  }

  // ---------------------------------------------------------------------------
  // 自动续签（演示模式专属：检测 JWT 即将过期 → 静默调 /__dev/sign 重签）
  // ---------------------------------------------------------------------------
  const renewingRef = useRef(false);

  /** 解码 JWT payload（不验签）提取 exp。失败返回 null。 */
  const getTokenExp = useCallback((token: string): number | null => {
    try {
      const parts = token.split(".");
      if (parts.length !== 3) return null;
      const padded = parts[1] + "=".repeat((-parts[1].length) % 4);
      const payload = JSON.parse(atob(padded));
      return typeof payload.exp === "number" ? payload.exp : null;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    if (identity === null || identity.via !== "sign") return;
    // 仅演示模式 + dev 签发通道可自动续签（paste 通道无角色信息无法重签）

    const tryRenew = async (): Promise<void> => {
      const exp = getTokenExp(identity.token);
      if (exp === null) return;
      const nowSec = Math.floor(Date.now() / 1000);
      const remaining = exp - nowSec;
      // 已过期或剩余不足提前量 → 触发续签
      if (remaining > AUTO_RENEW_MARGIN_SECONDS) return;
      if (renewingRef.current) return;
      renewingRef.current = true;
      try {
        const outcome = await devSign(identity.role, identity.claims);
        if (outcome.kind === "ok") {
          // 解码新 token 的 claims（与签发时一致）
          const parts = outcome.token.split(".");
          if (parts.length === 3) {
            const padded = parts[1] + "=".repeat((-parts[1].length) % 4);
            const payload = JSON.parse(atob(padded)) as {
              role?: string;
              user_context?: Record<string, unknown>;
            };
            const next: ActiveIdentity = {
              token: outcome.token,
              role: typeof payload.role === "string" ? payload.role : identity.role,
              claims:
                typeof payload.user_context === "object" && payload.user_context !== null
                  ? (payload.user_context as Record<string, unknown>)
                  : identity.claims,
              via: "sign",
            };
            setIdentity(next);
            try {
              localStorage.setItem(DEMO_IDENTITY_KEY, JSON.stringify(next));
            } catch {
              // 存储不可用：不阻塞
            }
          }
        }
      } catch {
        // 续签失败：静默忽略，等下次检查或用户手动续签
      } finally {
        renewingRef.current = false;
      }
    };

    // 立即检查一次
    void tryRenew();
    // 定时检查
    const timer = setInterval(() => void tryRenew(), AUTO_RENEW_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [identity, getTokenExp]);

  const selectedKey = location.pathname.startsWith("/governance")
    ? "/governance"
    : location.pathname;

  return (
    <>
      <a href="#main-content" className="skip-link" style={{ position: "absolute", left: -9999, top: "auto", width: 1, height: 1, overflow: "hidden" }}>
        跳至主内容
      </a>
      <Layout style={{ minHeight: "100vh" }}>
        <Layout.Header className="atlas-header">
          <Typography.Title level={4} className="atlas-header__title">
            Atlas 控制台
          </Typography.Title>
          <SnapshotBadge health={health} error={healthError} />
          <Menu
            mode="horizontal"
            selectedKeys={[selectedKey]}
            onClick={({ key }) => navigate(key)}
            items={NAV_ITEMS}
            style={{ flex: 1, minWidth: 0, background: "transparent" }}
          />
          <ThemeSwitcher />
          <SessionEntry identity={identity} domain={domain} onActivate={activateIdentity} />
        </Layout.Header>
        <Layout.Content className="atlas-content">
        <Routes>
          <Route path="/" element={<Navigate to="/ask" replace />} />
          <Route
            path="/ask"
            element={
              <AskWorkbench
                key={sessionState.id}
                token={identity?.token ?? ""}
                model={domain}
                onModelChange={setDomain}
                sessionId={sessionState.id}
                onTurnSeen={handleTurnSeen}
                queryHistory={queryHistory}
                onRecordHistory={handleRecordHistory}
                onDeleteHistory={handleDeleteHistory}
                onClearHistory={handleClearHistory}
              />
            }
          />
          <Route path="/runs" element={<RunRoute token={identity?.token ?? ""} />} />
          <Route
            path="/setup"
            element={<SetupPipeline token={identity?.token ?? ""} />}
          />
          <Route path="/governance" element={<Navigate to="/governance/models" replace />} />
          <Route
            path="/governance/:section"
            element={
              <GovernanceLayout
                token={identity?.token ?? null}
                domain={domain}
                onDomainChange={setDomain}
              />
            }
          />
          <Route
            path="*"
            element={
              <Alert
                type="warning"
                showIcon
                message="未知路径"
                description="控制台有四个入口：/ask（工作台）、/runs（运行历史）、/setup（数据源接入 + 语义草稿与发布）、/governance/:section（治理 8 子页）。"
              />
            }
          />
        </Routes>
      </Layout.Content>
    </Layout>
    </>
  );
}

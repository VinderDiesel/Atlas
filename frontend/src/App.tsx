/**
 * 根组件（P2：路由布局 + 全局单源状态）。
 *
 * 顶栏：标题、SnapshotBadge（/api/v1/health 公开面）、导航（工作台 / 会话 / 治理）、
 * RoleSwitcher（激活身份；token 只存内存，刷新即失——§3.5 约束 3）。
 *
 * 全局单源（P2 起，三条）：
 * - `domain`：工作台 model / 顶栏角色清单过滤 / 治理页域选择器共用一个域；
 * - `sessionState {id, log}`：当前会话 id + 本标签页累积的会话日志（SessionPanel
 *   消费）。激活**不同身份**即轮换 session_id 并 message 提示（§3.5 约束 1；
 *   首激活静默，同身份重粘贴/重签发不轮换）；
 * - `identity`：token + 解码身份（不验签，仅展示——校验在服务端）。
 *
 * 路由表：`/` → `/ask`；`/ask` 工作台（`key={sessionState.id}`：切角色强制重挂载，
 * 清空上一会话的展示态）；`/sessions`；`/governance` → `/governance/models`；
 * `/governance/:section`（8 子页）；`*` 未知路径说明。
 */
import { Alert, App as AntdApp, Layout, Menu, Typography } from "antd";
import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";

import { getJson } from "./api/client";
import { API } from "./api/endpoints";
import type { HealthPayload, ModelDomain } from "./api/types";
import SnapshotBadge from "./components/SnapshotBadge";
import GovernanceLayout from "./panels/governance/GovernanceLayout";
import RoleSwitcher from "./panels/role/RoleSwitcher";
import SessionPanel from "./panels/sessions/SessionPanel";
import AskWorkbench from "./panels/workbench/AskWorkbench";
import { ROLE_SWITCH_NOTICE, identityKey, type ActiveIdentity } from "./state/role";
import {
  recordTurns,
  rotateSession,
  startSessionLog,
  type SessionLogEntry,
} from "./state/session";

const NAV_ITEMS = [
  { key: "/ask", label: "工作台" },
  { key: "/sessions", label: "会话" },
  { key: "/governance", label: "治理" },
];

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
  const [identity, setIdentity] = useState<ActiveIdentity | null>(null);
  const [domain, setDomain] = useState<ModelDomain>("finance");
  // 初始会话 role=null（未认证启动）：首激活轮换时不提示（无「旧会话」可弃）
  const [sessionState, setSessionState] = useState<SessionState>(() => {
    const first = startSessionLog(null, Date.now());
    return { id: first.id, log: [first] };
  });

  useEffect(() => {
    getJson<HealthPayload>(API.health)
      .then(setHealth)
      .catch((e: unknown) => setHealthError(e instanceof Error ? e.message : String(e)));
  }, []);

  /** 激活身份：身份变化即轮换 session_id 并提示；同身份重激活不轮换（§3.5 约束 1）。 */
  function activateIdentity(next: ActiveIdentity): void {
    const prev = identity;
    setIdentity(next);
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

  const selectedKey = location.pathname.startsWith("/governance")
    ? "/governance"
    : location.pathname;

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Header style={{ display: "flex", alignItems: "center", gap: 16, paddingInline: 24 }}>
        <Typography.Title level={4} style={{ color: "#fff", margin: 0, whiteSpace: "nowrap" }}>
          Atlas 控制台
        </Typography.Title>
        <SnapshotBadge health={health} error={healthError} />
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[selectedKey]}
          onClick={({ key }) => navigate(key)}
          items={NAV_ITEMS}
          style={{ flex: 1, minWidth: 0, background: "transparent" }}
        />
        <RoleSwitcher identity={identity} domain={domain} onActivate={activateIdentity} />
      </Layout.Header>
      <Layout.Content style={{ padding: 24 }}>
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
              />
            }
          />
          <Route
            path="/sessions"
            element={
              <SessionPanel
                health={health}
                healthError={healthError}
                log={sessionState.log}
                currentId={sessionState.id}
              />
            }
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
                description="控制台只有三条入口：/ask（工作台）、/sessions（会话）、/governance/:section（治理 8 子页）。"
              />
            }
          />
        </Routes>
      </Layout.Content>
    </Layout>
  );
}

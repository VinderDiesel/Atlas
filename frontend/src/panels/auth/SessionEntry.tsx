/**
 * 顶栏认证区（ADR-0031 D02/D13；T03d 前端登录入口）。
 *
 * 形态由 `state/auth.authView`（纯函数，唯一判定点——退出门的机器断言对象）决定：
 * - probing：探测中，不渲染任何认证控件（私有模式不闪现代理入口）；
 * - demo：演示模式 → RoleSwitcher。**开发角色选择仅此形态可见**（D02 退出门）；
 * - login：私有模式未登录/状态未知 → 登录入口（整页跳 `/api/v1/auth/login`，
 *   由服务端 302 到 IdP；浏览器不经手、不存储 IdP token——D02 ③）；
 * - blocked：配置不全/授予非法 → 显示阻塞说明，**不给登录入口**（D02 ②）；
 * - session：已登录 → 身份弹层（issuer/subject/role/capabilities/到期）+ 退出。
 *
 * 纪律：CSRF token 只在内存（no-persist.test.ts 静态扫描兜底）；退出是写请求，
 * 必须带 `X-Atlas-CSRF` 头（token 来自 session 响应），成功后整页刷新回匿名态。
 */
import { App as AntdApp, Button, Popover, Space, Tooltip, Typography } from "antd";
import { useEffect, useState } from "react";

import { logoutSession, probeSession, type SessionProbe } from "../../api/auth";
import { API } from "../../api/endpoints";
import type { ModelDomain } from "../../api/types";
import { authView, blockedMessage, sessionIdentity } from "../../state/auth";
import type { ActiveIdentity } from "../../state/role";
import RoleSwitcher from "../role/RoleSwitcher";

const { Text } = Typography;

interface Props {
  /** 演示模式身份（App 内存态；私有模式不使用——Cookie 会话不经此通道）。 */
  identity: ActiveIdentity | null;
  /** 当前域（RoleSwitcher 按域过滤角色清单）。 */
  domain: ModelDomain;
  /** 演示模式激活回调（App 负责 session_id 轮换与提示）。 */
  onActivate: (next: ActiveIdentity) => void;
}

/** 会话到期时刻：epoch 秒 → ISO8601（本地化展示不在 M0 范围，原文 ISO 即可）。 */
function expiresText(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toISOString();
}

export default function SessionEntry({ identity, domain, onActivate }: Props) {
  const { message } = AntdApp.useApp();
  const [probe, setProbe] = useState<SessionProbe | null>(null);
  const [logoutBusy, setLogoutBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void probeSession().then((result) => {
      if (!cancelled) {
        setProbe(result);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const view = authView(probe);

  if (view === "demo") {
    return <RoleSwitcher identity={identity} domain={domain} onActivate={onActivate} />;
  }
  if (view === "probing") {
    return null;
  }
  if (view === "blocked") {
    return (
      <Tooltip title={blockedMessage(probe)}>
        <Button danger disabled>
          登录配置阻塞
        </Button>
      </Tooltip>
    );
  }
  if (view === "login") {
    return (
      <Button type="primary" href={API.authLogin}>
        登录
      </Button>
    );
  }

  const session = sessionIdentity(probe);
  if (session === null) {
    // authView 已穷尽形态，此处不可达；防御式返回而不是渲染半残身份
    return null;
  }

  async function handleLogout(): Promise<void> {
    if (session === null) {
      return;
    }
    setLogoutBusy(true);
    try {
      await logoutSession(session.csrf_token);
      // 会话 Cookie 已由服务端清除：整页刷新回到匿名态（内存态归零）
      window.location.assign("/");
    } catch (error) {
      void message.error(`退出失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setLogoutBusy(false);
    }
  }

  return (
    <Space size={8} align="center">
      <Popover
        placement="bottomRight"
        title="私有部署会话（OIDC）"
        content={
          <Space direction="vertical" size={2}>
            <Text>issuer：{session.issuer}</Text>
            <Text>subject：{session.subject}</Text>
            <Text>role：{session.role}</Text>
            <Text>capabilities：{session.capabilities.join("、") || "（无）"}</Text>
            <Text type="secondary">会话到期：{expiresText(session.expires_at)}</Text>
            <Text type="secondary">
              IdP token 不下发浏览器（D02 ③）；工作台 /runs 通道属 M1（T05/T06）。
            </Text>
          </Space>
        }
      >
        <Button size="small">{`${session.role} · ${session.subject}`}</Button>
      </Popover>
      <Button size="small" loading={logoutBusy} onClick={() => void handleLogout()}>
        退出
      </Button>
    </Space>
  );
}

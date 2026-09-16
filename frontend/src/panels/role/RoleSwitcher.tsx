/**
 * 面板 2：角色切换器（顶栏组件区；设计页 §2/§3.5、ADR-0021 决策 ⑤）——P2。
 *
 * 消费 `GET /api/v1/governance/policies`（0022 端点 6；Bearer）：角色清单与
 * claims 契约全部来自该响应——**矩阵不硬编码**（§3.5），按当前域过滤（域唯一
 * 事实源是 App，与工作台的 model 选择同源）。
 *
 * 双通道激活身份（token 只存内存，刷新即失——§3.5 约束 3 的刻意不对称）：
 * ① 粘贴通道（引导 / 回退）：`make token ROLE=… CONTEXT='<json>'` 签发后粘贴。
 *    治理端点需 Bearer，**未认证时取不到 policies**（2026-09-16 实测 401）→
 *    首次激活与刷新后必须先粘贴一次；设计页 §3.5 的「刷新后点一次角色」据此
 *    收窄为「刷新后重粘贴」，偏差登记见 dev-plan §2.7 回执；
 * ② 签发通道（dev-only）：POST /__dev/sign（vite middleware 调 sign_token，
 *    与 make token 逐字同构）——仅 `make ui-dev` 下存在；不可用/失败时降级为
 *    「复制 make 命令 + 粘贴」，不伪造第二套签发逻辑。
 *
 * 三条硬约束的落点：
 * - 约束 1：激活不同身份时由 App 旋转 session_id 并 message 提示——本组件的
 *   已认证态常驻显示 ROLE_SWITCH_NOTICE（不得静默新建会话）；
 * - 约束 2：列表 claims 帮助文本在含列表输入时恒显（LIST_CLAIM_HELP——说明
 *   CLI 的 --role-ctx 不解析列表值，本面板是便捷入口）；
 * - 约束 3：token 只经 onActivate 回调进 App 内存态；本组件零持久化
 *   （__tests__/no-persist.test.ts 全量扫描兜底）。
 */
import { Alert, Button, Divider, Input, Popover, Space, Tooltip, Typography } from "antd";
import { Fragment, useEffect, useState, type ReactNode } from "react";

import { getJson } from "../../api/client";
import { devSign } from "../../api/devsign";
import { API } from "../../api/endpoints";
import type { Envelope, ModelDomain, PolicyItem } from "../../api/types";
import CopyBlock from "../../components/CopyBlock";
import ErrorNote from "../../components/ErrorNote";
import {
  LIST_CLAIM_HELP,
  ROLE_SWITCH_NOTICE,
  ROLE_UNREGISTERED_NOTICE,
  buildUserContext,
  claimFieldsOf,
  decodeTokenPayload,
  formatClaimValue,
  makeTokenCommand,
  rolesForDomain,
  type ActiveIdentity,
} from "../../state/role";

const { Text } = Typography;

interface Props {
  /** 当前身份（App 内存态；null = 未认证）。 */
  identity: ActiveIdentity | null;
  /** 当前域（App 单源；角色清单按域过滤——§3.5）。 */
  domain: ModelDomain;
  /** 激活回调（App 负责 session_id 轮换与 ROLE_SWITCH_NOTICE 提示）。 */
  onActivate: (next: ActiveIdentity) => void;
}

type SignNote = { kind: "ok" | "unavailable" | "error"; text: string };

export default function RoleSwitcher({ identity, domain, onActivate }: Props) {
  const [policies, setPolicies] = useState<Envelope<"governance.policies", PolicyItem> | null>(
    null,
  );
  const [policiesError, setPoliciesError] = useState<unknown>(null);
  const [pasteValue, setPasteValue] = useState("");
  const [pasteError, setPasteError] = useState<string | null>(null);
  const [selectedRole, setSelectedRole] = useState<string | null>(null);
  const [claimValues, setClaimValues] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [signBusy, setSignBusy] = useState(false);
  const [signNote, setSignNote] = useState<SignNote | null>(null);
  const [fallbackCommand, setFallbackCommand] = useState<string | null>(null);

  const token = identity?.token ?? null;

  // 角色清单：身份激活（token 变化）即取；未认证时不发必 401 的请求
  useEffect(() => {
    if (token === null) {
      setPolicies(null);
      setPoliciesError(null);
      return;
    }
    let cancelled = false;
    getJson<Envelope<"governance.policies", PolicyItem>>(API.governancePolicies, token)
      .then((envelope) => {
        if (!cancelled) {
          setPolicies(envelope);
          setPoliciesError(null);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setPoliciesError(error);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const roles = policies === null ? [] : rolesForDomain(policies.items, domain);
  const selected = roles.find((role) => role.name === selectedRole) ?? null;
  const fields = selected === null ? null : claimFieldsOf(selected);

  function pickRole(name: string): void {
    setSelectedRole(name);
    setClaimValues({});
    setFormError(null);
    setSignNote(null);
    setFallbackCommand(null);
  }

  async function signAndActivate(): Promise<void> {
    if (selected === null || !selected.registered) {
      return;
    }
    const built = buildUserContext(selected, claimValues);
    if (!built.ok) {
      setFormError(`缺少必填 claims：${built.missing.join("、")}`);
      return;
    }
    setSignBusy(true);
    setFormError(null);
    setSignNote(null);
    setFallbackCommand(null);
    try {
      const outcome = await devSign(selected.name, built.context);
      if (outcome.kind === "ok") {
        const decoded = decodeTokenPayload(outcome.token);
        if (decoded === null) {
          setSignNote({
            kind: "error",
            text: "签发返回的 token 无法解码（非三段 JWT）——拒绝激活",
          });
          return;
        }
        setSignNote({ kind: "ok", text: `已激活 ${decoded.role}（dev 签发）` });
        onActivate({
          token: outcome.token,
          role: decoded.role,
          claims: decoded.user_context,
          via: "sign",
        });
      } else if (outcome.kind === "unavailable") {
        setSignNote({ kind: "unavailable", text: outcome.detail });
        setFallbackCommand(makeTokenCommand(selected.name, built.context));
      } else {
        setSignNote({ kind: "error", text: `HTTP ${outcome.status}：${outcome.detail}` });
      }
    } finally {
      setSignBusy(false);
    }
  }

  function activatePasted(): void {
    const trimmed = pasteValue.trim();
    if (trimmed === "") {
      setPasteError("token 为空");
      return;
    }
    const decoded = decodeTokenPayload(trimmed);
    if (decoded === null) {
      setPasteError("无法解码 token：需标准三段 JWT（本面板不验签，仅按 payload 显示实际身份）");
      return;
    }
    onActivate({ token: trimmed, role: decoded.role, claims: decoded.user_context, via: "paste" });
    setPasteValue("");
    setPasteError(null);
    setSignNote(null);
  }

  function renderSignSection(): ReactNode {
    if (identity === null) {
      return (
        <Text type="secondary">
          先完成上方①粘贴激活：角色清单来自 /governance/policies（需 Bearer），
          未认证时取不到；激活后本区按域列出角色。
        </Text>
      );
    }
    if (policiesError !== null) {
      return (
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <ErrorNote error={policiesError} title="角色清单加载失败" />
          <Text type="secondary">token 可能已失效：重新签发（make token）后用①粘贴激活。</Text>
        </Space>
      );
    }
    if (policies === null) {
      return <Text type="secondary">角色清单加载中…</Text>;
    }
    const unregistered = roles.filter((role) => !role.registered);
    return (
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <Space wrap size={4}>
          {roles.map((role) => {
            const button = (
              <Button
                size="small"
                type={selectedRole === role.name ? "primary" : "default"}
                disabled={!role.registered}
                onClick={() => pickRole(role.name)}
              >
                {role.name}
              </Button>
            );
            return role.registered ? (
              <Fragment key={role.name}>{button}</Fragment>
            ) : (
              <Tooltip key={role.name} title={ROLE_UNREGISTERED_NOTICE}>
                <span style={{ display: "inline-block", cursor: "not-allowed" }}>{button}</span>
              </Tooltip>
            );
          })}
          {roles.length === 0 && (
            <Text type="secondary">
              {`${domain} 域无角色声明（policies 的 declared_by_models 不含该域）`}
            </Text>
          )}
        </Space>
        {unregistered.length > 0 && (
          <Text type="secondary">
            {`${ROLE_UNREGISTERED_NOTICE}：${unregistered.map((role) => role.name).join("、")}`}
          </Text>
        )}
        {selected !== null && fields !== null && (
          <>
            <Divider style={{ margin: "4px 0" }} />
            <Text strong>{`签发 ${selected.name}（user_context 来自下方表单）`}</Text>
            {fields.scalars.map((key) => (
              <Input
                key={key}
                size="small"
                addonBefore={key}
                placeholder={`必填（${key}）`}
                value={claimValues[key] ?? ""}
                onChange={(e) =>
                  setClaimValues((prev) => ({ ...prev, [key]: e.target.value }))
                }
              />
            ))}
            {fields.lists.map((key) => (
              <Input
                key={key}
                size="small"
                addonBefore={key}
                placeholder="多个值用逗号分隔（签发为 JSON 数组）"
                value={claimValues[key] ?? ""}
                onChange={(e) =>
                  setClaimValues((prev) => ({ ...prev, [key]: e.target.value }))
                }
              />
            ))}
            {fields.lists.length > 0 && <Text type="secondary">{LIST_CLAIM_HELP}</Text>}
            {fields.scalars.length === 0 && fields.lists.length === 0 && (
              <Text type="secondary">该角色无必填 claims（user_context 为空对象）</Text>
            )}
            <Button
              size="small"
              type="primary"
              loading={signBusy}
              onClick={() => void signAndActivate()}
            >
              签发并激活
            </Button>
            {formError !== null && <Text type="danger">{formError}</Text>}
          </>
        )}
        {signNote !== null && (
          <Alert
            type={
              signNote.kind === "ok"
                ? "success"
                : signNote.kind === "unavailable"
                  ? "warning"
                  : "error"
            }
            showIcon
            message={signNote.text}
            description={
              fallbackCommand !== null && (
                <Space direction="vertical" size="small" style={{ width: "100%" }}>
                  <Text type="secondary">改用命令签发后回到①粘贴激活：</Text>
                  <CopyBlock command={fallbackCommand} />
                </Space>
              )
            }
          />
        )}
      </Space>
    );
  }

  const claimsText =
    identity === null || Object.keys(identity.claims).length === 0
      ? "（无）"
      : Object.entries(identity.claims)
          .map(([key, value]) => `${key}=${formatClaimValue(value)}`)
          .join(" · ");

  const content = (
    <div style={{ width: 480, maxHeight: "70vh", overflowY: "auto" }}>
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        {identity === null ? (
          <Text type="secondary">
            当前未认证。工作台与治理面均需 Bearer token（make token 签发）。
          </Text>
        ) : (
          <>
            <Text>
              当前身份：<Text code>{identity.role}</Text>
              {`（${identity.via === "sign" ? "dev 签发" : "粘贴"}）`}
            </Text>
            <Text type="secondary">claims：{claimsText}</Text>
            <Text type="warning">{ROLE_SWITCH_NOTICE}</Text>
          </>
        )}
        <Divider style={{ margin: "4px 0" }} />
        <Text strong>① 粘贴激活（引导 / 回退）</Text>
        <Space.Compact style={{ width: "100%" }}>
          <Input.Password
            placeholder="粘贴 token（make token ROLE=… 的输出）"
            value={pasteValue}
            onChange={(e) => {
              setPasteValue(e.target.value);
              setPasteError(null);
            }}
          />
          <Button type="primary" onClick={activatePasted} disabled={pasteValue.trim() === ""}>
            激活
          </Button>
        </Space.Compact>
        {pasteError !== null && <Text type="danger">{pasteError}</Text>}
        <Text type="secondary">
          未认证时治理端点 401、取不到角色清单 → 首次激活与刷新后需先签发一次
          （token 只存内存，刷新即失——设计如此）：
        </Text>
        <CopyBlock command={makeTokenCommand("hq_admin", {})} />
        <Divider style={{ margin: "4px 0" }} />
        <Text strong>{`② 就地签发（dev-only；当前域 ${domain}）`}</Text>
        {renderSignSection()}
      </Space>
    </div>
  );

  return (
    <Popover
      trigger="click"
      placement="bottomRight"
      title="角色切换（面板 2）"
      content={content}
    >
      <Button>{identity === null ? "未认证 · 激活身份" : identity.role}</Button>
    </Popover>
  );
}

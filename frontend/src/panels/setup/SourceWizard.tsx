/**
 * 面板 5：数据源接入向导（SourceWizard；ADR-0031 D03/D04/D13；T07d）。
 *
 * 三块职责（服务端 D03/D04：
 * - **源修订**（GET/POST /manage/sources）：追加式登记 Doris 只读源——秘密只收
 *   环境变量引用（env:<NAME>），明文密码/DSN 在提交前被本向导拒绝（N9），
 *   不在前端构造「带秘密的请求让后端 422」的通道；
 * - **受限探测**（POST /manage/sources/{id}/probes）：固定目录查询、**无请求体**
 *   （不提供任意「测试 SQL」），结果是有时间戳的证据而非永久保证——阻塞原因
 *   逐条中文化展示，未证实能力如实显示为「未证实」；
 * - **部署绑定**（GET/POST /manage/deployments）：创建即 draft（首次批准发布前
 *   active_release_id=null，D04）——发布/激活属 T08，本面板不提供隐式发布。
 *
 * 诚实性纪律：
 * - 渲染只取自固定键（Table 列 + 描述项）：数据里夹带的 host/password 等未知键
 *   不得出现在输出（UI 不泄漏连接串）；secret_ref 引用名可回显（D03）。
 * - token 空串不发请求（认证前零 401 噪声）。
 */
import {
  Alert,
  Button,
  Collapse,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Radio,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { useCallback, useEffect, useState } from "react";

import {
  createDeployment,
  createSource,
  listDeployments,
  listSources,
  probeSource,
} from "../../api/control";
import type {
  DeploymentRecord,
  ProbeBlockedReason,
  ProbeResult,
  SourceRevisionBody,
  SourceRevisionRecord,
  TlsPolicy,
} from "../../api/types";
import EmptyState from "../../components/EmptyState";
import ErrorNote from "../../components/ErrorNote";
import PageHeader from "../../components/PageHeader";
import Section from "../../components/Section";

const { Text } = Typography;

/** 与后端 contracts.py 同形态：env 引用、UTC 偏移、对象名。 */
const SECRET_REF_RE = /^env:[A-Za-z_][A-Za-z0-9_]{0,127}$/;
const OFFSET_RE = /^[+-]\d{2}:\d{2}$/;
const NAME_RE = /^[a-zA-Z0-9_.-]+$/;

/** 向导表单值（多行文本形态；提交前经 validateSourceForm 校验）。 */
export interface SourceFormValues {
  sourceId: string;
  revision: string;
  secretRef: string;
  allowedCatalogsText: string;
  allowedTablesText: string;
  timezone: string;
  tlsPolicy: TlsPolicy;
  queryBudget: number;
}

/** 多行文本 → 条目数组（trim、去空行；不拆分逗号——不猜用户的输入习惯）。 */
export function parseLines(raw: string): string[] {
  return raw
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

/** 表单校验（与 SourceRevisionRequest 同口径的前端防线；空数组 = 通过）。 */
export function validateSourceForm(values: SourceFormValues): string[] {
  const errors: string[] = [];
  if (!NAME_RE.test(values.sourceId)) {
    errors.push("source_id 必须是字母/数字/点/下划线/连字符组成的对象名");
  }
  if (values.revision.trim() === "") {
    errors.push("revision 不能为空（追加式修订需要一个可读的修订名）");
  }
  if (!SECRET_REF_RE.test(values.secretRef)) {
    errors.push("secret_ref 只接受环境变量引用（env:<NAME>）——不接收明文密码/DSN");
  }
  const catalogs = parseLines(values.allowedCatalogsText);
  const tables = parseLines(values.allowedTablesText);
  if (catalogs.length === 0) {
    errors.push("allowed_catalogs 至少一条");
  }
  if (catalogs.some((name) => !NAME_RE.test(name))) {
    errors.push("allowed_catalogs 含不合法条目（只允许对象名字符）");
  }
  if (tables.length === 0) {
    errors.push("allowed_tables 至少一条");
  }
  for (const table of tables) {
    const parts = table.split(".");
    if (parts.length !== 3 || parts.some((part) => !NAME_RE.test(part))) {
      errors.push(`allowed_tables 必须是 catalog.db.table 三段全名：${table}`);
      continue;
    }
    if (!catalogs.includes(parts[0])) {
      errors.push(`allowed_tables 越界（${table} 不属于 allowed_catalogs）`);
    }
  }
  if (!OFFSET_RE.test(values.timezone)) {
    errors.push("timezone 必须是显式 UTC 偏移（如 +08:00）");
  }
  if (!Number.isInteger(values.queryBudget) || values.queryBudget < 1) {
    errors.push("query_budget 必须是不小于 1 的整数");
  }
  return errors;
}

/** 表单 → 提交体（字段名为后端 snake_case；合法前置条件由调用方保证）。 */
export function toSourceBody(values: SourceFormValues): SourceRevisionBody {
  return {
    source_id: values.sourceId,
    revision: values.revision,
    connector_kind: "doris",
    secret_ref: values.secretRef,
    allowed_catalogs: parseLines(values.allowedCatalogsText),
    allowed_tables: parseLines(values.allowedTablesText),
    timezone: values.timezone,
    tls_policy: values.tlsPolicy,
    query_budget: values.queryBudget,
  };
}

/** 阻塞理由中文标签（9 类全覆盖；null → 空串）。 */
const BLOCKED_LABELS: Record<ProbeBlockedReason, string> = {
  credential_missing: "凭据缺失（环境变量未解析）",
  target_forbidden: "目标禁止（云元数据/link-local/回环）",
  target_not_allowlisted: "目标不在允许列表",
  tls_error: "TLS 校验失败",
  table_out_of_whitelist: "表白名单越界",
  read_only_unconfirmed: "只读能力未确认",
  metadata_missing: "元数据缺失（白名单表不存在）",
  credential_rejected: "目标授权撤销（只读账号被拒）",
  connect_failed: "连接失败",
};

export function blockedReasonLabel(reason: ProbeBlockedReason | null): string {
  return reason === null ? "" : BLOCKED_LABELS[reason];
}

/** 源目录表（只渲染固定列；引用名可回显，秘密与 DSN 永不回显）。 */
export function SourceRowsSection({
  items,
  onProbe,
  probing,
}: {
  items: SourceRevisionRecord[];
  onProbe: (sourceId: string) => void;
  probing: string | null;
}) {
  return (
    <Table
      size="small"
      rowKey="source_id"
      pagination={false}
      scroll={{ x: "max-content" }}
      dataSource={items}
      columns={[
        {
          title: "source_id",
          dataIndex: "source_id",
          render: (value: string) => <Text code>{value}</Text>,
        },
        { title: "版本", dataIndex: "version", width: 64 },
        { title: "connector", dataIndex: "connector_kind", width: 88 },
        {
          title: "secret_ref",
          dataIndex: "secret_ref",
          render: (value: string) => <Text code>{value}</Text>,
        },
        {
          title: "catalogs",
          dataIndex: "allowed_catalogs",
          render: (value: string[]) => value.join(", "),
        },
        {
          title: "白名单表",
          dataIndex: "allowed_tables",
          width: 88,
          render: (value: string[]) => `${value.length} 张`,
        },
        { title: "tls", dataIndex: "tls_policy", width: 84 },
        {
          title: "最近探测",
          key: "last_probe",
          width: 240,
          render: (_: unknown, row: SourceRevisionRecord) =>
            row.last_probe === null ? (
              <Text type="secondary">未探测</Text>
            ) : (
              <Space size={4}>
                <Tag color={row.last_probe.status === "ok" ? "green" : "red"}>
                  {row.last_probe.status === "ok" ? "通过" : "阻塞"}
                </Tag>
                <Text type="secondary">
                  {blockedReasonLabel(row.last_probe.blocked_reason)}
                </Text>
              </Space>
            ),
        },
        {
          title: "操作",
          key: "op",
          width: 104,
          render: (_: unknown, row: SourceRevisionRecord) => (
            <Button
              type="link"
              size="small"
              loading={probing === row.source_id}
              onClick={() => onProbe(row.source_id)}
            >
              受限探测
            </Button>
          ),
        },
      ]}
    />
  );
}

/** 探测结果（能力/错误展示；只渲染固定键，未证实能力如实显示为「未证实」）。 */
export function ProbeResultSection({ result }: { result: ProbeResult }) {
  const caps = result.capabilities;
  const capItems: Array<[string, boolean]> = [
    ["只读连接（read_only）", caps.read_only],
    ["元数据探测（metadata_probe）", caps.metadata_probe],
    ["取消查询（cancel_query）", caps.cancel_query],
    ["快照读取（snapshot_read）", caps.snapshot_read],
    ["一致性分析（consistent_analysis）", caps.consistent_analysis],
  ];
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Space size={8} wrap>
        <Tag color={result.status === "ok" ? "green" : "red"}>
          {result.status === "ok" ? "探测通过" : "探测阻塞"}
        </Tag>
        <Text type="secondary">
          source：{result.source_id} · 修订版本：{result.version} · 方言：{caps.dialect}
        </Text>
        <Text type="secondary">观察时间：{result.observed_at}</Text>
      </Space>
      {result.status === "blocked" ? (
        <Alert
          type="warning"
          showIcon
          message={`阻塞原因：${blockedReasonLabel(result.blocked_reason)}`}
          description="探测未建立连接：凭据与目标校验在服务端完成，阻塞配置项不产生任何数据面副作用。"
        />
      ) : (
        <Descriptions size="small" column={2} title="本次探测证实的能力">
          {capItems.map(([label, ok]) => (
            <Descriptions.Item key={label} label={label}>
              {ok ? "本次探测证实" : "未证实"}
            </Descriptions.Item>
          ))}
        </Descriptions>
      )}
      <Text type="secondary">
        engine_version：{result.engine_version ?? "—"} · schema_digest：
        {result.schema_digest ?? "—"} · 可复现声明：False（历史结论不当作本次事实）
      </Text>
      <Text type="secondary">
        探测结果是有时间戳的证据，不是永久保证（D03）；不写回 EX。
      </Text>
    </Space>
  );
}

/** 部署列表（active_release_id=null 即 draft——首次发布前不绑定任何制品）。 */
export function DeploymentRowsSection({ items }: { items: DeploymentRecord[] }) {
  return (
    <Table
      size="small"
      rowKey="deployment_id"
      pagination={false}
      scroll={{ x: "max-content" }}
      dataSource={items}
      columns={[
        {
          title: "deployment_id",
          dataIndex: "deployment_id",
          render: (value: string) => <Text code>{value}</Text>,
        },
        { title: "领域", dataIndex: "scope", width: 96 },
        { title: "source_id", dataIndex: "source_id", width: 152 },
        {
          title: "活动发布",
          dataIndex: "active_release_id",
          width: 240,
          render: (value: string | null) =>
            value === null ? (
              <Tag>draft（未绑定发布）</Tag>
            ) : (
              <Text code>{value}</Text>
            ),
        },
        { title: "revision", dataIndex: "revision", width: 76 },
        {
          title: "创建者",
          key: "created_by",
          width: 168,
          render: (_: unknown, row: DeploymentRecord) =>
            `${row.created_by.issuer}/${row.created_by.subject}`,
        },
      ]}
    />
  );
}

const FORM_DEFAULTS: SourceFormValues = {
  sourceId: "doris-primary",
  revision: "",
  secretRef: "env:",
  allowedCatalogsText: "atlas",
  allowedTablesText: "",
  timezone: "+08:00",
  tlsPolicy: "required",
  queryBudget: 10000,
};

/** 页面头（认证前后同一条：页名与说明不随认证态变化）。 */
const PAGE_HEADER = (
  <PageHeader
    title="数据源"
    description="连接数据源并管理语义模型。"
  />
);

interface DeploymentFormValues {
  deploymentId: string;
  scope: string;
  sourceId: string;
}

interface Props {
  /** Bearer token（App 内存态）；空串时不发请求。 */
  token: string;
}

export default function SourceWizard({ token }: Props) {
  const [sources, setSources] = useState<SourceRevisionRecord[] | null>(null);
  const [sourcesError, setSourcesError] = useState<unknown>(null);
  const [probeResults, setProbeResults] = useState<Record<string, ProbeResult>>({});
  const [probing, setProbing] = useState<string | null>(null);
  const [deployments, setDeployments] = useState<DeploymentRecord[] | null>(null);
  const [deploymentsError, setDeploymentsError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [formErrors, setFormErrors] = useState<string[]>([]);

  const reload = useCallback((): void => {
    if (token === "") {
      return;
    }
    setSourcesError(null);
    listSources(token)
      .then(setSources)
      .catch((e: unknown) => setSourcesError(e));
    setDeploymentsError(null);
    listDeployments(token)
      .then(setDeployments)
      .catch((e: unknown) => setDeploymentsError(e));
  }, [token]);

  useEffect(() => {
    if (token === "") {
      setSources(null);
      setDeployments(null);
      return;
    }
    reload();
  }, [token, reload]);

  function handleProbe(sourceId: string): void {
    setProbing(sourceId);
    setActionError(null);
    probeSource(sourceId, token)
      .then((result) => {
        setProbeResults((prev) => ({ ...prev, [sourceId]: result }));
      })
      .catch((e: unknown) => setActionError(e))
      .finally(() => setProbing(null));
  }

  async function handleCreate(values: SourceFormValues): Promise<void> {
    const errors = validateSourceForm(values);
    setFormErrors(errors);
    if (errors.length > 0) {
      return;
    }
    setActionError(null);
    try {
      await createSource(toSourceBody(values), token);
      reload();
    } catch (e: unknown) {
      setActionError(e);
    }
  }

  async function handleCreateDeployment(values: DeploymentFormValues): Promise<void> {
    setActionError(null);
    try {
      await createDeployment(
        {
          deployment_id: values.deploymentId,
          scope: values.scope,
          source_id: values.sourceId,
        },
        token,
      );
      reload();
    } catch (e: unknown) {
      setActionError(e);
    }
  }

  if (token === "") {
    return (
      <div className="atlas-page">
        {PAGE_HEADER}
        <Section title="源目录">
          <EmptyState
            icon="lock"
            title="尚未登录"
            description="请先在右上角选择角色开始使用"
          />
        </Section>
      </div>
    );
  }

  const probeList = Object.values(probeResults);

  return (
    <div className="atlas-page">
      {PAGE_HEADER}

      <Collapse
        ghost
        size="small"
        className="atlas-rules"
        items={[
          {
            key: "rules",
            label: "使用说明",
            children: (
              <div className="atlas-rules__body">
                <span>
                  登记数据源时，凭据只接受环境变量引用（env:&lt;NAME&gt;），不接受明文密码。
                  表名需使用 catalog.db.table 三段全名。
                </span>
                <span>
                  连接探测使用固定查询验证能力，不执行任意 SQL，不写入数据。
                </span>
                <span>
                  新部署默认为草稿状态，需经发布流程正式激活。
                </span>
              </div>
            ),
          },
        ]}
      />

      {actionError !== null && <ErrorNote error={actionError} title="操作失败" />}

      <Section title="源目录" meta="每源最新修订">
        {sourcesError !== null ? (
          <ErrorNote error={sourcesError} title="源目录加载失败" />
        ) : sources === null ? (
          <Text type="secondary">加载中…</Text>
        ) : sources.length === 0 ? (
          <Text type="secondary">尚无数据源：用下方表单登记第一个 Doris 只读源。</Text>
        ) : (
          <SourceRowsSection items={sources} onProbe={handleProbe} probing={probing} />
        )}
      </Section>

      <Section
        title="登记数据源"
        meta="追加式修订"
      >
        {formErrors.length > 0 && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message="表单校验未通过（未提交）"
            description={
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {formErrors.map((err) => (
                  <li key={err}>{err}</li>
                ))}
              </ul>
            }
          />
        )}
        <Form
          layout="vertical"
          initialValues={FORM_DEFAULTS}
          onFinish={(values: SourceFormValues) => void handleCreate(values)}
        >
          <div className="atlas-group-label">源与修订</div>
          <div className="atlas-grid">
            <Form.Item
              label="source_id"
              name="sourceId"
              rules={[{ required: true, message: "source_id 必填" }]}
            >
              <Input placeholder="doris-primary" />
            </Form.Item>
            <Form.Item
              label="revision（修订名）"
              name="revision"
              rules={[{ required: true, message: "revision 必填" }]}
            >
              <Input placeholder="rev-2026-09-22-1" />
            </Form.Item>
            <Form.Item label="timezone（显式偏移）" name="timezone">
              <Input placeholder="+08:00" />
            </Form.Item>
          </div>

          <div className="atlas-group-label">凭据与传输</div>
          <div className="atlas-grid">
            <Form.Item
              label="secret_ref（环境变量引用）"
              name="secretRef"
              rules={[{ required: true, message: "secret_ref 必填" }]}
              extra="只接受 env:<NAME>；秘密只由服务端从环境变量解析，不回显"
            >
              <Input placeholder="env:ATLAS_DORIS_RO" />
            </Form.Item>
            <Form.Item label="tls_policy" name="tlsPolicy">
              <Radio.Group>
                <Radio.Button value="required">required</Radio.Button>
                <Radio.Button value="disabled">disabled</Radio.Button>
              </Radio.Group>
            </Form.Item>
            <Form.Item label="query_budget" name="queryBudget">
              <InputNumber min={1} max={10_000_000} style={{ width: "100%" }} />
            </Form.Item>
          </div>

          <div className="atlas-group-label">允许清单（每行一条）</div>
          <div className="atlas-grid">
            <Form.Item label="allowed_catalogs" name="allowedCatalogsText">
              <Input.TextArea rows={4} placeholder="atlas" />
            </Form.Item>
            <Form.Item
              label="allowed_tables（catalog.db.table 三段全名）"
              name="allowedTablesText"
              rules={[{ required: true, message: "allowed_tables 必填" }]}
            >
              <Input.TextArea
                rows={4}
                placeholder={"atlas.dwd.fact_trades\natlas.dwd.dim_account"}
              />
            </Form.Item>
          </div>

          <div className="atlas-form-footer">
            <Button type="primary" htmlType="submit">
              登记源修订
            </Button>
          </div>
        </Form>
      </Section>

      {probeList.length > 0 && (
        <Section
          title="探测结果"
          meta="连接验证证据"
        >
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            {probeList.map((result) => (
              <ProbeResultSection key={result.probe_id} result={result} />
            ))}
          </Space>
        </Section>
      )}

      <Section
        title="部署"
        meta="草稿状态，待发布激活"
      >
        {deploymentsError !== null ? (
          <ErrorNote error={deploymentsError} title="部署目录加载失败" />
        ) : deployments === null ? (
          <Text type="secondary">加载中…</Text>
        ) : deployments.length === 0 ? (
          <Text type="secondary">尚无部署：创建后由 T08 的发布流程显式绑定首个制品。</Text>
        ) : (
          <DeploymentRowsSection items={deployments} />
        )}
        <Form
          layout="vertical"
          style={{ marginTop: 16 }}
          initialValues={{ deploymentId: "", scope: "finance", sourceId: "doris-primary" }}
          onFinish={(values: DeploymentFormValues) => void handleCreateDeployment(values)}
        >
          <div className="atlas-grid">
            <Form.Item
              label="deployment_id"
              name="deploymentId"
              rules={[{ required: true, message: "deployment_id 必填" }]}
            >
              <Input placeholder="finance-live" />
            </Form.Item>
            <Form.Item
              label="scope"
              name="scope"
              rules={[{ required: true, message: "scope 必填" }]}
            >
              <Input placeholder="finance" />
            </Form.Item>
            <Form.Item
              label="source_id"
              name="sourceId"
              rules={[{ required: true, message: "source_id 必填" }]}
            >
              <Input placeholder="doris-primary" />
            </Form.Item>
          </div>
          <div className="atlas-form-footer">
            <Button type="primary" htmlType="submit">
              创建 draft 部署
            </Button>
          </div>
        </Form>
      </Section>
    </div>
  );
}

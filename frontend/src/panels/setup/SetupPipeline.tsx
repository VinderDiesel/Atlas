/**
 * 接入流水线（SetupPipeline；ADR-0031 D02/D03/D04/D13；T07+T08 统一）。
 *
 * 把 SourceWizard + SemanticDraft 两块独立面板合并为 6 步引导式流水线：
 *   ① 连接数据源 → ② 部署绑定 → ③ 创建草稿 → ④ 校验与审核 → ⑤ 导入制品 → ⑥ 发布激活
 *
 * 五项 UX 改进（相对旧版 SourceWizard + SemanticDraft）：
 * - 创建+校验一体化：创建草稿后自动校验，一步完成（后端要求 draft_id 存在才能校验）；
 * - 表单复用：创建与编辑共用 DraftFormFields 组件；
 * - 导入下拉：draft_id / source_id 用 Select 选择，不手动输入；
 * - 发布下拉：deployment_id / release_id 用 Select 选择；
 * - 步骤式布局：Steps 组件引导，每步状态一目了然。
 *
 * 诚实性纪律（同旧版）：渲染只取固定键，不泄漏凭据/连接串；token 空串不发请求。
 */
import { DeleteOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Radio,
  Result,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../../api/client";
import {
  createDeployment,
  createDraft,
  createSource,
  deleteDeployment,
  editDraft,
  exportDraftPatch,
  getFileContent,
  getDraft,
  importRelease,
  listDeployments,
  listDrafts,
  listReleases,
  listSources,
  probeSource,
  publishRelease,
  reviewDraft,
  validateDraft,
} from "../../api/control";
import type {
  DeploymentRecord,
  DraftBody,
  DraftContent,
  DraftPatchView,
  DraftRecord,
  DraftReviewRecord,
  DraftValidationRecord,
  ProbeResult,
  ReleaseImportRecord,
  ReleaseRecord,
  SourceProbeSummary,
  SourceRevisionBody,
  SourceRevisionRecord,
  TlsPolicy,
} from "../../api/types";
import EmptyState from "../../components/EmptyState";
import ErrorNote from "../../components/ErrorNote";
import PageHeader from "../../components/PageHeader";
import Section from "../../components/Section";
import StarBadge from "../../components/StarBadge";

import HarborLane from "./HarborLane";

const { Text } = Typography;

// ---------------------------------------------------------------------------
// 常量与辅助
// ---------------------------------------------------------------------------

const SECRET_REF_RE = /^env:[A-Za-z_][A-Za-z0-9_]{0,127}$/;
const OFFSET_RE = /^[+-]\d{2}:\d{2}$/;
const NAME_RE = /^[a-zA-Z0-9_.-]+$/;
const TARGET_RE = /^semantic\/ossie\/[A-Za-z0-9_-]+\.ossie\.yaml$/;

/** 6 步流水线定义已迁至 HarborLane（PIPELINE_STEPS）；草稿状态展示统一用 StarBadge 契约原文。 */

/** 多行文本 → 条目数组。 */
function parseLines(raw: string): string[] {
  return raw.split("\n").map((l) => l.trim()).filter((l) => l !== "");
}

/** 泊位卡头：泊位号 chip（01–06 等宽）+ 标题。 */
function berthTitle(number: number, title: string) {
  return (
    <>
      <span className="atlas-berth-chip atlas-mono">{`${String(number).padStart(2, "0")}`}</span>
      {title}
    </>
  );
}

/** 仪表读数（label + 等宽值），用于卡片 extra 计数。 */
function gauge(label: string, value: number) {
  return (
    <span className="atlas-gauge">
      <span className="atlas-gauge__label">{label}</span>
      <span className="atlas-gauge__value atlas-mono">{value}</span>
    </span>
  );
}

/** 探测状态星体（导出供测试）：null → 「未探测」文本（契约外事实不造词）。 */
export function ProbeBadge({ probe }: { probe: SourceProbeSummary | null }) {
  if (probe === null) {
    return <Text type="secondary">未探测</Text>;
  }
  return <StarBadge status={probe.status} />;
}

/** 活动发布绑定星体（导出供测试）：null → unbound；有值 → bound + 等宽 sha。 */
export function ActiveReleaseBadge({ releaseId }: { releaseId: string | null }) {
  if (releaseId === null) {
    return <StarBadge status="unbound" />;
  }
  return (
    <Space size={4}>
      <StarBadge status="bound" />
      <Text code>{`${releaseId.slice(0, 16)}…`}</Text>
    </Space>
  );
}

// ---------------------------------------------------------------------------
// 共享草稿表单字段（创建与编辑复用）
// ---------------------------------------------------------------------------

function DraftFormFields({
  scopeDisabled = false,
  targetDisabled = false,
  form,
  token,
  onActionError,
}: {
  scopeDisabled?: boolean;
  targetDisabled?: boolean;
  form?: any;
  token?: string;
  onActionError?: (error: unknown) => void;
}) {
  const [loadingFile, setLoadingFile] = useState(false);

  async function handleLoadFromFile(): Promise<void> {
    if (!form || !token) return;
    const target = form.getFieldValue("target");
    if (!target) {
      onActionError?.(new Error("请先填写 target 路径"));
      return;
    }
    setLoadingFile(true);
    try {
      const result = await getFileContent(target, token);
      // 使用 js-yaml 解析 YAML 并转为 JSON（自动去除所有注释）
      const yaml = await import("js-yaml");
      const parsed = yaml.load(result.content);
      const jsonContent = JSON.stringify(parsed, null, 2);
      form.setFieldsValue({ documentText: jsonContent });
    } catch (e) {
      onActionError?.(e);
    } finally {
      setLoadingFile(false);
    }
  }

  return (
    <>
      <div className="atlas-group-label">目标与领域</div>
      <div className="atlas-grid">
        <Form.Item
          label="scope（领域）"
          name="scope"
          rules={[{ required: true, message: "scope 必填" }]}
        >
          <Input placeholder="finance" disabled={scopeDisabled} />
        </Form.Item>
        <Form.Item
          label="target（制品路径）"
          name="target"
          rules={[{ required: true, message: "target 必填" }]}
          extra="semantic/ossie/<name>.ossie.yaml"
        >
          <Input disabled={targetDisabled} />
        </Form.Item>
      </div>
      <div className="atlas-group-label">
        文档内容（JSON）
        {!targetDisabled && form && token && (
          <Button
            type="link"
            size="small"
            onClick={handleLoadFromFile}
            loading={loadingFile}
            style={{ marginLeft: 8 }}
          >
            从现有文件加载
          </Button>
        )}
      </div>
      <Form.Item
        label="document"
        name="documentText"
        rules={[{ required: true, message: "document 必填" }]}
      >
        <Input.TextArea rows={8} className="atlas-editor" />
      </Form.Item>
    </>
  );
}

// ---------------------------------------------------------------------------
// 源表单校验（与 SourceWizard 同口径）
// ---------------------------------------------------------------------------

interface SourceFormValues {
  sourceId: string;
  revision: string;
  secretRef: string;
  allowedCatalogsText: string;
  allowedTablesText: string;
  timezone: string;
  tlsPolicy: TlsPolicy;
  queryBudget: number;
}

function validateSourceForm(v: SourceFormValues): string[] {
  const errors: string[] = [];
  if (!NAME_RE.test(v.sourceId)) errors.push("source_id 必须是合法对象名");
  if (v.revision.trim() === "") errors.push("revision 不能为空");
  if (!SECRET_REF_RE.test(v.secretRef)) errors.push("secret_ref 必须是 env:<NAME>");
  const catalogs = parseLines(v.allowedCatalogsText);
  const tables = parseLines(v.allowedTablesText);
  if (catalogs.length === 0) errors.push("allowed_catalogs 至少一条");
  if (tables.length === 0) errors.push("allowed_tables 至少一条");
  for (const t of tables) {
    const parts = t.split(".");
    if (parts.length !== 3 || parts.some((p) => !NAME_RE.test(p))) {
      errors.push(`allowed_tables 必须是 catalog.db.table：${t}`);
      continue;
    }
    if (!catalogs.includes(parts[0])) errors.push(`allowed_tables 越界：${t}`);
  }
  if (!OFFSET_RE.test(v.timezone)) errors.push("timezone 必须是 UTC 偏移（如 +08:00）");
  if (!Number.isInteger(v.queryBudget) || v.queryBudget < 1) errors.push("query_budget ≥ 1");
  return errors;
}

function toSourceBody(v: SourceFormValues): SourceRevisionBody {
  return {
    source_id: v.sourceId,
    revision: v.revision,
    connector_kind: "doris",
    secret_ref: v.secretRef,
    allowed_catalogs: parseLines(v.allowedCatalogsText),
    allowed_tables: parseLines(v.allowedTablesText),
    timezone: v.timezone,
    tls_policy: v.tlsPolicy,
    query_budget: v.queryBudget,
  };
}

// ---------------------------------------------------------------------------
// 草稿表单校验
// ---------------------------------------------------------------------------

interface DraftFormValues {
  scope: string;
  target: string;
  documentText: string;
}

function parseJsonObject(raw: string): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return { ok: false, error: "顶层必须是 JSON 对象" };
    }
    return { ok: true, value: parsed as Record<string, unknown> };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

function validateDraftFormValues(v: DraftFormValues): string[] {
  const errors: string[] = [];
  if (!NAME_RE.test(v.scope)) errors.push("scope 必须是合法对象名");
  if (!v.target.startsWith("semantic/ossie/")) errors.push("target 必须在 semantic/ossie/ 下");
  else if (!TARGET_RE.test(v.target)) errors.push("target 必须匹配 *.ossie.yaml");
  const parsed = parseJsonObject(v.documentText);
  if (!parsed.ok) errors.push(`document：${parsed.error}`);
  return errors;
}

function toDraftBody(v: DraftFormValues): DraftBody {
  const parsed = parseJsonObject(v.documentText);
  if (!parsed.ok) throw new Error(parsed.error);
  return { kind: "semantic", scope: v.scope, content: { target: v.target, document: parsed.value } };
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

interface Props {
  token: string;
}

export default function SetupPipeline({ token }: Props) {
  // 数据
  const [sources, setSources] = useState<SourceRevisionRecord[] | null>(null);
  const [deployments, setDeployments] = useState<DeploymentRecord[] | null>(null);
  const [drafts, setDrafts] = useState<DraftRecord[] | null>(null);
  const [releases, setReleases] = useState<ReleaseRecord[] | null>(null);
  const [loading, setLoading] = useState(false);

  // 当前步骤
  const [currentStep, setCurrentStep] = useState(0);

  // 全局错误
  const [actionError, setActionError] = useState<unknown>(null);
  // 认证错误（401）
  const [authError, setAuthError] = useState<string | null>(null);

  // Step 0: 源
  const [probing, setProbing] = useState<string | null>(null);
  const [probeResult, setProbeResult] = useState<ProbeResult | null>(null);

  // Step 0: 源编辑
  const [editingSource, setEditingSource] = useState<SourceRevisionRecord | null>(null);
  const [sourceForm] = Form.useForm();

  // Step 1: 部署
  const [deploymentForm] = Form.useForm();

  // Step 2+3: 草稿
  const [selectedDraft, setSelectedDraft] = useState<DraftRecord | null>(null);
  const [validation, setValidation] = useState<DraftValidationRecord | null>(null);
  const [review, setReview] = useState<DraftReviewRecord | null>(null);
  const [patch, setPatch] = useState<DraftPatchView | null>(null);
  const [draftLoading, setDraftLoading] = useState(false);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();

  // Step 4: 导入
  const [importRecord, setImportRecord] = useState<ReleaseImportRecord | null>(null);
  const [importing, setImporting] = useState(false);

  // Step 5: 激活
  const [activating, setActivating] = useState(false);
  const [activateForm] = Form.useForm();

  // 加载数据
  const reload = useCallback((): void => {
    if (!token) return;
    setLoading(true);
    setAuthError(null);
    Promise.all([
      listSources(token)
        .then(setSources)
        .catch((e) => {
          if (e instanceof ApiError && e.status === 401) {
            setAuthError("认证已过期或无效，请重新登录");
          }
          setSources([]);
        }),
      listDeployments(token)
        .then(setDeployments)
        .catch((e) => {
          if (e instanceof ApiError && e.status === 401) {
            setAuthError("认证已过期或无效，请重新登录");
          }
          setDeployments([]);
        }),
      listDrafts(token)
        .then(setDrafts)
        .catch((e) => {
          if (e instanceof ApiError && e.status === 401) {
            setAuthError("认证已过期或无效，请重新登录");
          }
          setDrafts([]);
        }),
      listReleases(token)
        .then(setReleases)
        .catch((e) => {
          if (e instanceof ApiError && e.status === 401) {
            setAuthError("认证已过期或无效，请重新登录");
          }
          setReleases([]);
        }),
    ]).finally(() => setLoading(false));
  }, [token]);

  useEffect(() => {
    if (token) reload();
  }, [token, reload]);

  // 自动选中首个
  useEffect(() => {
    if (sources && sources.length > 0 && !probing) {
      // 有源后自动跳到步骤 1
    }
  }, [sources]);

  // 选中草稿时加载详情
  useEffect(() => {
    if (!selectedDraft || !token) return;
    getDraft(selectedDraft.draft_id, token)
      .then((d) => {
        setSelectedDraft(d);
        editForm.setFieldsValue({
          scope: d.scope,
          target: d.content.target,
          documentText: JSON.stringify(d.content.document, null, 2),
        });
      })
      .catch(() => {});
  }, [selectedDraft?.draft_id]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------------------
  // 处理函数
  // ---------------------------------------------------------------------------

  async function handleCreateSource(values: SourceFormValues): Promise<void> {
    const errors = validateSourceForm(values);
    if (errors.length > 0) {
      setActionError(new Error(errors.join("；")));
      return;
    }
    setActionError(null);
    try {
      await createSource(toSourceBody(values), token);
      setEditingSource(null);
      sourceForm.resetFields();
      await reload();
      setCurrentStep(1);
    } catch (e) {
      setActionError(e);
    }
  }

  function handleEditSource(source: SourceRevisionRecord): void {
    setEditingSource(source);
    sourceForm.setFieldsValue({
      sourceId: source.source_id,
      revision: `rev-${new Date().toISOString().slice(0, 10)}-${source.version + 1}`,
      secretRef: source.secret_ref,
      allowedCatalogsText: source.allowed_catalogs.join("\n"),
      allowedTablesText: source.allowed_tables.join("\n"),
      timezone: source.timezone,
      tlsPolicy: source.tls_policy,
      queryBudget: source.query_budget,
    });
  }

  function handleProbe(sourceId: string): void {
    setProbing(sourceId);
    setActionError(null);
    probeSource(sourceId, token)
      .then(setProbeResult)
      .catch(setActionError)
      .finally(() => setProbing(null));
  }

  async function handleCreateDeployment(values: {
    deploymentId: string;
    scope: string;
    sourceId: string;
  }): Promise<void> {
    setActionError(null);
    try {
      await createDeployment(
        { deployment_id: values.deploymentId, scope: values.scope, source_id: values.sourceId },
        token,
      );
      await reload();
      setCurrentStep(2);
    } catch (e) {
      setActionError(e);
    }
  }

  async function handleDeleteDeployment(deploymentId: string): Promise<void> {
    setActionError(null);
    try {
      await deleteDeployment(deploymentId, token);
      await reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setActionError(new Error("该部署有活动发布，请先回退再删除"));
      } else {
        setActionError(e);
      }
    }
  }

  async function handleCreateDraft(values: DraftFormValues): Promise<void> {
    const errors = validateDraftFormValues(values);
    if (errors.length > 0) {
      setActionError(new Error(errors.join("；")));
      return;
    }
    setActionError(null);
    setDraftLoading(true);
    try {
      const draft = await createDraft(toDraftBody(values), token);
      await reload();
      setSelectedDraft(draft);
      // 创建后自动校验
      try {
        const val = await validateDraft(draft.draft_id, token);
        setValidation(val);
        const refreshed = await getDraft(draft.draft_id, token);
        setSelectedDraft(refreshed);
      } catch {
        // 校验失败不阻塞，用户可手动重试
      }
      setCurrentStep(3);
    } catch (e) {
      setActionError(e);
    } finally {
      setDraftLoading(false);
    }
  }

  async function handleEditDraft(): Promise<void> {
    if (!selectedDraft) return;
    const text = editForm.getFieldValue("documentText") as string;
    const parsed = parseJsonObject(text);
    if (!parsed.ok) {
      setActionError(new Error(`document：${parsed.error}`));
      return;
    }
    setActionError(null);
    setDraftLoading(true);
    try {
      const updated = await editDraft(
        selectedDraft.draft_id,
        { target: selectedDraft.content.target, document: parsed.value },
        selectedDraft.revision,
        token,
      );
      setSelectedDraft(updated);
      setValidation(null);
      setReview(null);
      setPatch(null);
      await reload();
    } catch (e) {
      setActionError(e);
    } finally {
      setDraftLoading(false);
    }
  }

  async function handleValidate(): Promise<void> {
    if (!selectedDraft) return;
    setActionError(null);
    setDraftLoading(true);
    try {
      const val = await validateDraft(selectedDraft.draft_id, token);
      setValidation(val);
      const refreshed = await getDraft(selectedDraft.draft_id, token);
      setSelectedDraft(refreshed);
    } catch (e) {
      setActionError(e);
    } finally {
      setDraftLoading(false);
    }
  }

  async function handleReview(decision: "approved" | "rejected", comment: string): Promise<void> {
    if (!selectedDraft) return;
    setActionError(null);
    setDraftLoading(true);
    try {
      const rev = await reviewDraft(
        selectedDraft.draft_id,
        comment ? { decision, comment } : { decision },
        token,
      );
      setReview(rev);
      const refreshed = await getDraft(selectedDraft.draft_id, token);
      setSelectedDraft(refreshed);
    } catch (e) {
      setActionError(e);
    } finally {
      setDraftLoading(false);
    }
  }

  async function handleExportPatch(): Promise<void> {
    if (!selectedDraft) return;
    setActionError(null);
    try {
      setPatch(await exportDraftPatch(selectedDraft.draft_id, token));
    } catch (e) {
      setActionError(e);
    }
  }

  async function handleImport(values: {
    draftId: string;
    sourceGitSha: string;
    sourceId: string;
  }): Promise<void> {
    setActionError(null);
    setImporting(true);
    try {
      const record = await importRelease(
        { draft_id: values.draftId, source_git_sha: values.sourceGitSha, source_id: values.sourceId },
        token,
      );
      setImportRecord(record);
      const refreshed = await getDraft(values.draftId, token);
      setSelectedDraft(refreshed);
      await reload();
      setCurrentStep(5);
    } catch (e) {
      setActionError(e);
    } finally {
      setImporting(false);
    }
  }

  async function handleActivate(values: {
    deploymentId: string;
    releaseId: string;
    expectedActive: string;
    action: "publish" | "rollback";
  }): Promise<void> {
    setActionError(null);
    setActivating(true);
    try {
      const body = {
        release_id: values.releaseId,
        expected_active_release_id: values.expectedActive || null,
      };
      await publishRelease(values.deploymentId, body, token);
      await reload();
      // 成功后清空表单，防止重复提交
      activateForm.resetFields();
      setActionError(null);
    } catch (e) {
      // 美化 409 错误信息
      if (e instanceof ApiError && e.status === 409) {
        setActionError(
          new Error(
            "发布冲突：活动发布已变化或部署不存在。可能原因：\n" +
              "1. 该发布已被激活（请勿重复提交）\n" +
              "2. 部署已被删除或修改\n" +
              "3. expected_active_release_id 与当前状态不匹配",
          ),
        );
      } else {
        setActionError(e);
      }
    } finally {
      setActivating(false);
    }
  }

  // ---------------------------------------------------------------------------
  // 渲染
  // ---------------------------------------------------------------------------

  if (!token) {
    return (
      <div className="atlas-page">
        <PageHeader title="接入流水线" description="数据接入港 · 数据源 → 部署 → 草稿 → 校验 → 导入 → 发布" />
        <Section title="开始">
          <EmptyState icon="lock" title="尚未登录" description="请先在右上角选择角色开始使用" />
        </Section>
      </div>
    );
  }

  const hasSources = sources !== null && sources.length > 0;
  const hasDeployments = deployments !== null && deployments.length > 0;

  return (
    <div className="atlas-page">
      <PageHeader title="接入流水线" description="数据接入港 · 按泊位完成数据源接入、语义模型发布与部署激活" />

      {authError !== null && (
        <Alert
          type="error"
          message="认证失败"
          description={
            <div>
              <p>{authError}</p>
              <Button
                type="link"
                size="small"
                onClick={() => {
                  setAuthError(null);
                  reload();
                }}
              >
                重新加载
              </Button>
            </div>
          }
          showIcon
          closable
          onClose={() => setAuthError(null)}
          style={{ marginBottom: 16 }}
        />
      )}

      <Collapse
        ghost
        size="small"
        className="atlas-rules"
        items={[
          {
            key: "rules",
            label: "流程说明",
            children: (
              <div className="atlas-rules__body">
                <span>共 6 步：连接数据源 → 部署绑定 → 创建草稿 → 校验审核 → 导入制品 → 发布激活。</span>
                <span>凭据只接受环境变量引用（env:NAME），不接受明文密码。发布前需将变更合入 Git。</span>
                <span>发布与回退使用版本校验（CAS），防止意外覆盖他人发布。</span>
              </div>
            ),
          },
        ]}
      />

      {actionError !== null && <ErrorNote error={actionError} title="操作失败" />}

      <HarborLane current={currentStep} onSelect={setCurrentStep} />

      {loading && <Text type="secondary">加载中…</Text>}

      {/* ── Step 0: 连接数据源 ──────────────────────────────────────── */}
      {currentStep === 0 && (
        <Card
          title={berthTitle(1, "连接数据源")}
          extra={hasSources ? gauge("已连接源", sources?.length ?? 0) : undefined}
        >
          {hasSources && (
            <Table
              size="small"
              rowKey="source_id"
              pagination={false}
              dataSource={sources ?? []}
              style={{ marginBottom: 16 }}
              columns={[
                {
                  title: "source_id",
                  dataIndex: "source_id",
                  render: (v: string) => <Text code>{v}</Text>,
                },
                { title: "connector", dataIndex: "connector_kind", width: 88 },
                {
                  title: "白名单表",
                  dataIndex: "allowed_tables",
                  width: 88,
                  render: (v: string[]) => (
                    <span>
                      <span className="atlas-mono">{v.length}</span>
                      {` 张`}
                    </span>
                  ),
                },
                {
                  title: "探测",
                  key: "probe",
                  width: 140,
                  render: (_: unknown, row: SourceRevisionRecord) => (
                    <ProbeBadge probe={row.last_probe} />
                  ),
                },
                {
                  title: "操作",
                  key: "op",
                  width: 160,
                  render: (_: unknown, row: SourceRevisionRecord) => (
                    <Space size={4}>
                      <Button
                        type="link"
                        size="small"
                        onClick={() => handleEditSource(row)}
                      >
                        编辑
                      </Button>
                      <Button
                        type="link"
                        size="small"
                        loading={probing === row.source_id}
                        onClick={() => handleProbe(row.source_id)}
                      >
                        探测
                      </Button>
                    </Space>
                  ),
                },
              ]}
            />
          )}

          {probeResult && (
            <Alert
              type={probeResult.status === "ok" ? "success" : "warning"}
              showIcon
              style={{ marginBottom: 16 }}
              message={probeResult.status === "ok" ? "探测通过" : `探测阻塞：${probeResult.blocked_reason}`}
              description={
                <span>
                  {"engine: "}
                  <span className="atlas-mono">{probeResult.engine_version ?? "—"}</span>
                  {" · 观察时间: "}
                  <span className="atlas-mono">{probeResult.observed_at}</span>
                </span>
              }
            />
          )}

          <Collapse
            ghost
            items={[
              {
                key: "new-source",
                label: editingSource
                  ? `编辑源：${editingSource.source_id}（v${editingSource.version} → v${editingSource.version + 1}）`
                  : hasSources
                    ? "登记新源"
                    : "登记数据源",
                children: (
                  <Form
                    form={sourceForm}
                    layout="vertical"
                    initialValues={{
                      sourceId: "doris-primary",
                      revision: "",
                      secretRef: "env:ATLAS_DORIS_RO",
                      allowedCatalogsText: "atlas",
                      allowedTablesText: "",
                      timezone: "+08:00",
                      tlsPolicy: "required",
                      queryBudget: 10000,
                    }}
                    onFinish={(v: SourceFormValues) => void handleCreateSource(v)}
                  >
                    <div className="atlas-grid">
                      <Form.Item label="source_id" name="sourceId" rules={[{ required: true }]}>
                        <Input placeholder="doris-primary" disabled={editingSource !== null} />
                      </Form.Item>
                      <Form.Item label="revision" name="revision" rules={[{ required: true }]}>
                        <Input placeholder="rev-2026-09-23-1" />
                      </Form.Item>
                      <Form.Item label="timezone" name="timezone">
                        <Input placeholder="+08:00" />
                      </Form.Item>
                    </div>
                    <div className="atlas-grid">
                      <Form.Item
                        label="secret_ref"
                        name="secretRef"
                        rules={[{ required: true }]}
                        extra="只接受 env:<NAME>"
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
                    <Form.Item label="allowed_catalogs（每行一条）" name="allowedCatalogsText">
                      <Input.TextArea rows={2} placeholder="atlas" />
                    </Form.Item>
                    <Form.Item
                      label="allowed_tables（catalog.db.table，每行一条）"
                      name="allowedTablesText"
                      rules={[{ required: true }]}
                    >
                      <Input.TextArea rows={4} placeholder="atlas.dwd.fact_trades" />
                    </Form.Item>
                    <Space>
                      <Button type="primary" htmlType="submit">
                        {editingSource ? "提交新修订" : "登记源修订"}
                      </Button>
                      {editingSource && (
                        <Button
                          onClick={() => {
                            setEditingSource(null);
                            sourceForm.resetFields();
                          }}
                        >
                          取消编辑
                        </Button>
                      )}
                    </Space>
                  </Form>
                ),
              },
            ]}
          />

          {hasSources && (
            <div style={{ marginTop: 16, textAlign: "right" }}>
              <Button type="primary" onClick={() => setCurrentStep(1)}>
                下一步：部署绑定 →
              </Button>
            </div>
          )}
        </Card>
      )}

      {/* ── Step 1: 部署绑定 ─────────────────────────────────────────── */}
      {currentStep === 1 && (
        <Card
          title={berthTitle(2, "部署绑定")}
          extra={
            hasDeployments ? gauge("已绑定部署", deployments?.length ?? 0) : undefined
          }
        >
          {hasDeployments && (
            <>
              <Table
                size="small"
                rowKey="deployment_id"
                pagination={false}
                dataSource={deployments ?? []}
                style={{ marginBottom: 16 }}
                columns={[
                  {
                    title: "deployment_id",
                    dataIndex: "deployment_id",
                    render: (v: string) => <Text code>{v}</Text>,
                  },
                  { title: "领域", dataIndex: "scope", width: 96 },
                  {
                    title: "source_id",
                    dataIndex: "source_id",
                    width: 152,
                    render: (v: string) => <Text code>{v}</Text>,
                  },
                  {
                    title: "活动发布",
                    dataIndex: "active_release_id",
                    render: (v: string | null) => <ActiveReleaseBadge releaseId={v} />,
                  },
                  {
                    title: "操作",
                    width: 120,
                    align: "center" as const,
                    render: (_: unknown, record: DeploymentRecord) =>
                      record.active_release_id === null ? (
                        <Popconfirm
                          title="确认删除"
                          description={`确定删除部署 ${record.deployment_id}？此操作不可撤销。`}
                          okText="删除"
                          cancelText="取消"
                          okButtonProps={{ danger: true }}
                          onConfirm={() => void handleDeleteDeployment(record.deployment_id)}
                        >
                          <Button danger size="small" icon={<DeleteOutlined />}>
                            删除
                          </Button>
                        </Popconfirm>
                      ) : (
                        <Tooltip title="该部署有活动发布，需先回退才能删除">
                          <Button size="small" disabled icon={<DeleteOutlined />}>
                            删除
                          </Button>
                        </Tooltip>
                      ),
                  },
                ]}
              />

              {/* 已发布制品列表 */}
              {releases && releases.length > 0 && (
                <>
                  <Text strong style={{ display: "block", marginBottom: 8 }}>
                    {`已发布制品 (`}
                    <span className="atlas-mono">{releases.length}</span>
                    {`)`}
                  </Text>
                  <Table
                    size="small"
                    rowKey="release_id"
                    pagination={{ pageSize: 5 }}
                    dataSource={releases}
                    columns={[
                      {
                        title: "release_id",
                        dataIndex: "release_id",
                        render: (v: string) => <Text code style={{ fontSize: 11 }}>{v.slice(0, 16)}…</Text>,
                        width: 180,
                      },
                      { title: "领域", dataIndex: "scope", width: 80 },
                      {
                        title: "source_revision",
                        dataIndex: "source_revision",
                        render: (v: string) => <Text code style={{ fontSize: 11 }}>{v}</Text>,
                        width: 150,
                      },
                      {
                        title: "创建时间",
                        dataIndex: "created_at",
                        render: (v: string) => new Date(v).toLocaleString("zh-CN"),
                        width: 160,
                      },
                    ]}
                  />
                </>
              )}
            </>
          )}

          <Form
            form={deploymentForm}
            layout="vertical"
            initialValues={{
              deploymentId: "",
              scope: "finance",
              sourceId: sources?.[0]?.source_id ?? "",
            }}
            onFinish={(v: { deploymentId: string; scope: string; sourceId: string }) =>
              void handleCreateDeployment(v)
            }
          >
            <div className="atlas-grid">
              <Form.Item label="deployment_id" name="deploymentId" rules={[{ required: true }]}>
                <Input placeholder="finance-live" />
              </Form.Item>
              <Form.Item label="scope" name="scope" rules={[{ required: true }]}>
                <Input placeholder="finance" />
              </Form.Item>
              <Form.Item label="source_id" name="sourceId" rules={[{ required: true }]}>
                <Select
                  placeholder="选择数据源"
                  options={(sources ?? []).map((s) => ({
                    label: `${s.source_id}（${s.connector_kind}，${s.allowed_tables.length} 表）`,
                    value: s.source_id,
                  }))}
                />
              </Form.Item>
            </div>
            <Button type="primary" htmlType="submit">
              创建部署绑定
            </Button>
          </Form>

          {hasDeployments && (
            <div style={{ marginTop: 16, textAlign: "right" }}>
              <Button type="primary" onClick={() => setCurrentStep(2)}>
                下一步：创建草稿 →
              </Button>
            </div>
          )}
        </Card>
      )}

      {/* ── Step 2: 创建草稿 ─────────────────────────────────────────── */}
      {currentStep === 2 && (
        <Card title={berthTitle(3, "创建草稿")} extra={<Text type="secondary">草稿不影响已发布版本</Text>}>
          <Form
            form={createForm}
            layout="vertical"
            initialValues={{
              scope: "finance",
              target: "semantic/ossie/atlas_finance.ossie.yaml",
              documentText: '{\n  "semantic_model": []\n}',
            }}
            onFinish={(v: DraftFormValues) => void handleCreateDraft(v)}
          >
            <DraftFormFields form={createForm} token={token} onActionError={setActionError} />
            <Button type="primary" htmlType="submit" loading={draftLoading}>
              创建并校验 →
            </Button>
          </Form>
        </Card>
      )}

      {/* ── Step 3: 校验与审核 ───────────────────────────────────────── */}
      {currentStep === 3 && (
        <Card
          title={berthTitle(4, "校验与审核")}
          extra={selectedDraft ? <StarBadge status={selectedDraft.status} /> : undefined}
        >
          {/* 草稿选择 */}
          {drafts && drafts.length > 0 && (
            <Space style={{ marginBottom: 16 }}>
              <Text>选择草稿：</Text>
              <Select
                style={{ minWidth: 300 }}
                value={selectedDraft?.draft_id}
                onChange={(id: string) => {
                  const d = drafts.find((x) => x.draft_id === id) ?? null;
                  setSelectedDraft(d);
                  setValidation(null);
                  setReview(null);
                  setPatch(null);
                }}
                options={drafts.map((d) => ({
                  label: `${d.draft_id}（${d.scope} · ${d.status}）`,
                  value: d.draft_id,
                }))}
              />
            </Space>
          )}

          {selectedDraft && (
            <>
              {/* 草稿详情 + 编辑 */}
              <Descriptions size="small" column={2} style={{ marginBottom: 12 }}>
                <Descriptions.Item label="kind">{selectedDraft.kind}</Descriptions.Item>
                <Descriptions.Item label="status">
                  <StarBadge status={selectedDraft.status} />
                </Descriptions.Item>
                <Descriptions.Item label="revision">{selectedDraft.revision}</Descriptions.Item>
                <Descriptions.Item label="content_digest">
                  <Text code style={{ fontSize: 11 }}>{selectedDraft.content_digest}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="base_git_sha">
                  <Text code style={{ fontSize: 11 }}>{selectedDraft.base_git_sha}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="target">{selectedDraft.content.target}</Descriptions.Item>
              </Descriptions>

              <Form form={editForm} layout="vertical">
                <DraftFormFields form={editForm} token={token} onActionError={setActionError} scopeDisabled targetDisabled />
                <Space style={{ marginBottom: 16 }}>
                  <Button onClick={() => void handleEditDraft()} loading={draftLoading}>
                    保存编辑
                  </Button>
                  <Button onClick={() => void handleValidate()} loading={draftLoading}>
                    重新校验
                  </Button>
                  <Button onClick={() => void handleExportPatch()}>导出 patch</Button>
                </Space>
              </Form>

              {/* 校验结果 */}
              {validation && (
                <Alert
                  type={validation.status === "passed" ? "success" : "error"}
                  showIcon
                  style={{ marginBottom: 16 }}
                  message={validation.status === "passed" ? "校验通过" : "校验未通过"}
                  description={
                    validation.findings.length > 0
                      ? validation.findings.map((f) => `${f.code}: ${f.message}`).join("；")
                      : "三套确定性校验器（结构 / 治理 / 策略）未发现异常"
                  }
                />
              )}

              {/* patch */}
              {patch && (
                <div style={{ marginBottom: 16 }}>
                  <Text strong>变更影响面：</Text>
                  <Space size={4} wrap style={{ marginTop: 4 }}>
                    {patch.impact.added_metrics.map((m) => (
                      <Tag key={`+m:${m}`} color="green">+指标 {m}</Tag>
                    ))}
                    {patch.impact.removed_metrics.map((m) => (
                      <Tag key={`-m:${m}`} color="red">-指标 {m}</Tag>
                    ))}
                    {patch.impact.changed_metrics.map((m) => (
                      <Tag key={`~m:${m}`} color="orange">~指标 {m}</Tag>
                    ))}
                    {patch.impact.added_dimensions.map((d) => (
                      <Tag key={`+d:${d}`} color="green">+维度 {d}</Tag>
                    ))}
                    {patch.impact.removed_dimensions.map((d) => (
                      <Tag key={`-d:${d}`} color="red">-维度 {d}</Tag>
                    ))}
                    {patch.impact.changed_dimensions.map((d) => (
                      <Tag key={`~d:${d}`} color="orange">~维度 {d}</Tag>
                    ))}
                  </Space>
                  {patch.patch && <pre className="atlas-pre" style={{ marginTop: 8 }}>{patch.patch}</pre>}
                </div>
              )}

              {/* 审核 */}
              {selectedDraft.status === "validated" || selectedDraft.status === "reviewed" ? (
                <Card size="small" title="人工审核" style={{ marginTop: 8 }}>
                  {review && (
                    <Space size={8} wrap style={{ marginBottom: 12 }}>
                      <Tag color={review.decision === "approved" ? "green" : "red"}>
                        {review.decision === "approved" ? "已批准" : "已拒绝"}
                      </Tag>
                      <Text type="secondary">
                        review_id: {review.review_id} · revision {review.revision}
                      </Text>
                      {review.comment && <Text type="secondary">意见: {review.comment}</Text>}
                    </Space>
                  )}
                  {(selectedDraft.status === "validated" || selectedDraft.status === "reviewed") && (
                    <Space>
                      <Button
                        type="primary"
                        onClick={() => void handleReview("approved", "")}
                        loading={draftLoading}
                      >
                        批准
                      </Button>
                      <Button
                        danger
                        onClick={() => void handleReview("rejected", "")}
                        loading={draftLoading}
                      >
                        拒绝
                      </Button>
                    </Space>
                  )}
                </Card>
              ) : null}

              {/* 下一步引导 */}
              {selectedDraft.status === "reviewed" && (
                <div style={{ marginTop: 16, textAlign: "right" }}>
                  <Button type="primary" onClick={() => setCurrentStep(4)}>
                    下一步：导入制品 →
                  </Button>
                </div>
              )}
            </>
          )}

          {!selectedDraft && (!drafts || drafts.length === 0) && (
            <Empty description="暂无草稿，请先在步骤 3 创建" />
          )}
        </Card>
      )}

      {/* ── Step 4: 导入制品 ─────────────────────────────────────────── */}
      {currentStep === 4 && (
        <Card title={berthTitle(5, "导入制品")} extra={<Text type="secondary">需已合入 Git 的 commit</Text>}>
          {importRecord ? (
            <Result
              status="success"
              title="制品导入成功"
              subTitle={`release_id: ${importRecord.release_id}`}
              extra={
                <Button type="primary" onClick={() => setCurrentStep(5)}>
                  下一步：发布激活 →
                </Button>
              }
            >
              <Descriptions column={1} size="small">
                <Descriptions.Item label="draft_id">
                  <Text code>{importRecord.draft_id}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="content_digest">
                  <Text code>{importRecord.content_digest}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="status">{importRecord.status}</Descriptions.Item>
                <Descriptions.Item label="target">{importRecord.target}</Descriptions.Item>
                <Descriptions.Item label="时间">{importRecord.created_at}</Descriptions.Item>
              </Descriptions>
            </Result>
          ) : (
            <Form
              layout="vertical"
              initialValues={{
                draftId: selectedDraft?.draft_id ?? "",
                sourceGitSha: "",
                sourceId: sources?.[0]?.source_id ?? "",
              }}
              onFinish={(v: { draftId: string; sourceGitSha: string; sourceId: string }) =>
                void handleImport(v)
              }
            >
              <div className="atlas-grid">
                <Form.Item label="草稿" name="draftId" rules={[{ required: true, message: "请选择草稿" }]}>
                  <Select
                    placeholder="选择已审核草稿"
                    options={(drafts ?? [])
                      .filter((d) => d.status === "reviewed" || d.status === "validated")
                      .map((d) => ({
                        label: `${d.draft_id}（${d.scope} · ${d.status}）`,
                        value: d.draft_id,
                      }))}
                  />
                </Form.Item>
                <Form.Item
                  label="source_id"
                  name="sourceId"
                  rules={[{ required: true, message: "请选择数据源" }]}
                >
                  <Select
                    placeholder="选择数据源"
                    options={(sources ?? []).map((s) => ({
                      label: `${s.source_id}（${s.connector_kind}）`,
                      value: s.source_id,
                    }))}
                  />
                </Form.Item>
              </div>
              <Form.Item
                label="source_git_sha（已合入 Git 的 commit）"
                name="sourceGitSha"
                rules={[{ required: true, message: "请输入 commit SHA" }]}
                extra="将变更合入 Git 后，运行 git rev-parse HEAD 获取"
              >
                <Input placeholder="40 位十六进制 commit SHA" />
              </Form.Item>
              <Button type="primary" htmlType="submit" loading={importing}>
                导入制品
              </Button>
            </Form>
          )}
        </Card>
      )}

      {/* ── Step 5: 发布激活 ─────────────────────────────────────────── */}
      {currentStep === 5 && (
        <Card title={berthTitle(6, "发布激活")} extra={<Text type="secondary">CAS 版本校验防冲突</Text>}>
          <Form
            form={activateForm}
            layout="vertical"
            initialValues={{
              deploymentId: deployments?.[0]?.deployment_id ?? "",
              releaseId: importRecord?.release_id ?? "",
              expectedActive: deployments?.[0]?.active_release_id ?? "",
              action: "publish",
            }}
            onFinish={(v: {
              deploymentId: string;
              releaseId: string;
              expectedActive: string;
              action: "publish" | "rollback";
            }) => void handleActivate(v)}
          >
            <div className="atlas-grid">
              <Form.Item
                label="部署"
                name="deploymentId"
                rules={[{ required: true, message: "请选择部署" }]}
              >
                <Select
                  placeholder="选择部署"
                  options={(deployments ?? []).map((d) => ({
                    label: `${d.deployment_id}（${d.scope}）`,
                    value: d.deployment_id,
                  }))}
                />
              </Form.Item>
              <Form.Item
                label="release_id"
                name="releaseId"
                rules={[{ required: true, message: "请选择制品" }]}
              >
                <Select
                  placeholder="选择发布制品"
                  options={(releases ?? []).map((r) => ({
                    label: `${r.release_id.slice(0, 16)}…（${r.scope} · ${r.source_revision}）`,
                    value: r.release_id,
                  }))}
                />
              </Form.Item>
              <Form.Item label="expected_active_release_id（留空 = 首发）" name="expectedActive">
                <Input placeholder="当前活动发布（空 = 期望无活动发布）" />
              </Form.Item>
            </div>
            <Form.Item label="动作" name="action">
              <Radio.Group>
                <Radio.Button value="publish">发布</Radio.Button>
                <Radio.Button value="rollback">回退</Radio.Button>
              </Radio.Group>
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={activating}>
              提交
            </Button>
          </Form>
        </Card>
      )}
    </div>
  );
}

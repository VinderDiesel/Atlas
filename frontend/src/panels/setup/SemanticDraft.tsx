/**
 * 面板 5b：语义草稿与发布（SemanticDraft；ADR-0031 D02/D04/D13；T08c）。
 *
 * 生命周期（D04）：`draft → validated → reviewed → source_imported →
 * release_ready → published → retired`——本面板把每一步都做成显式动作：
 *
 * - **草稿**（GET/POST /manage/drafts、GET/PUT /manage/drafts/{id}）：UI 编辑写
 *   SQLite 非权威草稿，**不写当前 active 文件**；编辑是 PUT + `If-Match:
 *   "<revision>"` 的 CAS（不盲写、冲突 409 原文展示）；修改内容使旧验证/审核
 *   失效（服务端原语，前端同步清空本地证据展示）；
 * - **校验 / 审核**（POST .../validations|reviews）：校验无请求体（校验对象就是
 *   草稿内容本身，不接受调用方指定规则集）；审核决定绑定当前摘要；
 * - **patch**（GET .../patch）：最小统一 diff + 影响面（base Git sha / 内容摘要 /
 *   指标·维度六类变更）——首版由人把 patch 合入 Git，本面板不代为提交；
 * - **导入 / 发布**（POST /manage/releases/imports、POST deployments/{id}/
 *   releases|rollbacks）：导入只收**显式 commit 的制品**（服务不自动
 *   commit/push/merge、不读脏工作树）；发布/回退以 `expected_active_release_id`
 *   做 CAS，不静默覆盖他人发布。
 *
 * 诚实性纪律：
 * - 渲染只取自固定键（Descriptions/Table 列）：数据夹带的 password/host 等未知
 *   键不得出现在输出；
 * - 影响面只展示契约实际字段（指标/维度六类）——不声称契约没有的报表/流程；
 * - token 空串不发请求（认证前零 401 噪声）。
 */
import {
  Alert,
  Button,
  Collapse,
  Descriptions,
  Form,
  Input,
  Radio,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { useCallback, useEffect, useState } from "react";

import {
  createDraft,
  editDraft,
  exportDraftPatch,
  getDraft,
  importRelease,
  listDrafts,
  publishRelease,
  reviewDraft,
  rollbackRelease,
  validateDraft,
} from "../../api/control";
import type {
  ActivationRecord,
  DraftBody,
  DraftPatchView,
  DraftRecord,
  DraftReviewRecord,
  DraftStatus,
  DraftValidationRecord,
  PatchImpact,
  ReleaseImportRecord,
} from "../../api/types";
import EmptyState from "../../components/EmptyState";
import ErrorNote from "../../components/ErrorNote";
import PageHeader from "../../components/PageHeader";
import Section from "../../components/Section";

const { Text } = Typography;

/** 与后端 contracts.py 同形态：对象名与 kind=semantic 的制品白名单。 */
const NAME_RE = /^[a-zA-Z0-9_.-]+$/;
const TARGET_PREFIX = "semantic/ossie/";
const TARGET_RE = /^semantic\/ossie\/[A-Za-z0-9_-]+\.ossie\.yaml$/;

/** 草稿表单值（document 为 JSON 文本形态；提交前经 validateDraftForm 校验）。 */
export interface DraftFormValues {
  scope: string;
  target: string;
  documentText: string;
}

/** JSON 文本 → 对象（判别联合；错误原文不吞，不静默改写）。 */
export function parseJsonObject(
  raw: string,
): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { ok: false, error: "顶层必须是 JSON 对象（当前是数组或标量）" };
  }
  return { ok: true, value: parsed as Record<string, unknown> };
}

/**
 * 表单校验（与 DraftRequest/DraftContentInvalid 同口径的前端防线；空数组 = 通过）。
 *
 * target 双门：kind=semantic 前缀（`semantic/ossie/`）+ 制品白名单全匹配——
 * 路径穿越（`..`）与非法扩展名在提交前被拒。
 */
export function validateDraftForm(values: DraftFormValues): string[] {
  const errors: string[] = [];
  if (!NAME_RE.test(values.scope)) {
    errors.push("scope 必须是非空对象名（字母/数字/点/下划线/连字符）");
  }
  if (!values.target.startsWith(TARGET_PREFIX)) {
    errors.push("target 必须位于 semantic/ossie/ 目录（kind=semantic 的制品白名单）");
  } else if (!TARGET_RE.test(values.target)) {
    errors.push("target 必须匹配 semantic/ossie/<name>.ossie.yaml 白名单形态");
  }
  const parsed = parseJsonObject(values.documentText);
  if (!parsed.ok) {
    errors.push(`document 必须是合法 JSON 对象：${parsed.error}`);
  }
  return errors;
}

/** 表单 → 提交体（字段名为后端 snake_case；合法前置条件由调用方保证）。 */
export function toDraftBody(values: DraftFormValues): DraftBody {
  const parsed = parseJsonObject(values.documentText);
  if (!parsed.ok) {
    throw new Error(`document 不是合法 JSON 对象：${parsed.error}`);
  }
  return {
    kind: "semantic",
    scope: values.scope,
    content: { target: values.target, document: parsed.value },
  };
}

/** 草稿状态中文标签（7 状态全覆盖；镜像 DraftStatus）。 */
const DRAFT_STATUS_LABELS: Record<DraftStatus, string> = {
  draft: "草稿（未校验）",
  validated: "已校验（结构/治理/策略）",
  reviewed: "已审核（人工批准）",
  source_imported: "已导入源",
  release_ready: "制品就绪（未激活）",
  published: "已发布（活动制品）",
  retired: "已退役",
};

export function draftStatusLabel(status: DraftStatus): string {
  return DRAFT_STATUS_LABELS[status];
}

/** 草稿目录表（只渲染固定列；夹带的隐藏字段不出现在输出）。 */
export function DraftRowsSection({
  items,
  selectedId,
  onSelect,
}: {
  items: DraftRecord[];
  selectedId: string | null;
  onSelect: (draftId: string) => void;
}) {
  return (
    <Table
      size="small"
      rowKey="draft_id"
      pagination={false}
      scroll={{ x: "max-content" }}
      dataSource={items}
      columns={[
        {
          title: "draft_id",
          dataIndex: "draft_id",
          render: (value: string) => <Text code>{value}</Text>,
        },
        { title: "kind", dataIndex: "kind", width: 96 },
        { title: "领域", dataIndex: "scope", width: 88 },
        {
          title: "状态",
          dataIndex: "status",
          width: 200,
          render: (value: DraftStatus) => <Tag>{draftStatusLabel(value)}</Tag>,
        },
        { title: "revision", dataIndex: "revision", width: 76 },
        {
          title: "base Git sha",
          dataIndex: "base_git_sha",
          width: 340,
          render: (value: string) => <Text code>{value}</Text>,
        },
        {
          title: "操作",
          key: "op",
          width: 88,
          render: (_: unknown, row: DraftRecord) => (
            <Button type="link" size="small" onClick={() => onSelect(row.draft_id)}>
              {row.draft_id === selectedId ? "编辑中" : "编辑"}
            </Button>
          ),
        },
      ]}
    />
  );
}

/** 校验证据（status + 逐条发现：校验器 code + 原文；绑定 revision/摘要）。 */
export function ValidationResultSection({
  validation,
}: {
  validation: DraftValidationRecord;
}) {
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Space size={8} wrap>
        <Tag color={validation.status === "passed" ? "green" : "red"}>
          {validation.status === "passed" ? "校验通过" : "校验不通过"}
        </Tag>
        <Text type="secondary">
          绑定 revision {validation.revision} · content_digest {validation.content_digest}
        </Text>
        <Text type="secondary">证据时间：{validation.created_at}</Text>
      </Space>
      {validation.findings.length === 0 ? (
        <Text type="secondary">无发现（三套确定性校验器：结构 / 治理 / 策略）</Text>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {validation.findings.map((finding) => (
            <li key={`${finding.code}:${finding.message}`}>
              <Text code>{finding.code}</Text> {finding.message}
            </li>
          ))}
        </ul>
      )}
    </Space>
  );
}

/** 补丁影响面（契约 6 键：指标/维度 × 新增/删除/修改；全空即无变更）。 */
export function ImpactSection({ impact }: { impact: PatchImpact }) {
  const rows: Array<[string, string[]]> = [
    ["新增指标", impact.added_metrics],
    ["删除指标", impact.removed_metrics],
    ["修改指标", impact.changed_metrics],
    ["新增维度", impact.added_dimensions],
    ["删除维度", impact.removed_dimensions],
    ["修改维度", impact.changed_dimensions],
  ];
  if (rows.every(([, names]) => names.length === 0)) {
    return <Text type="secondary">影响面：无变更（内容与 base Git 版本逐块一致）</Text>;
  }
  return (
    <Descriptions size="small" column={1} title="变更影响面（指标 / 维度；确定性 diff）">
      {rows.map(([label, names]) => (
        <Descriptions.Item key={label} label={label}>
          {names.length === 0 ? <Text type="secondary">无</Text> : names.join("、")}
        </Descriptions.Item>
      ))}
    </Descriptions>
  );
}

/** patch 视图（base Git sha / 内容摘要 / target / patch 原文 / 影响面）。 */
export function PatchViewSection({ view }: { view: DraftPatchView }) {
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Space size={8} wrap>
        <Text type="secondary">
          target：{view.target} · 修订：{view.revision}
        </Text>
      </Space>
      <Text type="secondary">base_git_sha：{view.base_git_sha}</Text>
      <Text type="secondary">content_digest：{view.content_digest}</Text>
      <ImpactSection impact={view.impact} />
      {view.patch === "" ? (
        <Text type="secondary">
          空 patch：草稿内容与 base Git 版本一致（未编辑）——无内容可合入。
        </Text>
      ) : (
        <pre className="atlas-pre">{view.patch}</pre>
      )}
      <Text type="secondary">
        patch 由人合入 Git（服务不自动 commit/push/merge）；合入后在本面板导入显式 commit 的制品。
      </Text>
    </Space>
  );
}

/** 导入回执（制品身份 + release_ready；导入不激活）。 */
export function ImportResultSection({ record }: { record: ReleaseImportRecord }) {
  return (
    <Descriptions size="small" column={1} title="导入回执（制品就绪，未激活）">
      <Descriptions.Item label="release_id">
        <Text code>{record.release_id}</Text>
      </Descriptions.Item>
      <Descriptions.Item label="content_digest">
        <Text code>{record.content_digest}</Text>
      </Descriptions.Item>
      <Descriptions.Item label="draft">
        <Text code>{record.draft_id}</Text>（revision {record.draft_revision}）
      </Descriptions.Item>
      <Descriptions.Item label="status">{record.status}（激活是独立 CAS 动作）</Descriptions.Item>
      <Descriptions.Item label="target">{record.target}</Descriptions.Item>
      <Descriptions.Item label="时间">{record.created_at}</Descriptions.Item>
    </Descriptions>
  );
}

/** 激活回执（固定 6 键；publish/rollback 区分；首发 previous=null 如实显示）。 */
export function ActivationResultSection({ record }: { record: ActivationRecord }) {
  return (
    <Descriptions
      size="small"
      column={1}
      title={record.action === "publish" ? "发布回执（指针已切换）" : "回退回执（指针已切换）"}
    >
      <Descriptions.Item label="deployment">
        <Text code>{record.deployment_id}</Text>
      </Descriptions.Item>
      <Descriptions.Item label="动作">
        {record.action === "publish" ? "发布" : "回退"}
      </Descriptions.Item>
      <Descriptions.Item label="先前发布">
        {record.previous_release_id === null ? (
          "（无：首次发布）"
        ) : (
          <Text code>{record.previous_release_id}</Text>
        )}
      </Descriptions.Item>
      <Descriptions.Item label="活动发布">
        <Text code>{record.active_release_id}</Text>
      </Descriptions.Item>
      <Descriptions.Item label="指针版本">{record.revision}</Descriptions.Item>
      <Descriptions.Item label="时间">{record.updated_at}</Descriptions.Item>
    </Descriptions>
  );
}

const DRAFT_DEFAULTS: DraftFormValues = {
  scope: "finance",
  target: "semantic/ossie/atlas_finance.ossie.yaml",
  documentText: '{\n  "semantic_model": []\n}',
};

const PAGE_HEADER = (
  <PageHeader
    title="语义模型"
    description="管理语义模型的草稿、审核与发布。"
  />
);

interface ImportFormValues {
  draftId: string;
  sourceGitSha: string;
  sourceId: string;
}

interface ActivationFormValues {
  deploymentId: string;
  releaseId: string;
  expectedActiveReleaseId: string;
  action: "publish" | "rollback";
}

interface Props {
  /** Bearer token（App 内存态）；空串时不发请求。 */
  token: string;
}

export default function SemanticDraft({ token }: Props) {
  const [drafts, setDrafts] = useState<DraftRecord[] | null>(null);
  const [draftsError, setDraftsError] = useState<unknown>(null);
  const [selected, setSelected] = useState<DraftRecord | null>(null);
  const [editorText, setEditorText] = useState<string>("");
  const [validation, setValidation] = useState<DraftValidationRecord | null>(null);
  const [review, setReview] = useState<DraftReviewRecord | null>(null);
  const [patch, setPatch] = useState<DraftPatchView | null>(null);
  const [importRecord, setImportRecord] = useState<ReleaseImportRecord | null>(null);
  const [activation, setActivation] = useState<ActivationRecord | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [formErrors, setFormErrors] = useState<string[]>([]);

  const reload = useCallback((): void => {
    if (token === "") {
      return;
    }
    setDraftsError(null);
    listDrafts(token)
      .then(setDrafts)
      .catch((e: unknown) => setDraftsError(e));
  }, [token]);

  useEffect(() => {
    if (token === "") {
      setDrafts(null);
      return;
    }
    reload();
  }, [token, reload]);

  /** 选中草稿：拉详情（含 content）并填入编辑器；旧证据展示清空。 */
  function handleSelect(draftId: string): void {
    setActionError(null);
    setValidation(null);
    setReview(null);
    setPatch(null);
    getDraft(draftId, token)
      .then((draft) => {
        setSelected(draft);
        setEditorText(JSON.stringify(draft.content.document, null, 2));
      })
      .catch((e: unknown) => setActionError(e));
  }

  async function handleCreate(values: DraftFormValues): Promise<void> {
    const errors = validateDraftForm(values);
    setFormErrors(errors);
    if (errors.length > 0) {
      return;
    }
    setActionError(null);
    try {
      await createDraft(toDraftBody(values), token);
      reload();
    } catch (e: unknown) {
      setActionError(e);
    }
  }

  async function handleEdit(): Promise<void> {
    if (selected === null) {
      return;
    }
    const parsed = parseJsonObject(editorText);
    if (!parsed.ok) {
      setFormErrors([`document 必须是合法 JSON 对象：${parsed.error}`]);
      return;
    }
    setFormErrors([]);
    setActionError(null);
    try {
      const updated = await editDraft(
        selected.draft_id,
        { target: selected.content.target, document: parsed.value },
        selected.revision,
        token,
      );
      setSelected(updated);
      setEditorText(JSON.stringify(updated.content.document, null, 2));
      // 内容变更使旧证据失效（服务端原语；本地展示同步清空）
      setValidation(null);
      setReview(null);
      setPatch(null);
      reload();
    } catch (e: unknown) {
      setActionError(e);
    }
  }

  async function handleValidate(): Promise<void> {
    if (selected === null) {
      return;
    }
    setActionError(null);
    try {
      const result = await validateDraft(selected.draft_id, token);
      setValidation(result);
      const refreshed = await getDraft(selected.draft_id, token);
      setSelected(refreshed);
      reload();
    } catch (e: unknown) {
      setActionError(e);
    }
  }

  async function handleReview(decision: "approved" | "rejected", comment: string): Promise<void> {
    if (selected === null) {
      return;
    }
    setActionError(null);
    try {
      const result = await reviewDraft(
        selected.draft_id,
        comment === "" ? { decision } : { decision, comment },
        token,
      );
      setReview(result);
      const refreshed = await getDraft(selected.draft_id, token);
      setSelected(refreshed);
      reload();
    } catch (e: unknown) {
      setActionError(e);
    }
  }

  async function handleExportPatch(): Promise<void> {
    if (selected === null) {
      return;
    }
    setActionError(null);
    try {
      setPatch(await exportDraftPatch(selected.draft_id, token));
    } catch (e: unknown) {
      setActionError(e);
    }
  }

  async function handleImport(values: ImportFormValues): Promise<void> {
    setActionError(null);
    try {
      setImportRecord(
        await importRelease(
          {
            draft_id: values.draftId,
            source_git_sha: values.sourceGitSha,
            source_id: values.sourceId,
          },
          token,
        ),
      );
      reload();
    } catch (e: unknown) {
      setActionError(e);
    }
  }

  async function handleActivate(values: ActivationFormValues): Promise<void> {
    setActionError(null);
    const body = {
      release_id: values.releaseId,
      expected_active_release_id:
        values.expectedActiveReleaseId === "" ? null : values.expectedActiveReleaseId,
    };
    try {
      setActivation(
        values.action === "publish"
          ? await publishRelease(values.deploymentId, body, token)
          : await rollbackRelease(values.deploymentId, body, token),
      );
    } catch (e: unknown) {
      setActionError(e);
    }
  }

  if (token === "") {
    return (
      <div className="atlas-page">
        {PAGE_HEADER}
        <Section title="草稿目录">
          <EmptyState
            icon="lock"
            title="尚未登录"
            description="请先在右上角选择角色开始使用"
          />
        </Section>
      </div>
    );
  }

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
                  编辑操作只写入草稿，不影响已发布的语义模型。修改内容后需重新校验和审核。
                </span>
                <span>
                  发布前需将变更合入 Git 仓库，然后在此导入已提交的制品。
                </span>
                <span>
                  发布与回退操作使用版本校验，防止意外覆盖。
                </span>
              </div>
            ),
          },
        ]}
      />

      {actionError !== null && <ErrorNote error={actionError} title="操作失败" />}

      <Section title="草稿目录" meta="按已授权领域服务端裁剪">
        {draftsError !== null ? (
          <ErrorNote error={draftsError} title="草稿目录加载失败" />
        ) : drafts === null ? (
          <Text type="secondary">加载中…</Text>
        ) : drafts.length === 0 ? (
          <Text type="secondary">尚无草稿：用下方表单创建第一个语义草稿。</Text>
        ) : (
          <DraftRowsSection
            items={drafts}
            selectedId={selected?.draft_id ?? null}
            onSelect={handleSelect}
          />
        )}
      </Section>

      <Section
        title="新建草稿"
        meta="草稿不影响已发布版本"
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
          initialValues={DRAFT_DEFAULTS}
          onFinish={(values: DraftFormValues) => void handleCreate(values)}
        >
          <div className="atlas-group-label">目标与领域</div>
          <div className="atlas-grid">
            <Form.Item
              label="scope（领域）"
              name="scope"
              rules={[{ required: true, message: "scope 必填" }]}
            >
              <Input placeholder="finance" />
            </Form.Item>
            <Form.Item
              label="target（semantic/ossie/<name>.ossie.yaml）"
              name="target"
              rules={[{ required: true, message: "target 必填" }]}
              extra="目标命中制品白名单；路径穿越/非法扩展名在提交前被拒"
            >
              <Input />
            </Form.Item>
          </div>

          <div className="atlas-group-label">文档内容</div>
          <div className="atlas-grid">
            <Form.Item
              label="document（JSON 对象；即 ossie 语义模型的解析形态）"
              name="documentText"
              rules={[{ required: true, message: "document 必填" }]}
            >
              <Input.TextArea rows={8} className="atlas-editor" />
            </Form.Item>
          </div>

          <div className="atlas-form-footer">
            <Button type="primary" htmlType="submit">
              创建草稿
            </Button>
          </div>
        </Form>
      </Section>

      {selected !== null && (
        <Section
          title="编辑草稿"
          meta={`draft_id=${selected.draft_id}`}
        >
          <Descriptions size="small" column={2} style={{ marginBottom: 12 }}>
            <Descriptions.Item label="kind">{selected.kind}</Descriptions.Item>
            <Descriptions.Item label="status">
              <Tag>{draftStatusLabel(selected.status)}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="revision">{selected.revision}</Descriptions.Item>
            <Descriptions.Item label="content_digest">
              <Text code>{selected.content_digest}</Text>
            </Descriptions.Item>
            <Descriptions.Item label="base_git_sha">
              <Text code>{selected.base_git_sha}</Text>
            </Descriptions.Item>
            <Descriptions.Item label="owner">
              {selected.owner.issuer}/{selected.owner.subject}
            </Descriptions.Item>
            <Descriptions.Item label="target">{selected.content.target}</Descriptions.Item>
            <Descriptions.Item label="updated_at">{selected.updated_at}</Descriptions.Item>
          </Descriptions>
          <Input.TextArea
            rows={10}
            value={editorText}
            onChange={(event) => setEditorText(event.target.value)}
            className="atlas-editor"
          />
          <div className="atlas-form-footer">
            <Button type="primary" onClick={() => void handleEdit()}>
              保存
            </Button>
            <Button onClick={() => void handleValidate()}>确定性校验</Button>
            <Button onClick={() => void handleExportPatch()}>导出 patch</Button>
          </div>
        </Section>
      )}

      {validation !== null && (
        <Section title="校验证据">
          <ValidationResultSection validation={validation} />
        </Section>
      )}

      {selected !== null && (
        <Section
          title="人工审核"
          meta="只认「已校验」状态"
        >
          <Form
            layout="vertical"
            initialValues={{ decision: "approved", comment: "" }}
            onFinish={(values: { decision: "approved" | "rejected"; comment: string }) =>
              void handleReview(values.decision, values.comment)
            }
          >
            <div className="atlas-grid">
              <Form.Item label="决定（绑定当前摘要）" name="decision">
                <Radio.Group>
                  <Radio.Button value="approved">approved（批准）</Radio.Button>
                  <Radio.Button value="rejected">rejected（拒绝）</Radio.Button>
                </Radio.Group>
              </Form.Item>
              <Form.Item label="意见（可选）" name="comment">
                <Input placeholder="口径核对说明" />
              </Form.Item>
            </div>
            <div className="atlas-form-footer">
              <Button type="primary" htmlType="submit">
                提交审核
              </Button>
            </div>
          </Form>
          {review !== null && (
            <Space size={8} wrap style={{ marginTop: 12 }}>
              <Tag color={review.decision === "approved" ? "green" : "red"}>
                {review.decision === "approved" ? "已批准" : "已拒绝"}
              </Tag>
              <Text type="secondary">
                review_id：{review.review_id} · 绑定 revision {review.revision} · 摘要{" "}
                {review.content_digest}
              </Text>
              {review.comment !== null && <Text type="secondary">意见：{review.comment}</Text>}
            </Space>
          )}
        </Section>
      )}

      {patch !== null && (
        <Section title="patch 与影响面">
          <PatchViewSection view={patch} />
        </Section>
      )}

      <Section
        title="导入制品"
        meta="需已合入 Git 的 commit"
      >
        <Form
          key={selected?.draft_id ?? "none"}
          layout="vertical"
          initialValues={{
            draftId: selected?.draft_id ?? "",
            sourceGitSha: "",
            sourceId: "doris-primary",
          }}
          onFinish={(values: ImportFormValues) => void handleImport(values)}
        >
          <div className="atlas-grid">
            <Form.Item
              label="draft_id"
              name="draftId"
              rules={[{ required: true, message: "draft_id 必填" }]}
            >
              <Input placeholder="draft-1" />
            </Form.Item>
            <Form.Item
              label="source_git_sha（已合入的显式 commit）"
              name="sourceGitSha"
              rules={[{ required: true, message: "source_git_sha 必填" }]}
            >
              <Input placeholder="40 位 hex" />
            </Form.Item>
            <Form.Item
              label="source_id"
              name="sourceId"
              rules={[{ required: true, message: "source_id 必填" }]}
            >
              <Input />
            </Form.Item>
          </div>
          <div className="atlas-form-footer">
            <Button type="primary" htmlType="submit">
              导入制品
            </Button>
          </div>
        </Form>
        {importRecord !== null && (
          <div style={{ marginTop: 16 }}>
            <ImportResultSection record={importRecord} />
          </div>
        )}
      </Section>

      <Section
        title="发布与回退"
        meta="版本校验防止冲突"
      >
        <Form
          layout="vertical"
          initialValues={{
            deploymentId: "finance-live",
            releaseId: "",
            expectedActiveReleaseId: "",
            action: "publish",
          }}
          onFinish={(values: ActivationFormValues) => void handleActivate(values)}
        >
          <div className="atlas-grid">
            <Form.Item
              label="deployment_id"
              name="deploymentId"
              rules={[{ required: true, message: "deployment_id 必填" }]}
            >
              <Input />
            </Form.Item>
            <Form.Item
              label="release_id"
              name="releaseId"
              rules={[{ required: true, message: "release_id 必填" }]}
            >
              <Input placeholder="制品 release_id（64 位 hex）" />
            </Form.Item>
            <Form.Item
              label="expected_active_release_id（留空 = 首发）"
              name="expectedActiveReleaseId"
            >
              <Input placeholder="当前指针（空 = 期望无活动发布）" />
            </Form.Item>
            <Form.Item label="动作" name="action">
              <Radio.Group>
                <Radio.Button value="publish">publish（发布）</Radio.Button>
                <Radio.Button value="rollback">rollback（回退）</Radio.Button>
              </Radio.Group>
            </Form.Item>
          </div>
          <div className="atlas-form-footer">
            <Button type="primary" htmlType="submit">
              提交
            </Button>
          </div>
        </Form>
        {activation !== null && (
          <div style={{ marginTop: 16 }}>
            <ActivationResultSection record={activation} />
          </div>
        )}
      </Section>
    </div>
  );
}

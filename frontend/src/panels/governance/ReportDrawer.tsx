/**
 * 治理子页 7 的钻取：评测报告（`/governance/reports/{name}`）——P3。
 *
 * 两种响应形态（serving/governance.py `_report_detail`）：
 * - **主报告**（文件名 = 纯 sha）：body 原样（7 键 + runner.py 演进可能追加的
 *   契约外键）→ 结构化渲染：Descriptions 四键 + notes 原文 + summary 递归展开
 *   （flattenKv，不假设嵌套形状）+ samples 全键并集表格（不挑好看的键）；
 * - **非主报告**：`{name, pattern, structured: false, raw}` → 降级渲染
 *   （§3.3 第 2 行的文案义务：`reportDegradeNote` + 原始 JSON 全文）——
 *   **不得伪造统一表头、不把 raw 摊平成表格列**。
 *
 * dry 标注（§3.3 第 3 行）走 `dryNote` 单一文案出口；structured 判别用
 * `payload.structured === false`（主报告 body 无 structured 键，缺失 ≠ false）。
 */
import { Alert, Collapse, Descriptions, Drawer, Space, Spin, Table, Tag, Typography } from "antd";
import { useEffect, useState, type CSSProperties } from "react";

import { getJson } from "../../api/client";
import { API } from "../../api/endpoints";
import type { ReportDetail, ReportDetailPayload, ReportRawFallback } from "../../api/types";
import ErrorNote from "../../components/ErrorNote";
import { dryNote, reportDegradeNote } from "../../lib/honesty";
import { cellText, flattenKv, sampleColumns, type FlatKvRow } from "../../lib/report";
import { shortSha } from "../../lib/sha";

import { dashOr } from "./GovernanceSection";

const { Text, Paragraph } = Typography;

type DrawerState =
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "ok"; payload: ReportDetailPayload };

interface Props {
  /** 报告文件名 stem（主报告 = 纯 sha）；null = 关闭（挂载态由父组件持有）。 */
  name: string | null;
  token: string;
  onClose: () => void;
}

/** 主报告 7 键（runner.py 产出）；其余键是契约外演进 → 原样 JSON 折叠展示。 */
const KNOWN_REPORT_KEYS: readonly string[] = [
  "sha",
  "created_at",
  "dry",
  "domains",
  "summary",
  "samples",
  "notes",
];

function isRawFallback(payload: ReportDetailPayload): payload is ReportRawFallback {
  return "structured" in payload && payload.structured === false;
}

const preStyle: CSSProperties = {
  margin: 0,
  padding: 12,
  background: "rgba(0, 0, 0, 0.03)",
  border: "1px solid rgba(0, 0, 0, 0.06)",
  borderRadius: 6,
  maxHeight: 480,
  overflow: "auto",
  fontSize: 12,
  whiteSpace: "pre-wrap",
};

/** 非主报告降级体：文案引用 reportDegradeNote，raw 全文不摊平。 */
function RawFallbackBody({ payload }: { payload: ReportRawFallback }) {
  const note = reportDegradeNote(payload);
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Alert type="warning" showIcon message="降级展示（非统一结构报告）" description={note} />
      <Text type="secondary">
        {`raw 为文件 ${payload.name}.json 的完整原文（未展开、未改字段名）；模式标签：${payload.pattern}`}
      </Text>
      <pre style={preStyle}>{JSON.stringify(payload.raw, null, 2)}</pre>
    </Space>
  );
}

/** 主报告结构化体：4 键 Descriptions + notes 原文 + summary 展开 + samples 并集表。 */
function StructuredBody({ detail }: { detail: ReportDetail }) {
  // pattern 在 structured=true 分支不参与 dryNote 判定（仅降级文案使用）；
  // 此处传空串而非编造文件名当模式标签。
  const dryTag = dryNote({ structured: true, pattern: "", dry: detail.dry });
  const summaryRows: FlatKvRow[] = flattenKv(detail.summary);
  const sampleCols = sampleColumns(detail.samples);
  const sampleRows = detail.samples.map((sample, index) => ({ ...sample, __key: String(index) }));
  const extras = Object.fromEntries(
    Object.entries(detail).filter(([key]) => !KNOWN_REPORT_KEYS.includes(key)),
  );
  const extraKeys = Object.keys(extras);
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label="快照 sha（sha）">
          <Text code title={detail.sha}>
            {shortSha(detail.sha)}
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label="生成时间（created_at）">{detail.created_at}</Descriptions.Item>
        <Descriptions.Item label="运行模式（dry）">
          {dryTag !== null ? <Tag color="orange">{dryTag}</Tag> : dashOr(detail.dry)}
        </Descriptions.Item>
        <Descriptions.Item label="覆盖域（domains）">
          {detail.domains.length === 0 ? "—" : detail.domains.join(" / ")}
        </Descriptions.Item>
      </Descriptions>
      <Text strong>口径说明（notes 原文）</Text>
      <Paragraph style={{ margin: 0 }}>{detail.notes}</Paragraph>
      <Text strong>{`汇总（summary，展开 ${summaryRows.length} 行）`}</Text>
      <Table<FlatKvRow>
        rowKey="path"
        size="small"
        pagination={false}
        scroll={{ x: "max-content", y: 320 }}
        dataSource={summaryRows}
        locale={{ emptyText: "summary 为空（展开 0 行）" }}
        columns={[
          {
            title: "路径（JSON 键路径）",
            dataIndex: "path",
            render: (value: string) => <Text code>{value}</Text>,
          },
          { title: "值", dataIndex: "value" },
        ]}
      />
      <Text strong>{`样本明细（samples，${detail.samples.length} 条 × ${sampleCols.length} 列）`}</Text>
      <Table
        size="small"
        rowKey="__key"
        pagination={false}
        scroll={{ x: "max-content", y: 360 }}
        dataSource={sampleRows}
        locale={{ emptyText: "samples: []（原文为空）" }}
        columns={sampleCols.map((key) => ({
          title: key,
          dataIndex: key,
          key,
          ellipsis: true,
          render: (value: unknown) => cellText(value),
        }))}
      />
      {extraKeys.length > 0 && (
        <Collapse
          items={[
            {
              key: "extra",
              label: `契约外字段（${extraKeys.length} 键：${extraKeys.join(" / ")}）——runner.py 演进追加，原样 JSON`,
              children: <pre style={preStyle}>{JSON.stringify(extras, null, 2)}</pre>,
            },
          ]}
        />
      )}
    </Space>
  );
}

export default function ReportDrawer({ name, token, onClose }: Props) {
  const [state, setState] = useState<DrawerState>({ status: "loading" });

  useEffect(() => {
    if (name === null) {
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    getJson<ReportDetailPayload>(API.governanceReportDetail.replace("{name}", name), token)
      .then((payload) => {
        if (!cancelled) {
          setState({ status: "ok", payload });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({ status: "error", error });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [name, token]);

  return (
    <Drawer
      open={name !== null}
      onClose={onClose}
      width={880}
      title={`报告钻取：${name ?? ""}`}
      destroyOnClose
    >
      {state.status === "loading" && <Spin size="small" />}
      {state.status === "error" && <ErrorNote error={state.error} />}
      {state.status === "ok" &&
        name !== null &&
        (isRawFallback(state.payload) ? (
          <RawFallbackBody payload={state.payload} />
        ) : (
          <StructuredBody detail={state.payload} />
        ))}
    </Drawer>
  );
}

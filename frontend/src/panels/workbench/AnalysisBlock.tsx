/**
 * 归因视图（AnalysisBlock）：消费 ADR-0026 `/analyze` 的 17 键 `analysis` 投影。
 *
 * 设计边界（本阶段"纯前端消费"，见 dev-plan-analyze-frontend-consumption）：
 * - **零重算（N1）**：所有数字（totals/items/contribution_pct）一律渲染后端原字符串；
 *   贡献条的**宽度**只取后端 `contribution_pct` 的绝对值做视觉比例，不聚合、不再算，
 *   展示文本永远是后端原串。
 * - **不推断业务成因（N2 / 0026 边界）**：标题与脚注只做"两期变化贡献分解"的陈述，
 *   不出现解释性/成因性措辞。
 * - **安全裁剪**：失败步（blocked/error）后端已把 sql/columns/rows 裁空，本组件对
 *   `sql === null` 的步不渲染任何 SQL/数据，只如实标注终态与原因码。
 * - **三态诚实**：status ≠ ok 时优先渲染后端 `text` 稳定文案（unavailable/blocked/error），
 *   不伪造 totals/items。
 *
 * 复用既有件：`SqlPreview`（步 SQL）、`DataTable`（步数据）、`lib/honesty.truncationState`
 * （渲染上限）、`lib/sha.shortSha`（快照标注）。不引入图表管道（C 决定）。
 */
import { Alert, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";

import type { AnalysisItem, AnalysisPayload, AnalysisStep } from "../../api/types";
import Section from "../../components/Section";
import { truncationState } from "../../lib/honesty";
import { shortSha } from "../../lib/sha";

import DataTable from "./DataTable";
import SqlPreview from "./SqlPreview";

interface Props {
  analysis: AnalysisPayload;
}

/** 贡献条：宽度 = 后端 `contribution_pct` 绝对值（视觉比例，非重算），文本 = 后端原串。 */
function ContributionBar({ item }: { item: AnalysisItem }): JSX.Element {
  const pct = Number(item.contribution_pct);
  const width = Number.isFinite(pct) ? Math.min(100, Math.abs(pct)) : 0;
  const negative = Number(item.delta) < 0;
  return (
    <Space size={8} align="center">
      <div className="atlas-contrib-track">
        <div
          className={`atlas-contrib-fill ${
            negative ? "atlas-contrib-fill--neg" : "atlas-contrib-fill--pos"
          }`}
          style={{ width: `${width}%` }}
        />
      </div>
      {/* 显示后端原字符串，不做二次格式化/重新舍入（N1） */}
      <Typography.Text>{item.contribution_pct}%</Typography.Text>
    </Space>
  );
}

const ITEM_COLUMNS: ColumnsType<AnalysisItem> = [
  { title: "维度值", dataIndex: "value", key: "value" },
  { title: "基线", dataIndex: "baseline", key: "baseline" },
  { title: "本期", dataIndex: "current", key: "current" },
  { title: "Δ 变化", dataIndex: "delta", key: "delta" },
  {
    title: "贡献占比",
    key: "contribution",
    render: (_: unknown, item: AnalysisItem) => <ContributionBar item={item} />,
  },
];

/** 单个分析步骤：成功步展示 SQL + 数据表；失败步只标终态/原因码（不渲染 SQL/数据）。 */
function StepDetail({ step }: { step: AnalysisStep }): JSX.Element {
  if (step.sql === null) {
    return (
      <Typography.Text type="secondary">
        该步未通过校验或执行失败，SQL 与数据不出网（reason_code={step.reason_code ?? "未知"}）。
      </Typography.Text>
    );
  }
  const t = truncationState({ rowCount: step.rows.length, sql: step.sql });
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <SqlPreview sql={step.sql} title={`步骤 SQL（${step.role}，只读）`} />
      <DataTable columns={step.columns} rows={step.rows} renderedRows={t.renderedRows} />
    </Space>
  );
}

export default function AnalysisBlock({ analysis }: Props): JSX.Element {
  const { status } = analysis;
  const isOk = status === "ok" && analysis.totals !== null;

  return (
    <Section title="变化贡献分解" meta={`status=${status}`}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {/* headline：身份 + 两期区间 */}
        <Typography.Text>
          指标 <b>{analysis.metric}</b> × 维度 <b>{analysis.dimension ?? "（无）"}</b>：
          {analysis.baseline.value} → {analysis.current.value}（{analysis.baseline.granularity}）
          {analysis.snapshot_sha !== null
            ? ` · 快照 ${shortSha(analysis.snapshot_sha)}`
            : " · 未绑定快照"}
        </Typography.Text>

        {/* 非 ok 态：优先渲染后端稳定文案（不伪造数字） */}
        {!isOk && (
          <Alert
            type={status === "error" ? "error" : "warning"}
            showIcon
            message={`分析未完成（${status}）`}
            description={analysis.text ?? "（后端未提供说明文案）"}
          />
        )}

        {/* ok 态：总量摘要 + 贡献表 */}
        {isOk && analysis.totals !== null && (
          <Typography.Text>
            总量：基线 {analysis.totals.baseline} → 本期 {analysis.totals.current}，Δ{" "}
            <b>{analysis.totals.delta}</b>
          </Typography.Text>
        )}
        {isOk && analysis.items.length > 0 && (
          <Table<AnalysisItem>
            size="small"
            rowKey={(record) => record.value}
            pagination={false}
            columns={ITEM_COLUMNS}
            dataSource={analysis.items}
          />
        )}

        {/* steps：按后端角色序逐条（失败步只标终态） */}
        <Collapseish steps={analysis.steps} />
      </Space>
    </Section>
  );
}

/** steps 折叠区（标题含角色名，供三态测试定位；扁平条目而非卡中卡——层级噪音只留一层）。 */
function Collapseish({ steps }: { steps: AnalysisStep[] }): JSX.Element {
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Typography.Text strong>分析步骤（{steps.length}）</Typography.Text>
      <div className="atlas-steps">
        {steps.map((step, i) => (
          <div key={`${step.role}-${String(i)}`} className="atlas-step">
            <div className="atlas-step__head">
              <span>{step.role}</span>
              <Tag color={step.kind === "answer" ? "blue" : "red"}>{step.kind}</Tag>
              <Typography.Text type="secondary">{step.latency_ms}ms</Typography.Text>
            </div>
            <StepDetail step={step} />
          </div>
        ))}
      </div>
    </Space>
  );
}

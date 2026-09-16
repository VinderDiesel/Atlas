/**
 * 治理面板 P2 的唯一 Drawer：值域钻取（`/governance/values/{item}`）。
 *
 * 响应是**裸对象**（非 4 键信封，见 serving/governance.py `governance_value_detail`），
 * 直接以 `ValueDetail` 消费。skip_reason 的渲染义务与 values 子页同源
 * （§3.3 第 1 行：引用原文）——`valueSkipNote` 用 `values.length` 作 ValuesFacts
 * 的 values_count 适配（skipped 时端点返回 `values: []`，两处事实同源）。
 *
 * 16 键全部如实展示（不挑好看的键）：表格放标量，values/aliases 各一张子表，
 * note 是派生说明原文（Paragraph 不截断）。
 */
import { Alert, Descriptions, Drawer, Space, Spin, Table, Typography } from "antd";
import { useEffect, useState } from "react";

import { getJson } from "../../api/client";
import { API } from "../../api/endpoints";
import type { ValueDetail } from "../../api/types";
import ErrorNote from "../../components/ErrorNote";
import { valueSkipNote } from "../../lib/honesty";
import { shortSha } from "../../lib/sha";

import { dashOr } from "./GovernanceSection";

const { Text, Paragraph } = Typography;

type DrawerState =
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "ok"; detail: ValueDetail };

interface Props {
  /** `<model>.<field>`；null = 关闭（drawer 挂载态由父组件持有）。 */
  item: string | null;
  token: string;
  onClose: () => void;
}

interface ValueRow {
  value: string;
  count: number;
}

interface AliasRow {
  alias: string;
  canonical: string;
}

function DetailBody({ detail }: { detail: ValueDetail }) {
  const skipNote = valueSkipNote({
    status: detail.status,
    skip_reason: detail.skip_reason,
    values_count: detail.values.length,
  });
  const aliases: AliasRow[] = Object.entries(detail.aliases).map(([alias, canonical]) => ({
    alias,
    canonical,
  }));
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      {skipNote !== null && (
        <Alert type="warning" showIcon message="未采集值域（status=skipped）" description={skipNote} />
      )}
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label="模型 / 字段（model / field）">{`${detail.model} / ${detail.field}`}</Descriptions.Item>
        <Descriptions.Item label="状态（status）">{detail.status}</Descriptions.Item>
        <Descriptions.Item label="快照 sha（snapshot_sha）">
          <Text code title={detail.snapshot_sha}>
            {shortSha(detail.snapshot_sha)}
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label="生成时间（generated_at）">{detail.generated_at}</Descriptions.Item>
        <Descriptions.Item label="绑定数据集（bound_dataset）">
          {detail.bound_dataset}
        </Descriptions.Item>
        <Descriptions.Item label="来源列（source_table.source_column）">
          {`${detail.source_table}.${detail.source_column}`}
        </Descriptions.Item>
        <Descriptions.Item label="基数（distinct_count / row_count）">
          {`${detail.distinct_count} / ${detail.row_count}`}
        </Descriptions.Item>
        <Descriptions.Item label="空值数（null_count）">{detail.null_count}</Descriptions.Item>
        <Descriptions.Item label="高基数阈值（max_cardinality）">
          {detail.max_cardinality}
        </Descriptions.Item>
        <Descriptions.Item label="跳过原因（skip_reason）">
          {detail.skip_reason === null ? "—" : detail.skip_reason}
        </Descriptions.Item>
      </Descriptions>
      <Text strong>{`值清单（values，${detail.values.length} 条）`}</Text>
      <Table<ValueRow>
        rowKey="value"
        size="small"
        pagination={false}
        scroll={{ x: "max-content", y: 320 }}
        dataSource={detail.values}
        locale={{ emptyText: "values: []（原文为空；跳过条目见上方 skip_reason）" }}
        columns={[
          { title: "值", dataIndex: "value" },
          {
            title: "行数",
            dataIndex: "count",
            render: (count: number) => dashOr(count),
          },
        ]}
      />
      {aliases.length > 0 && (
        <>
          <Text strong>{`别名（aliases，${aliases.length} 条）`}</Text>
          <Table<AliasRow>
            rowKey="alias"
            size="small"
            pagination={false}
            scroll={{ x: "max-content", y: 240 }}
            dataSource={aliases}
            columns={[
              { title: "别名", dataIndex: "alias" },
              { title: "规范值", dataIndex: "canonical" },
            ]}
          />
        </>
      )}
      <Text strong>派生说明（note 原文）</Text>
      <Paragraph style={{ margin: 0 }}>{detail.note}</Paragraph>
    </Space>
  );
}

export default function ValuesDrawer({ item, token, onClose }: Props) {
  const [state, setState] = useState<DrawerState>({ status: "loading" });

  useEffect(() => {
    if (item === null) {
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    getJson<ValueDetail>(API.governanceValueDetail.replace("{item}", item), token)
      .then((detail) => {
        if (!cancelled) {
          setState({ status: "ok", detail });
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
  }, [item, token]);

  return (
    <Drawer
      open={item !== null}
      onClose={onClose}
      width={720}
      title={`值域钻取：${item ?? ""}`}
      destroyOnClose
    >
      {state.status === "loading" && <Spin size="small" />}
      {state.status === "error" && <ErrorNote error={state.error} />}
      {state.status === "ok" && item !== null && <DetailBody detail={state.detail} />}
    </Drawer>
  );
}

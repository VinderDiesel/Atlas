/**
 * 治理子页 6：行级策略（policies）——P2。
 *
 * 消费端点 6（`/governance/policies`，全域一次返回；顶栏 RoleSwitcher 的
 * 角色矩阵与此同源——`rolesForDomain` 从本响应过滤，前端不复制矩阵）。
 *
 * §3.3 第 5 行的渲染义务：`registered: false`（策略声明但未在 ROLE_DIRECTORY
 * 注册）的角色必须显示未注册文案原文（`ROLE_UNREGISTERED_NOTICE`，不可签发）
 * ——注册态由 `registered` 字段驱动，不硬编码角色名。
 *
 * `condition` 是模板原文（含 `{{ user.X }}` 占位符，不含渲染值）：以 `Text code`
 * 原样展示——前端不做渲染、求值或替换。
 */
import { Collapse, Space, Table, Tag, Typography } from "antd";
import type { ReactNode } from "react";

import type { PolicyItem, RoleItem } from "../../api/types";
import { ROLE_UNREGISTERED_NOTICE } from "../../state/role";

import { dashOr, SectionData, type CollectionState } from "./GovernanceSection";

const { Text } = Typography;

interface Props {
  state: CollectionState<PolicyItem>;
}

function claimTags(list: string[]): ReactNode {
  if (list.length === 0) {
    return "—";
  }
  return list.map((key) => <Tag key={key}>{key}</Tag>);
}

function RoleTable({ roles }: { roles: RoleItem[] }) {
  return (
    <Table<RoleItem>
      rowKey="name"
      size="small"
      pagination={false}
      scroll={{ x: "max-content" }}
      dataSource={roles}
      locale={{ emptyText: "（该策略未声明角色）" }}
      columns={[
        { title: "角色", dataIndex: "name", render: (value: string) => <Text code>{value}</Text> },
        {
          title: "注册态",
          dataIndex: "registered",
          render: (registered: boolean) =>
            registered ? (
              <Tag color="green">已注册</Tag>
            ) : (
              <Space direction="vertical" size={0}>
                <Tag color="red">未注册</Tag>
                <Text type="danger">{ROLE_UNREGISTERED_NOTICE}</Text>
              </Space>
            ),
        },
        {
          title: "condition 模板原文",
          dataIndex: "condition",
          render: (value: string | null) =>
            value === null ? dashOr(value) : <Text code>{value}</Text>,
        },
        {
          title: "required_claims",
          dataIndex: "required_claims",
          render: claimTags,
        },
        {
          title: "list_claims（sql_in 渲染）",
          dataIndex: "list_claims",
          render: claimTags,
        },
        {
          title: "说明",
          dataIndex: "description",
          render: (value: string | null) => dashOr(value),
        },
      ]}
    />
  );
}

export default function PoliciesSection({ state }: Props) {
  return (
    <SectionData
      title="行级策略"
      state={state}
      render={(items) => {
        if (items.length === 0) {
          return <Text type="secondary">（响应 items 为空——服务端未给出策略载荷）</Text>;
        }
        return (
          <Collapse
            defaultActiveKey={items.map((item, index) => item.name ?? `policy-${index}`)}
            items={items.map((item, index) => ({
              key: item.name ?? `policy-${index}`,
              label: (
                <Space wrap size={8}>
                  <Text strong>{dashOr(item.name)}</Text>
                  {item.declared_by_models.map((domain) => (
                    <Tag key={domain} color="blue">
                      {domain}
                    </Tag>
                  ))}
                  <Text type="secondary">
                    {`default_deny=${dashOr(item.default_deny)} · 角色 ${item.roles.length} 个`}
                  </Text>
                </Space>
              ),
              children: (
                <Space direction="vertical" size="small" style={{ width: "100%" }}>
                  <Text type="secondary">{`说明：${dashOr(item.description)}`}</Text>
                  <RoleTable roles={item.roles} />
                </Space>
              ),
            }))}
          />
        );
      }}
    />
  );
}

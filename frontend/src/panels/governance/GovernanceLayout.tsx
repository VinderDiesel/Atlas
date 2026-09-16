/**
 * 面板 4：治理页布局（`/governance/:section`，8 子页）——P2 前 6 子页，
 * P3 补 reports、snapshots 两子页后达终态。
 *
 * 「挂载即发 8 条并发」（ADR-0022 决策 ⑥ 限流 240/min 的推导前提就是终态
 * 8 条——不得为减请求数合并或懒加载）：models 全域、metrics/dimensions 带
 * 当前域、synonyms 带当前 locale、values、policies、reports、snapshots。
 * **子页切换不重拉**——Tabs 只做导航与内容切换，8 条装载态由本页持有。
 * 钻取（values/{item}、reports/{name}）由子页/Drawer 自取，不进这 8 条。
 *
 * 未认证态（token === null）不发展开请求的 401（2026-09-16 实测治理端点
 * 一律 Bearer）——整页替换为激活引导 + `make token` 命令；角色激活路径见
 * 顶栏 RoleSwitcher（首次必须粘贴，见该组件头注）。
 *
 * 域与 locale 是本页头部两个选择器：域是 App 单源（与顶栏角色清单、工作台
 * model 同一事实源）——切换重拉 metrics + dimensions 2 条；locale 是本页局部
 * 态（`_LOCALES = ("zh_cn", "en_us")`，serving/governance.py 枚举镜像）——
 * 切换重拉 synonyms 1 条。
 */
import { Alert, Divider, Select, Space, Tabs, Typography } from "antd";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { getJson } from "../../api/client";
import { API } from "../../api/endpoints";
import type {
  DimensionsItem,
  Envelope,
  MetricsItem,
  ModelDomain,
  ModelsItem,
  PolicyItem,
  ReportsItem,
  SnapshotsItem,
  SynonymsItem,
  ValuesItem,
} from "../../api/types";
import CopyBlock from "../../components/CopyBlock";
import { makeTokenCommand } from "../../state/role";

import DimensionsSection from "./DimensionsSection";
import type { CollectionState } from "./GovernanceSection";
import MetricsSection from "./MetricsSection";
import ModelsSection from "./ModelsSection";
import PoliciesSection from "./PoliciesSection";
import ReportsSection from "./ReportsSection";
import SnapshotsSection from "./SnapshotsSection";
import SynonymsSection from "./SynonymsSection";
import ValuesSection from "./ValuesSection";

const { Text } = Typography;

/** 8 子页的唯一清单（Tabs 顺序 = 设计页 §2 面板 4 顺序）。 */
const SECTIONS = [
  { key: "models", label: "语义模型（models）" },
  { key: "metrics", label: "指标（metrics）" },
  { key: "dimensions", label: "维度（dimensions）" },
  { key: "synonyms", label: "locale 词典（synonyms）" },
  { key: "values", label: "值域注册表（values）" },
  { key: "policies", label: "行级策略（policies）" },
  { key: "reports", label: "评测报告（reports）" },
  { key: "snapshots", label: "快照（snapshots）" },
] as const;

type SectionKey = (typeof SECTIONS)[number]["key"];

/** locale 枚举（serving/governance.py `_LOCALES` 的镜像；不做本地化推断）。 */
const LOCALES = ["zh_cn", "en_us"] as const;

function isSectionKey(value: string): value is SectionKey {
  return SECTIONS.some((item) => item.key === value);
}

const SECTION_LIST_TEXT = SECTIONS.map((item) => item.key).join(" / ");

function unknownSectionNote(section: string): string {
  return `未知子页 ${section}：治理面为 8 子页（${SECTION_LIST_TEXT}）。`;
}

/**
 * 单条集合的装载 hook（path/token 任一为空不发请求；两者变化即重拉）。
 *
 * token 为空返回恒 loading——调用方在未认证态整页替换为引导，不会渲染
 * 装载态（不发必 401 的请求是刻意的）。
 */
function useCollection<T>(path: string, token: string | null): CollectionState<T> {
  const [state, setState] = useState<CollectionState<T>>({ status: "loading" });
  useEffect(() => {
    if (token === null) {
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    getJson<Envelope<string, T>>(path, token)
      .then((envelope) => {
        if (!cancelled) {
          setState({ status: "ok", envelope });
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
  }, [path, token]);
  return state;
}

interface Props {
  /** 激活身份 token（App 内存态）；null = 未认证（整页显示激活引导）。 */
  token: string | null;
  /** 当前域（App 单源；与顶栏角色清单、工作台 model 同源）。 */
  domain: ModelDomain;
  onDomainChange: (domain: ModelDomain) => void;
}

export default function GovernanceLayout({ token, domain, onDomainChange }: Props) {
  const params = useParams<{ section: string }>();
  const navigate = useNavigate();
  const section = params.section ?? "models";
  const [locale, setLocale] = useState<string>("zh_cn");

  // 挂载即发 8 条（ADR-0022 决策 ⑥ 的终态口径；不得合并或懒加载）
  const models = useCollection<ModelsItem>(API.governanceModels, token);
  const metrics = useCollection<MetricsItem>(
    `${API.governanceMetrics}?model=${encodeURIComponent(domain)}`,
    token,
  );
  const dimensions = useCollection<DimensionsItem>(
    `${API.governanceDimensions}?model=${encodeURIComponent(domain)}`,
    token,
  );
  const synonyms = useCollection<SynonymsItem>(
    `${API.governanceSynonyms}?locale=${encodeURIComponent(locale)}`,
    token,
  );
  const values = useCollection<ValuesItem>(API.governanceValues, token);
  const policies = useCollection<PolicyItem>(API.governancePolicies, token);
  const reports = useCollection<ReportsItem>(API.governanceReports, token);
  const snapshots = useCollection<SnapshotsItem>(API.governanceSnapshots, token);

  // 域选项：models 响应的 domain 去重（唯一事实源），union 当前域兜底
  const domainOptions = useMemo(() => {
    const known = models.status === "ok" ? models.envelope.items.map((item) => item.domain) : [];
    const union = new Set<string>([...known, domain]);
    return [...union].map((value) => ({ value, label: value }));
  }, [models, domain]);

  if (!isSectionKey(section)) {
    return (
      <Alert
        type="info"
        showIcon
        message={`子页 ${section} 未开放`}
        description={unknownSectionNote(section)}
      />
    );
  }

  if (token === null) {
    return (
      <Alert
        type="warning"
        showIcon
        message="未认证：治理端点一律要求 Bearer"
        description={
          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            <Text>
              2026-09-16 实测：未带 token 请求 /api/v1/governance/policies 返回 401——治理面 8
              条集合与 2 条钻取均需先激活身份。
            </Text>
            <Text>
              激活路径：顶栏「角色」切换器 → 用下方命令签发 token 后粘贴（首次必须粘贴；已认证后可在面板内就地签发，仅 make ui-dev 提供 dev
              签发）。
            </Text>
            <CopyBlock command={makeTokenCommand("hq_admin", {})} />
          </Space>
        }
      />
    );
  }

  const sectionChildren: Record<SectionKey, ReactNode> = {
    models: <ModelsSection state={models} />,
    metrics: <MetricsSection state={metrics} />,
    dimensions: <DimensionsSection state={dimensions} />,
    synonyms: <SynonymsSection state={synonyms} />,
    values: <ValuesSection state={values} token={token} />,
    policies: <PoliciesSection state={policies} />,
    reports: <ReportsSection state={reports} token={token} />,
    snapshots: <SnapshotsSection state={snapshots} />,
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Space wrap align="center" size={8}>
        <Text strong>域（model）</Text>
        <Select
          size="small"
          style={{ width: 110 }}
          value={domain}
          options={domainOptions}
          onChange={(next) => {
            if (next === "finance" || next === "retail") {
              onDomainChange(next);
            }
          }}
        />
        <Text type="secondary">切换重拉 metrics + dimensions（2 条）</Text>
        <Divider type="vertical" />
        <Text strong>locale</Text>
        <Select
          size="small"
          style={{ width: 110 }}
          value={locale}
          options={LOCALES.map((value) => ({ value, label: value }))}
          onChange={setLocale}
        />
        <Text type="secondary">切换重拉 synonyms（1 条）</Text>
      </Space>
      <Text type="secondary">
        本页挂载即并发请求 8 条治理集合（models / metrics / dimensions / synonyms / values /
        policies / reports / snapshots）；子页切换不重拉。域与顶栏角色清单、工作台 model
        是同一事实源。
      </Text>
      <Tabs
        activeKey={section}
        onChange={(key) => navigate(`/governance/${key}`)}
        items={SECTIONS.map(({ key, label }) => ({
          key,
          label,
          children: sectionChildren[key],
        }))}
      />
    </Space>
  );
}

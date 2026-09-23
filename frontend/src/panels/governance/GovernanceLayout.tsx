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
 * 发布身份区块（PublishIdentitySection；ADR-0031 D04「governance 显示发布
 * 身份」）不占这 8 条：`GET /manage/deployments` 由用户显式点击加载——挂载
 * 即发会破坏上述限流推导前提（240/min 的成立条件），需重推 ADR。
 * 未认证态（token === null）不发展开请求的 401（2026-09-16 实测治理端点
 * 一律 Bearer）——整页替换为激活引导 + `make token` 命令；角色激活路径见
 * 顶栏 RoleSwitcher（首次必须粘贴，见该组件头注）。
 *
 * 域与 locale 是本页头部两个选择器：域是 App 单源（与顶栏角色清单、工作台
 * model 同一事实源）——切换重拉 metrics + dimensions 2 条；locale 是本页局部
 * 态（`_LOCALES = ("zh_cn", "en_us")`，serving/governance.py 枚举镜像）——
 * 切换重拉 synonyms 1 条。
 *
 * **只读边界（ADR-0028 决策 ③）**：治理面 8 子页 + 2 钻取 + 发布身份区块一律只读——所有数据
 * 经 `getJson` 取回，无 PUT/POST/写控件/editable 属性（`governance-readonly.test.ts`
 * 静态断言守线）。语义定义的编辑只可能经 ADR-0027 proposal 链路（提交候选 →
 * 人工 + CI 落 Git），绝不直写；`semantic/` 的 Git 唯一事实源地位（ADR-0002）
 * 与 N8 同名指标唯一性由此守住。
 */
import { Alert, Collapse, Divider, Select, Space, Tabs, Typography } from "antd";
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
import EmptyState from "../../components/EmptyState";
import PageHeader from "../../components/PageHeader";
import { makeTokenCommand } from "../../state/role";

import DimensionsSection from "./DimensionsSection";
import type { CollectionState } from "./GovernanceSection";
import MetricsSection from "./MetricsSection";
import ModelsSection from "./ModelsSection";
import PoliciesSection from "./PoliciesSection";
import PublishIdentitySection from "./PublishIdentitySection";
import ReportsSection from "./ReportsSection";
import SnapshotsSection from "./SnapshotsSection";
import SynonymsSection from "./SynonymsSection";
import ValuesSection from "./ValuesSection";

const { Text } = Typography;

/**
 * 8 子页的唯一清单（Tabs 顺序 = 设计页 §2 面板 4 顺序）。
 * 导出为 GOV_SECTIONS 供结构断言测试（governance-grouping.test.ts）消费。
 * group 字段划入 build（语义构建面 5 页）/ control（治理控制面 3 页）两组
 * （ADR-0028 决策 ①）；8 key 不变，旧路由仍可解析。
 */
export const GOV_SECTIONS = [
  { key: "models", label: "语义模型", group: "build" as const },
  { key: "metrics", label: "指标", group: "build" as const },
  { key: "dimensions", label: "维度", group: "build" as const },
  { key: "synonyms", label: "同义词", group: "build" as const },
  { key: "values", label: "值域", group: "build" as const },
  { key: "policies", label: "权限策略", group: "control" as const },
  { key: "reports", label: "评测报告", group: "control" as const },
  { key: "snapshots", label: "数据快照", group: "control" as const },
] as const;

type SectionKey = (typeof GOV_SECTIONS)[number]["key"];

/** locale 枚举（serving/governance.py `_LOCALES` 的镜像；不做本地化推断）。 */
const LOCALES = ["zh_cn", "en_us"] as const;

function isSectionKey(value: string): value is SectionKey {
  return GOV_SECTIONS.some((item) => item.key === value);
}

const SECTION_LIST_TEXT = GOV_SECTIONS.map((item) => item.key).join(" / ");

function unknownSectionNote(section: string): string {
  return `未知子页 ${section}：治理面为 8 子页（${SECTION_LIST_TEXT}）。`;
}

const PAGE_HEADER = (
  <PageHeader
    title="治理"
    description="管理指标、维度、词典和权限策略。"
  />
);

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
      <div className="atlas-page">
        {PAGE_HEADER}
        <Alert
          type="info"
          showIcon
          message={`子页 ${section} 未开放`}
          description={unknownSectionNote(section)}
        />
      </div>
    );
  }

  if (token === null) {
    return (
      <div className="atlas-page">
        {PAGE_HEADER}
        <EmptyState
          icon="lock"
          title="尚未登录"
          description={
            <Space direction="vertical" size="small" style={{ width: "100%", maxWidth: 400 }}>
              <Text>
                治理页面需要登录后才能访问。请先在右上角选择角色，或使用以下命令签发 token 后粘贴。
              </Text>
              <CopyBlock command={makeTokenCommand("hq_admin", {})} />
            </Space>
          }
        />
      </div>
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
    <div className="atlas-page">
      {PAGE_HEADER}

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
        <Divider type="vertical" />
        <Text strong>locale</Text>
        <Select
          size="small"
          style={{ width: 110 }}
          value={locale}
          options={LOCALES.map((value) => ({ value, label: value }))}
          onChange={setLocale}
        />
      </Space>

      <Collapse
        ghost
        size="small"
        className="atlas-rules"
        items={[
          {
            key: "rules",
            label: "本页请求与只读边界（8 条挂载口径）",
            children: (
              <div className="atlas-rules__body">
                <span>
                  本页挂载即并发请求 8 条治理集合（models / metrics / dimensions / synonyms /
                  values / policies / reports / snapshots）；子页切换不重拉。域与顶栏角色清单、
                  工作台 model 是同一事实源；切换域重拉 metrics + dimensions（2 条），切换
                  locale 重拉 synonyms（1 条）。
                </span>
                <span>
                  治理面一律只读（ADR-0028 决策 ③）：所有数据经 getJson 取回，无写控件；
                  语义定义编辑只经 proposal 链路（ADR-0027），不直写。
                </span>
              </div>
            ),
          },
        ]}
      />

      <PublishIdentitySection token={token} />
      <Tabs
        activeKey={section}
        onChange={(key) => navigate(`/governance/${key}`)}
        items={GOV_SECTIONS.filter((s) => s.group === "build").map(({ key, label }) => ({
          key,
          label,
          children: sectionChildren[key],
        }))}
      />
      <Divider orientation="left" plain>
        管控
      </Divider>
      <Tabs
        activeKey={section}
        onChange={(key) => navigate(`/governance/${key}`)}
        items={GOV_SECTIONS.filter((s) => s.group === "control").map(({ key, label }) => ({
          key,
          label,
          children: sectionChildren[key],
        }))}
      />
    </div>
  );
}

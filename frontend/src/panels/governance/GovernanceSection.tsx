/**
 * 治理面板共用件（P2）：集合装载态、章节外壳（页头 + sources 展示）、装载分支。
 *
 * §3.2 的 sources 渲染义务：每个治理子页的页头必须显示「数据来自 N 个 Git 文件」
 * 并**可展开路径列表**——不得只在 hover tooltip 里给（0022 决策 ⑤ 的理由：
 * 治理面的诚实性要求数据可溯源到唯一事实源，而不是「服务端说的」）。
 */
import { Button, Space, Spin, Typography } from "antd";
import { useState, type ReactNode } from "react";

import type { Envelope } from "../../api/types";
import ErrorNote from "../../components/ErrorNote";

const { Text } = Typography;

/** 单条集合的装载态（由 GovernanceLayout 逐条持有；钻取由各自子页/组件自取）。 */
export type CollectionState<T> =
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "ok"; envelope: Envelope<string, T> };

/** null/undefined → 「—」；其余如实 `String()`（0 显示 0，不误显示为缺失）。 */
export function dashOr(value: unknown): string {
  return value === null || value === undefined ? "—" : String(value);
}

/** 字节数展示（B/KB/MB 一位小数；只换单位不改数值语义）。 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const kb = bytes / 1024;
  return kb < 1024 ? `${kb.toFixed(1)} KB` : `${(kb / 1024).toFixed(1)} MB`;
}

interface ShellProps {
  title: string;
  count: number | null;
  sources: string[] | null;
  children: ReactNode;
}

/**
 * 来源路径列表（sources 展开区唯一渲染出口）：星图视觉——每条来源一个
 * 星体节点 + 连接线，路径**原文**展示（Git 事实源，不缩写、不翻译）。
 */
export function SourceList({ sources }: { sources: string[] }) {
  return (
    <ul className="atlas-source-list">
      {sources.map((path) => (
        <li key={path} className="atlas-source-list__item">
          <span className="atlas-source-list__node" aria-hidden="true" />
          <Text code>{path}</Text>
        </li>
      ))}
    </ul>
  );
}

/** 章节外壳：标题 + 条数 + 「数据来自 N 个 Git 文件」（可展开 → 路径列表）。 */
export function SectionShell({ title, count, sources, children }: ShellProps) {
  const [showSources, setShowSources] = useState(false);
  return (
    <section className="atlas-section" aria-label={title}>
      <div className="atlas-section__head">
        <h2 className="atlas-section__title">{title}</h2>
        {count !== null && (
          <span className="atlas-section__meta">
            {`共 `}
            <span className="atlas-mono">{count}</span>
            {` 条`}
          </span>
        )}
        {sources !== null && (
          <span className="atlas-section__meta">
            {`数据来自 `}
            <span className="atlas-mono">{sources.length}</span>
            {` 个 Git 文件`}
          </span>
        )}
        {sources !== null && (
          <Button type="link" size="small" onClick={() => setShowSources((open) => !open)}>
            {showSources ? "收起来源" : "展开来源"}
          </Button>
        )}
      </div>
      <div className="atlas-section__body">
        {showSources && sources !== null && <SourceList sources={sources} />}
        {children}
      </div>
    </section>
  );
}

interface DataProps<T> {
  title: string;
  state: CollectionState<T>;
  render: (items: T[], envelope: Envelope<string, T>) => ReactNode;
}

/** 装载分支：loading → Spin；error → ErrorNote（原文透传）；ok → SectionShell + render。 */
export function SectionData<T>({ title, state, render }: DataProps<T>) {
  if (state.status === "loading") {
    return (
      <SectionShell title={title} count={null} sources={null}>
        <Spin size="small" />
      </SectionShell>
    );
  }
  if (state.status === "error") {
    return (
      <SectionShell title={title} count={null} sources={null}>
        <ErrorNote error={state.error} />
      </SectionShell>
    );
  }
  return (
    <SectionShell title={title} count={state.envelope.count} sources={state.envelope.sources}>
      {render(state.envelope.items, state.envelope)}
    </SectionShell>
  );
}

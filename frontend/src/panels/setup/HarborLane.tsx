/**
 * 数据接入港：航道导航（Harbor Lane）——Phase 2c。
 *
 * 星图仪表语言：6 个泊位（berth）沿航道排布，替换 AntD Steps：
 * - done：绿（已通过该泊位）；active：青 + 光晕（当前泊位）；wait：灰（待办理）
 * - 节点编号 01–06 等宽；点击泊位跳步（与旧 `Steps onChange` 同语义）
 * - SSR 初始态零请求（纯展示组件，不取数）
 */
export interface PipelineStep {
  title: string;
  description: string;
}

/** 6 步流水线定义（自 SetupPipeline 迁入；标题唯一，作 key 用）。 */
export const PIPELINE_STEPS: PipelineStep[] = [
  { title: "连接数据源", description: "登记 Doris 只读源" },
  { title: "部署绑定", description: "创建部署并绑定源" },
  { title: "创建草稿", description: "编辑语义模型内容" },
  { title: "校验与审核", description: "确定性校验 + 人工审核" },
  { title: "导入制品", description: "从 Git commit 导入" },
  { title: "发布激活", description: "CAS 切换活动发布" },
];

type BerthState = "done" | "active" | "wait";

function berthState(index: number, current: number): BerthState {
  if (index < current) return "done";
  if (index === current) return "active";
  return "wait";
}

interface Props {
  /** 当前泊位序号（0 起）。 */
  current: number;
  /** 点击泊位跳步（与旧 Steps onChange 同语义）。 */
  onSelect: (index: number) => void;
}

export default function HarborLane({ current, onSelect }: Props) {
  return (
    <ol className="atlas-harbor-lane" aria-label="接入航道">
      {PIPELINE_STEPS.map((step, index) => {
        const state = berthState(index, current);
        return (
          <li
            key={step.title}
            className={`atlas-harbor-lane__berth atlas-harbor-lane__berth--${state}`}
          >
            <button
              type="button"
              className="atlas-harbor-lane__button"
              onClick={() => onSelect(index)}
              aria-current={index === current ? "step" : undefined}
            >
              <span className="atlas-harbor-lane__dot atlas-mono">{`${String(index + 1).padStart(2, "0")}`}</span>
              <span className="atlas-harbor-lane__title">{step.title}</span>
              <span className="atlas-harbor-lane__desc">{step.description}</span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

/**
 * 页面头（H1 页名 + 一行页面说明）——层级体系的最上层，一屏一个。
 *
 * 语义用原生 <h1>（Typography.Title 的 cssinjs 字号/边距需 !important 对抗，
 * 骨架层用类名直控更干净）；视觉规格见 styles/global.css 的
 * .atlas-page-header__title（20/600）。
 */
import type { ReactNode } from "react";

interface Props {
  /** 页名（与导航项同名，用户在这里确认"我在哪"）。 */
  title: string;
  /** 一行页面说明（可选；细节属于各 Section，不在这里展开）。 */
  description?: ReactNode;
}

export default function PageHeader({ title, description }: Props) {
  return (
    <header className="atlas-page-header">
      <h1 className="atlas-page-header__title">{title}</h1>
      {description !== undefined && <p className="atlas-page-header__desc">{description}</p>}
    </header>
  );
}

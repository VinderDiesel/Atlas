/**
 * 区块（Section）——页面骨架的中间层：标题行（H2 + 可选说明）+ 内容区。
 * 层级规格见 styles/global.css 的 .atlas-section__*（15/600）。
 */
import type { ReactNode } from "react";

interface Props {
  /** 区块标题（一句话说明这块是什么，如「源目录」）。 */
  title: string;
  /** 标题行补充说明（可选，12px 次级色，如「每源最新修订」）。 */
  meta?: string;
  children: ReactNode;
}

export default function Section({ title, meta, children }: Props) {
  return (
    <section className="atlas-section" aria-label={title}>
      <div className="atlas-section__head">
        <h2 className="atlas-section__title">{title}</h2>
        {meta !== undefined && <span className="atlas-section__meta">{meta}</span>}
      </div>
      <div className="atlas-section__body">{children}</div>
    </section>
  );
}

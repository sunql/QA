import { Collapse, Tag, Typography } from "antd";
import type { WikiCitation } from "../../types/wikiChat";

const { Paragraph } = Typography;

interface Props {
  citations: WikiCitation[];
}

const DIMENSION_LABELS: Record<string, string> = {
  RULE: "规则",
  PROCESS: "流程",
  CONCEPT: "概念",
  METRIC: "指标",
};

export function WikiChatCitationList({ citations }: Props) {
  if (!citations.length) return null;
  return (
    <Collapse
      className="wiki-chat-citation-list"
      size="small"
      // 默认全部收起：只显示 [n] + 标题 + 分数，正文点击展开（再点收起）
      items={citations.map((c) => ({
        key: c.id,
        label: (
          <span data-testid={`wiki-chat-citation-header-${c.id}`}>
            <span className="wiki-chat-citation-id">[{c.id}]</span>{" "}
            {c.title || c.pageId}
            {c.dimension && (
              <Tag style={{ marginLeft: 8 }}>
                {DIMENSION_LABELS[c.dimension] ?? c.dimension}
              </Tag>
            )}
            <span className="wiki-chat-citation-score" style={{ float: "right" }}>
              {(c.score * 100).toFixed(1)}%
            </span>
          </span>
        ),
        children: c.chunkText ? (
          <Paragraph style={{ marginBottom: 0, fontSize: 13 }}>
            {c.chunkText}
          </Paragraph>
        ) : null,
      }))}
      style={{ marginBottom: 8 }}
    />
  );
}

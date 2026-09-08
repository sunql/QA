import { Card, Typography } from "antd";
import type { DocQaCitation } from "../../types/document";

const { Paragraph } = Typography;

interface Props {
  citations: DocQaCitation[];
}

export function DocumentQaCitationList({ citations }: Props) {
  if (!citations.length) return null;
  return (
    <div className="doc-qa-citation-list">
      {citations.map((c) => (
        <Card
          key={c.id}
          size="small"
          title={
            <span>
              <span className="doc-qa-citation-id">[{c.id}]</span>{" "}
              {c.document_name}
            </span>
          }
          extra={<span className="doc-qa-citation-score">{(c.score * 100).toFixed(1)}%</span>}
          style={{ marginBottom: 8 }}
          data-citation-id={c.id}
        >
          <Paragraph ellipsis={{ rows: 4, expandable: true, symbol: "展开" }} style={{ marginBottom: 0, fontSize: 13 }}>
            {c.chunk_text}
          </Paragraph>
        </Card>
      ))}
    </div>
  );
}

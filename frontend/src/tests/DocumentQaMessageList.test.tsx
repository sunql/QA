import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DocumentQaMessageList } from "../components/documents/DocumentQaMessageList";

describe("DocumentQaMessageList citation parsing", () => {
  it("parses [n] markers into clickable citation refs", () => {
    const messages = [
      {
        role: "user" as const,
        content: "什么是质量协议？",
        citations: null,
      },
      {
        role: "assistant" as const,
        content: "根据合同条款[1]，质量协议[2]是...",
        citations: [
          { id: 1, document_id: "DOC-A", document_name: "合同", chunk_text: "...", score: 0.85 },
          { id: 2, document_id: "DOC-B", document_name: "指南", chunk_text: "...", score: 0.62 },
        ],
      },
    ];
    render(<DocumentQaMessageList messages={messages} loading={false} />);
    const refs = screen.getAllByTestId("doc-qa-citation-ref");
    expect(refs.length).toBe(2);
    expect(refs[0].getAttribute("data-cite")).toBe("1");
    expect(refs[1].getAttribute("data-cite")).toBe("2");
    // 引用卡片也渲染
    expect(screen.getByText("合同")).toBeTruthy();
    expect(screen.getByText("指南")).toBeTruthy();
  });

  it("non-citation text rendered normally", () => {
    const messages = [
      { role: "assistant" as const, content: "普通文本", citations: [] },
    ];
    render(<DocumentQaMessageList messages={messages} loading={false} />);
    expect(screen.getByText("普通文本")).toBeTruthy();
  });
});

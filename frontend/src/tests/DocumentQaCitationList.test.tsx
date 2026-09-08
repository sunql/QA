import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DocumentQaCitationList } from "../components/documents/DocumentQaCitationList";

describe("DocumentQaCitationList", () => {
  it("renders one card per citation with id and document_name", () => {
    const citations = [
      { id: 1, document_id: "DOC-A", document_name: "合同 V2", chunk_text: "条款...", score: 0.85 },
      { id: 2, document_id: "DOC-B", document_name: "指南", chunk_text: "段落...", score: 0.62 },
    ];
    render(<DocumentQaCitationList citations={citations} />);
    expect(screen.getByText("[1]")).toBeTruthy();
    expect(screen.getByText("[2]")).toBeTruthy();
    expect(screen.getByText("合同 V2")).toBeTruthy();
    expect(screen.getByText("指南")).toBeTruthy();
    expect(screen.getByText("85.0%")).toBeTruthy();
  });
});

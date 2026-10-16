/* P1-5 HypothesisPanel verificationStatus rendering tests (RED).
 *
 * Coverage:
 * - No verificationStatus -> pending tag
 * - verificationStatus=supported -> green tag
 * - verificationStatus=contradicted -> red tag
 * - verificationStatus=inconclusive -> orange tag
 * - verificationStatus=execution_failed -> default tag
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import HypothesisPanel from "../components/chat/HypothesisPanel";
import type { HypothesisView } from "../types/chat";

function makeHypothesis(overrides: Partial<HypothesisView> = {}): HypothesisView {
  return {
    id: Math.floor(Math.random() * 100000),
    statement: "purchase amount increased",
    driver: "AMT",
    verificationSql: "SELECT 1",
    turnQuestion: "why",
    createdTime: null,
    ...overrides,
  };
}

function renderPanel(hypotheses: HypothesisView[], onVerify = vi.fn()) {
  return render(<HypothesisPanel hypotheses={hypotheses} onVerify={onVerify} />);
}

describe("HypothesisPanel verificationStatus", () => {
  it("shows pending tag when not verified", () => {
    renderPanel([makeHypothesis()]);
    expect(screen.getByText(/pending/i)).toBeTruthy();
  });

  it("shows green supported tag when supported", () => {
    renderPanel([makeHypothesis({ verificationStatus: "supported" })]);
    expect(screen.getByText(/supported/i)).toBeTruthy();
  });

  it("shows red contradicted tag when contradicted", () => {
    renderPanel([makeHypothesis({ verificationStatus: "contradicted" })]);
    expect(screen.getByText(/contradicted/i)).toBeTruthy();
  });

  it("shows orange inconclusive tag when inconclusive", () => {
    renderPanel([makeHypothesis({ verificationStatus: "inconclusive" })]);
    expect(screen.getByText(/inconclusive/i)).toBeTruthy();
  });

  it("shows default tag when execution_failed", () => {
    renderPanel([makeHypothesis({ verificationStatus: "execution_failed" })]);
    expect(screen.getByText(/execution_failed/i)).toBeTruthy();
  });
});

/* P1-5 HypothesisPanel verificationStatus rendering tests.
 *
 * 断言真实文案，而不是「key 里恰好含英文枚举」——旧用例用 /pending/i 匹配，
 * 缺 i18n key 时 i18next 回落到 key 本身（chat.hypothesis.status.pending），
 * 用例照样通过，等于没有覆盖。
 *
 * Coverage:
 * - No verificationStatus -> pending tag
 * - verificationStatus=supported/contradicted/inconclusive/execution_failed -> 对应标签
 * - 未知状态 -> 回落 pending，且不把原始 key 渲染给用户
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

/** 缺 key 时 i18next 回落为 key 本身；渲染出任何 status key 都说明文案没接上。 */
function expectNoRawKey() {
  expect(screen.queryByText(/chat\.hypothesis\.status\./)).toBeNull();
}

describe("HypothesisPanel verificationStatus", () => {
  it("shows 待验证 tag when not verified", () => {
    renderPanel([makeHypothesis()]);
    expect(screen.getByText("待验证")).toBeTruthy();
    expectNoRawKey();
  });

  it("shows 支持 tag when supported", () => {
    renderPanel([makeHypothesis({ verificationStatus: "supported" })]);
    expect(screen.getByText("支持")).toBeTruthy();
    expectNoRawKey();
  });

  it("shows 相反 tag when contradicted", () => {
    renderPanel([makeHypothesis({ verificationStatus: "contradicted" })]);
    expect(screen.getByText("相反")).toBeTruthy();
    expectNoRawKey();
  });

  it("shows 无法判定 tag when inconclusive", () => {
    renderPanel([makeHypothesis({ verificationStatus: "inconclusive" })]);
    expect(screen.getByText("无法判定")).toBeTruthy();
    expectNoRawKey();
  });

  it("shows 执行失败 tag when execution_failed", () => {
    renderPanel([makeHypothesis({ verificationStatus: "execution_failed" })]);
    expect(screen.getByText("执行失败")).toBeTruthy();
    expectNoRawKey();
  });

  it("falls back to 待验证 for an unknown status without leaking the raw key", () => {
    renderPanel([makeHypothesis({ verificationStatus: "not_a_real_status" })]);
    expect(screen.getByText("待验证")).toBeTruthy();
    expectNoRawKey();
  });
});

import { test } from "@playwright/test";

// E2E 占位 — Task 10 在 N4 plan 实施时因 mockApi.ts 无 multi-step 帧支持而 downgrade.
// 待未来 plan 扩展 mockApi (加 step_plan/step_result 事件) 后回填此 spec.
//
// 参考:
// - docs/superpowers/specs/2026-10-17-n4-qwen-resume-design.md 末尾 "手动验证 checklist"
// - frontend/e2e/support/mockApi.ts:443 SSE 帧格式
// - frontend/src/stores/stream_chunk_parser.ts 帧解析

test.describe.skip("N4 Qwen 中断后跨模型续跑 (待 mockApi 多步支持后启用)", () => {
  test.skip("Qwen step 3 失败 → 选 deepseek → step 3 成功", () => {
    // 占位, 见 design spec 末尾 manual checklist
  });
});

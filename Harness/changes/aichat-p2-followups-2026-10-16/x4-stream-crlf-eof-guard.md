# X4 — 流式 CRLF + EOF 守门 ✅ PR #40

## 范围

承接 1012 cross-refs spec §P0-X4。Chat 流式 P1-3（b818910d）已修，
research 流式还在用旧 `consumeFrames` 重复发明轮子，本批把 research 改走
共享的 `StreamChunkParser`，与 chat 对齐。

## 落地

**Worktree**：`/private/tmp/qa-fix-x4-stream-crlf`
**Branch**：`fix/qa-1012-p0-x4`（base=main tip `ec0570b0`）
**Commit**：`db03dd7e` fix(stream): research SSE 切帧走共享 StreamChunkParser (X4/1012-P0)
**PR**：**#40** https://github.com/sunql/QA/pull/40

## 问题域

两类 bug（chat 已在 b818910d 修过，research 漏了）：

1. **跨 chunk 边界 CRLF 误切**：旧 `consumeFrames` 在拆帧前对整 buffer
   做 `replace(/\r\n|\r/g, "\n")`，把 data 内的 `\r` 抹成 `\n`。当 `\r`
   跨 chunk（chunk1 末尾停在 `\r`，chunk2 开头是 `\n`）时，`\r` 被提前
   归一，下一个 `\n` 形成虚假 `\n\n` 边界，把一帧切成两半 ⇒ JSON 残缺
   ⇒ JSON.parse 抛错 ⇒ 帧被丢弃。

2. **EOF 守门缺失**：旧 `consumeFrames` 在 reader done 时把残留 buffer
   当 remainder 返回，**不**强制切为最后一帧。后端以非 `\n\n` 结尾
   的最后一条事件（连接提前关）会被静默丢弃。

## 修法

- **`StreamChunkParser` 扩展**（`frontend/src/stores/stream_chunk_parser.ts`）：
  - 解析 `id: <seq>` 行（按 SSE 规范最后一条 id: 生效，空 / 非整数
    **重置**先前有效值）
  - `StreamFrame` 加可选 `id?: number` 字段
  - 处理 SSE 规范的「无字段前缀行 = data: <整行>」（跨 chunk 边界的 `}`
    这类续行按 SSE 拼接成 data，旧实现直接丢弃）
- **`research.ts` 重构**（`frontend/src/api/research.ts`）：
  - reader 循环接 `parser.feed` + `parser.flush` 强吐残留
  - `handleFrame` 收 `StreamFrame` 而非 raw frame string
  - 删除独立的 `parseFrameId` 函数（parser 接管）
  - 帧末尾 data 的 `\r`（CRLF 终止符残余）在 `handleFrame` 剥掉
    （**不**在 parser 剥，因为行内 `\r` 是 JSON 字符串内合法字符）

## 验证

- 73 个 research SSE 测试全绿
- 4 个 parser 测试全绿（含「行内 `\r` 保留原样」反向断言）

## 反向断言 / 防退化

- 新增「data 内的 `\r` 跨 chunk 边界仍保留」—— 若有人把 parser 还原
  成归一化模式，本测试立刻 RED。
- 新增「最后一帧无 `\n\n` 终止也照常分发（EOF 守门）」—— 若有人删了
  `parser.flush()` 调用，最后一帧丢失。
- 既有「行内 `\r` 保留原样」（parser 自己的 4 个测试之一）—— 若有人
  把 data 行末尾的 `\r` 也归一化（破坏 CRLF 但保留行内），老测试立刻
  RED。

## 副作用 / 兼容性

- `StreamFrame` 新加 `id?: number` 字段，**可选**。chat 忽略；research
  用之。
- `handleFrame` 签名变了：raw string → `StreamFrame`。research.ts 是
  唯一调用方。
- `parseFrameId` 函数删除（功能被 parser 接管）。
- `id:` 行解析**下沉到共享 parser**—— chat 后续若要支持 SSE 游标重连
  也可直接受益（chat 当前不读 `id` 字段，未受影响）。

## 测试限制

- chatStore 相关测试在本 worktree 因预存在 `} else { } else {` 编译错
  （PR #30 #36 同款修复未合本 worktree base）跑不起来，**不**由本改动
  引入；parser 接口扩展对 chat 透明（chat 不读 `id` 字段）。
- RequireAuth / chatApi / chatStoreHydration 等失败是 X2 / P1-3 等
  未合 worktree base 的预存 rot，与 X4 无关。

## 链接

- Spec：`docs/superpowers/specs/2026-10-13-1012-code-review-cross-refs.md` §P0-X4
- 母 SSOT：`Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md` 序 3
- 平行项：X1（PR #37）/ X2（PR #38）/ X3（PR #39）/ X4（PR #40）
- 下一项：X5 会话归属 + 状态互斥

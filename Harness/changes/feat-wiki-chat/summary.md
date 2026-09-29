# 变更：feat-wiki-chat（企业 Wiki 下的 Wiki Chat 语义知识库对话工具）

- **日期**：2026-09-16
- **Phase**：feature（wiki 知识消费入口：对话问答，提问 + 给建议，不是检索列表）
- **状态**：done（后端已部署 + 真机端到端验证；前端已 compose build + bundle probe 通过）
- **触发**：用户要求在企业 wiki 下新增「Wiki Chat」菜单——语义知识库对话，提问、给建议，作为知识库对话工具，不要只检索
- **计划文件**：~/.claude/plans/deep-giggling-frost.md（已批准，本次覆盖写入）
- **MEMORY**：qa-system-wiki-chat.md

---

## 1. 需求

企业 Wiki 已有语义检索（feat-wiki-semantic-search）但只返回命中列表。用户要求：
对话式的知识库工具——提问、给建议，「不要只检索」。

## 3. 核心决策：镜像 doc_qa 全链路

`rag_qa_service.py`（doc_qa）已是完整模式：语义检索 chunks → 编号 citations →
SSE 流式 LLM → session_message 持久化（citations JSONB）。
Wiki Chat = 同一模式 + wiki 向量源（`WikiVectorService.searchSemantic`）+
wiki 专属 prompt + `channel="wiki_qa"`。

## 4. 接口契约变更

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/v1/wiki/chat` | SSE 流式问答。body：`sessionId/question/topK(1-20 默认 8)/dimension?/modelId?`；事件：`qa_meta`→`qa_citations`→`token`×N→`qa_done`；错误走 `error` 事件（errorType LLM/DOMAIN/INTERNAL），SSE 已开始后不再映射 HTTP 错误码 |
| （复用） | `GET /sessions/chat-history?channel=wiki_qa` | 会话历史面板 |

数据：无 alembic 迁移。复用 `session_message`（channel=wiki_qa）+ `wiki_token_usage`（mechanism 新增 **QA**）。

## 5. 实现要点

- **wiki_qa_prompt.py**（新）：system prompt 规则含「适用时给出可执行的建议（『建议：』开头条目）」——「给建议」由 prompt 承载；无相关信息时明确说明不编造
- **wiki_qa_service.py**（新，镜像 RagQaService）：
  - `load_history` 只取 channel=wiki_qa 最近 5 轮；检索源 `WikiVectorService.searchSemantic`（embedding 计量已内置）
  - 短路：无命中或 top1 score<0.3 → 固定模板，零 LLM（但仍落库，历史面板可见）
  - 选模型：modelId 优先（校验 active）→ `ModelRouterService.selectModel`
  - **LLM 计量（核心约束 #3）**：`WikiTokenUsageService.record(mechanism="QA", purpose="wiki_chat_answer")`，与 persist 同事务。**有意优于 doc_qa**（doc_qa 只在 done 事件报数不落台账）
  - **修正 rag_qa 的 prod bug**：RagQaService 的 `llm_factory` 缺省 None → prod 直跑 `RuntimeError("llm_factory 未配置")`（真机复现）。Wiki 版缺省走 `createClient`，client 为 None 抛 `LLMUnavailableError`（DOMAIN）
- **wiki.py POST /chat**：`@limiter.limit` + ownership 守卫（session 已有 wiki_qa 行且 user_id 不匹配 → 422）+ eventSource 兜底 `Exception`（doc_qa 漏了兜底，SSE 已开始后未预期异常直接断连；wiki 版补 INTERNAL 事件 + logger.exception）
- **菜单 seed**：`item.wikiChat`（icon=message，sort_order=245，path=/wiki-chat）——企业 Wiki 组内第一位（知识消费入口在管理面板之前）；seed 是 on_conflict_do_nothing，prod 需手动跑 `docker exec qa-backend python scripts/seed_menu_config.py`
- **前端**：独立 `wikiChatStore`（不复用全局 chatStore——chat/doc_qa 共用 store 已有状态互染教训）；SSE 解析照 api/document.ts fetch+ReadableStream 模式；复用 props 驱动的 `DocumentQaHistoryPanel`；空会话预置建议问题 chips；引用卡片 [n] + title + score% + chunkText，正文 [n] 上标点击滚动；citations 编号/正文高亮色用主题 teal #00D9C0
- **RBAC**：admin 免授权可见；普通用户需经 RBAC 授权 `item.wikiChat`

## 6. 测试

| 套件 | 结果 |
|---|---|
| unit `test_wiki_qa_service.py` 13 用例（事件序列/短路零 LLM/citations 编号/prompt 拼装/历史注入/persist/QA 计量/停用模型拒绝/缺省 factory） | ✅ 13/13 |
| integration `test_wiki_api.py` 38 用例（+4：空白 422/SSE 透传/VectorError→error 事件/ownership 422） | ✅ 38/38 |
| menu 集成套件 25 用例（seed count 43→45、routes 集合 +wiki-chat +DQ 两项——**HEAD 预存漂移一并修正**） | ✅ 25/25 |
| 前端 vitest wikiChatApi/wikiChatStore 9 用例 | ✅ 9/9；tsc 0 |
| 全量单测/前端全量 | 40 后端单测 + 57 前端用例失败——**经 git stash 对照 HEAD 基线逐组确认全部预存**（menu seed 计数漂移、rag_qa persist commit mock 缺失、supplier_risk FeatureRuleRegistry warmUp 等），与本特性无关 |

## 8. 部署验证

```bash
./scripts/deploy_backend.sh
docker exec qa-backend python scripts/seed_menu_config.py     # 45 rows upserted
docker compose build --no-cache --build-arg NPM_REGISTRY=https://registry.npmmirror.com frontend
docker compose up -d frontend                                  # bundle grep wiki-chat = 2
```

真机端到端（modelId=1 deepseek-chat，session smoke-wc-4）：
- SSE：1 meta + 1 citations(8 条) + 495 token + 1 done
- `wiki_token_usage`：id=677，mechanism=**QA**，deepseek-chat，3197+495 tokens，cost=0.005862，purpose=wiki_chat_answer
- `session_message`：user+assistant 双行 channel=wiki_qa，citations=8
- 回答正文带 [1][2] 编号引用 + 「建议：」三条可执行建议
- 无关问题（「量子纠缠在奶茶店的应用」）：LLM 按 prompt 规则诚实说明未找到 + 给建议（top1 score 0.47 超阈值放行，prompt 规则 3 兜底）
- 无 modelId：router 选无 key 的 Qwen → createClient None → DOMAIN error 事件「未配置可用的 LLM」（与 [[qa-system-chat-llm-router-keyless-500]] 同根因；前端显示 error 事件内容，不断连）

## 7.1 后续补齐（2026-09-16 同日晚，用户反馈驱动）

1. **模型选择框（照 AIChatService/ChatPanel 语义）**：WikiChatInput 顶部加模型
   Select（自动路由 = -1 ↔ store null；options = `GET /models?activeOnly=true`，
   显示 `modelName (provider)`）；store 加 `selectedModelId` 并随 send 传
   `modelId`；选中模型被禁用时自动切回自动路由（照 ChatPanel 守卫）。
   之前「未配置可用的 LLM」报错的直接解法：用户手动选 deepseek-chat（id=1）。
   根因仍是 router 按 weight 选中无 key 的本地 Qwen（见元教训 4）。
2. **doc_qa prod bug 同步修复**：`RagQaService` 缺省 factory 改走 `createClient`，
   client None → `LLMUnavailableError`（DOMAIN）——与 wiki 版同款修正；
   顺手补 rag_qa 测试缺失的 `session.commit = AsyncMock()`（该文件 4 个预存红测
   因此转绿，7/7）。生产未部署改动仅影响 doc_qa 行为（此前必炸 → 现在给可读错误）。
3. 前端测试 +2（modelId 透传 / auto-route 省略）→ wikiChat store/api 共 11 用例；
   前端已重建部署（bundle probe setSelectedModelId=2）。

3. **历史面板 422（2026-09-16 晚）**：`GET /sessions/chat-history` 的 channel Query
   校验 `pattern="^(chat|doc_qa)$"` 拦掉 wiki_qa → 前端 422（store 静默吞错，
   只有 console 可见）。session.py 放行 `wiki_qa` + 集成 +2（channel 过滤/
   非法值 422），15/15 绿，已部署。**教训：给 channel 枚举加值要同时改两处——
   前端类型联合只是编译期，后端 Query pattern 才是运行时守门员。**

4. **UI 优化三连（2026-09-16 晚，用户反馈）**：①助手内容字号 14→15；
   ②引用改 antd Collapse（默认收起只显示 [n]+标题+分数，点击展开正文再点收起，
   比原 Card+ellipsis 省空间；测试断言用 `.ant-collapse-item-active` 计数——
   Collapse 收起后 children 仍留在 DOM 只是隐藏，queryByText 会误判）；
   ③历史面板显示文案 title→lastQuestion→sessionId 切片三级回退（doc_qa 同受益，
   ChatSession.lastQuestion 本就在 payload 里）。已重建部署（bundle probe 通过）。

5. **历史会话删除（2026-09-16 晚）**：HistoryPanel 每项加垃圾桶按钮（可选
   `onDelete` prop——不传不渲染，doc_qa 不受影响），Popconfirm 二次确认
   （提示不可恢复），store.deleteSession 照 chatStore 语义：调既有
   `DELETE /sessions/{id}`（message+token_usage+query_state 三表同删），
   不可变移除；删的是当前会话则清空消息并换新 sessionId。
   测试坑：垃圾桶 aria-label 与 Popconfirm 确认按钮文案都叫「删除」，
   getByRole 撞车——用 `.ant-popconfirm-buttons .ant-btn-primary` 定位。
   已重建部署（bundle probe 通过）。

## 9. 关联与元教训

- 关联：[feat-wiki-semantic-search](../feat-wiki-semantic-search/summary.md)（检索底座）、qa-system-chat-llm-router-keyless-500（keyless 模型同坑）、qa-system-menu-config-seed（菜单 DB 驱动）

### 元教训

1. **参照实现也有 prod bug**：镜像模式前先在真机跑一遍参照——RagQaService 的 `llm_factory` 缺省 None 在 prod 必炸（doc_qa 同病），照抄会继承 bug。
2. **SSE 端点的错误契约**：StreamingResponse 开始后 HTTP 错误码已不可用，所有异常必须转 error 事件；且要兜底 `Exception`（ref 实现漏了，未预期异常直接裸断连）。
3. **Docker build 是最后一道 tsc 关**：本地 `npx tsc --noEmit` 通过 ≠ 容器内 `npm run build` 通过（测试文件也在检查范围、tsconfig 覆盖面不同）——mock 对象字面量传入窄化回调类型会炸。
4. **后台 Bash 的管道会吞日志**：`build | tail -3` 后台跑，真实失败被 tail 掩盖且 exit 0；长构建重定向到文件再 grep。
5. **全量测试失败先 stash 对照基线**：40 个后端单测失败逐组与 HEAD 对照后确认全是预存漂移（menu 计数、rag_qa commit mock 等），避免误修别人的债。

# Agent（后端智能层）说明

> 记录 PetPal「宠伴」后端 Agent 层的整体架构：双模式、agent 环、上下文派生、
> 工具调用、压缩、记忆注入与事件契约。实现对照见 `backend/` 各模块；本文档是
> 阅读/排查的入口。

---

## 0. 一句话定位

后端 Agent = **会话层（存真源）→ 上下文层（派生出送模型视图）→ 压缩层（超预算摘要）→
记忆层（v2 扁平文件）** 的完整推理链路，由 `agent_runtime.run_agent_loop` 驱动。

前端只负责采集语音/播放回复；所有"想"的动作都发生在这一链路里。

---

## 1. 双模式（闲聊 / 工作）

| 模式 | 常量 | 定位 | 工具白名单 | 完整保留轮 | 轮数压缩触发 |
|---|---|---|---|---|---|
| 闲聊 | `CHAT_MODE` | 陪伴闲聊、轻松短句 | `web_search` / `read` / `calculator` | 15 轮 | 完整轮 > 15 即触发（`chat_max_rounds=15`） |
| 工作 | `WORK_MODE` | 任务导向、读写执行 | 全量（`bash`/`write`/`edit`/`read`/`web_search`/`calculator`/`get_weather`/`ask_user_questions`…） | 5 轮 | 不启用轮数触发，纯 token 预算 |

- 两模式共享同一份会话历史（session_store），只是**送模型视图**与**轮次上限**不同。
- 切换模式 → `mode_state.ModeState` + `build_switch_context`，运行时按 `get_mode_config(mode)` 读配置。
- 语音指令（如"进入工作模式"）与手动均可切换。

---

## 2. 分层架构

```
session_store（会话层）
   └─ JSONL 逐条追加，全量真源（带 run_id / sub_turn / tool_call_id）
        │
        ▼
context_builder.build_model_context（上下文层，只读派生，永不改写会话）
   ├─ group_into_turns：按 user 轮分组，绝不在 assistant(tool_calls) 与其 tool 结果间切开
   ├─ work 模式：超保留窗口的旧轮工具调用对 → JSON 占位（片段+续读）
   │    近 10 条工具结果全文保留；完整内容落盘 tool_result/<uuid>.txt（3 天过期）
   ├─ 注入：user_profile + 压缩检查点摘要 + 记忆（v2 扁平 / v1 回退）
   └─ 估算 token → 决定是否触发压缩
        │
        ▼
compaction.prepare_compaction（压缩层）
   └─ 超预算 → 最早完整轮 → LLM 摘要（五层字段）→ CompactionState 检查点
        │
        ▼
记忆层（v2，见《记忆模块改造计划.md》）
   ├─ user_profile     ＝ L3 自传融合 L2（身份/偏好画像，恒注入）
   ├─ MEMORY.md        ＝ 长期事务/知识主干（≤1000 token，恒注入）
   └─ YYYY-MM-DD.json  ＝ L1 每日事件（只注入昨天；更久按需查）
```

---

## 3. Agent 环（run / sub_turn）

- **run** = 处理一条用户输入的一次完整 agent loop（每 run 生成 `run_id`）。
- **sub_turn** = run 里第 N 次模型调用（工具轮 + 最终回复轮）。
- 每次 sub_turn 都从会话层**重建上下文**（拿到新工具结果/压缩检查点/记忆），
  因此在一次 run 内可以连续多轮调用工具，前后文保持一致。
- 降级兜底：sub_turn 超过该模式 `max_sub_turns` 后，最后一次模型调用前注入
  "轮次已达上限"描述并清空 tools，让模型生成收尾回复。

### yield 事件契约

| 事件 | 载荷 | 含义 |
|---|---|---|
| `("sub_turn", n)` | 轮次号 | 每个 sub_turn 开始 |
| `("tool", name, call_id, args)` | 工具信息 | 工具开始执行 |
| `("reply", sentence, emotion)` | 文本 + 情绪 | 最终回复逐句（流式） |
| `("compacted", count)` | 压缩次数 | 完成一次压缩（随后异步持久化记忆） |
| `("done", outcome)` | 结果 | run 结束（`completed` / `max_turns`） |

---

## 4. 工具机制

- **原生 function calling**：工具以 API `tools` 参数传给 LLM（按模式白名单构建，
  `tools/loader.build_tools_list(mode)`），LLM 返回原生 `tool_calls`（带 id）。
- 执行：并发调度（上限 `MAX_PARALLEL_TOOL_CALLS=2`）→ 按 `tool_call_id` 回填
  `tool` 角色消息写回会话层。
- 工具实现在 `tools/`；`execution_mode` / approval 声明在 `tools/loader.py`。
- 主动记忆工具：`memory_add` / `memory_forget`（`tools/memory.py`），
  由 `main.py` `bind_memory(store, extractor, memory_fs=...)` 注入运行时依赖；
  `memory_add` 同时按三分工写穿 v2 扁平文件。

---

## 5. 压缩（check_context → compact_memory）

### 5.1 触发预算（design 2.2）

```
history_budget = max_input_length × compact_ratio × 0.95
               − system_prompt_tokens − summary_tokens
```

- `max_input_length = QWEN_CONTEXT_TOKENS = 1_000_000`
- `compact_ratio = 0.7`，`reserve = 0.95`
- 已占用历史 token > `history_budget` 即触发上下文拆分（`agent_runtime` 计算并传入
  `prepare_compaction(..., threshold=min(budget, config.compaction_threshold))`）。
- 兜底：`ctx.estimated_tokens ≥ config.compaction_threshold`（= 700k）也触发。

### 5.2 摘要格式（五层字段，小写无空格）

```
goal          用户目标
constraints   约束和偏好
progress      任务进展
keydecision   关键决策
nextsteps     下一步计划
```

### 5.3 执行与沉淀

- **异步不阻塞**：压缩摘要由独立 summarizer 生成（`main.py` 内 `_summarize`，
  摘要预算 ≈ 历史 token × 0.1，`COMPACT_SUMMARY_RATIO`，上限 2000 下限 200）。
- 压缩提交后 `asyncio.create_task(_persist_after_compaction(...))` 异步写：
  `dialog/YYYY-MM-DD.json`（memory_compact 条目）、`memory/YYYY-MM-DD.md`、
  `MEMORY.md`（沉淀 keydecision / nextsteps）。
- **每日主动触发一次**：`_daily_persist_loop` 每小时检查，一天至少落一次基线到
  `dialog/YYYY-MM-DD.json`（不再是"会话结束"触发）。

---

## 6. 记忆注入（v2 扁平文件三分工）

| 层 | 文件 | 注入策略 |
|---|---|---|
| user_profile（=L3 自传融合 L2） | `users/<uid>/profile.json` + MEMORY.md 中身份偏好 | 恒注入 `<user-profile>` |
| MEMORY.md（长期主干） | `memories/<uid>/MEMORY.md` | 恒注入（≤1000 token，预算截断） |
| YYYY-MM-DD.json（=L1 事件） | `memories/<uid>/memory/昨日.md` | 只注入昨天；更久按需查询 |

- 注入文本由 `MemoryFs.build_inject_text(max_tokens)` 组成（MEMORY.md 优先，
  昨日日志次之，预算内截断），经 `agent_runtime._v2_memory_text` 传入
  `build_model_context(memory_text=...)`。
- 有 `memory_fs` 时 v2 扁平记忆为唯一注入源（`memory_blocks` 置 None）；
  无 `memory_fs` 时回退 v1 `memory_store.recall_blocks()`（`## Long-term Memory` 块，
  L3 > L2 > L1，`memory_max_tokens=1800` 预算 clamp）。
- 存储层 `MemoryFs`：`MEMORY.md` / `memory/` / `tool_result/` / `dialog/`，
  tool_result 3 天过期自动清理；文件读写全程单层锁。

---

## 7. 配置清单（`agent_config.py`）

| 常量 | 值 | 含义 |
|---|---|---|
| `QWEN_CONTEXT_TOKENS` | 1_000_000 | 模型输入上限 |
| `COMPACT_RATIO` | 0.7 | 压缩预算占比 |
| `RESERVE_RATIO` | 0.95 | 预算余量 |
| `COMPACT_SUMMARY_RATIO` | 0.1 | 摘要长度约为历史 10% |
| `CHAT_MAX_ROUNDS` | 15 | 闲聊轮数压缩触发 |
| `MEMORY_ENABLED` | True | 记忆总开关 |
| `MEMORY_MAX_TOKENS` | 1800 | 每轮记忆注入预算 |
| `MEMORY_TOOL_CHAT_ENABLED` | True | 闲聊模式开放记忆工具 |

模式配置 `ModeAgentConfig`：`keep_complete_turns`（chat 15 / work 5）、
`max_sub_turns`（chat 10 / work 30）、`drop_old_tool_results`、
`context_max_tokens`、`compaction_threshold`、`chat_max_rounds`。

---

## 8. 相关文件映射

| 文件 | 职责 |
|---|---|
| `agent_config.py` | 模式配置 + 预算公式 |
| `session_store.py` | 会话层（JSONL 真源，transcript 视图） |
| `context_builder.py` | 上下文派生（轮分组/工具压缩/记忆注入/近 10 条工具保留） |
| `compaction.py` | 压缩判断 + 五层摘要指令 + 检查点状态 |
| `agent_runtime.py` | agent 环（run/sub_turn/工具/压缩/持久化调度） |
| `memory_fs.py` | v2 扁平记忆文件系统层 |
| `memory_store.py` / `memory_extractor.py` | v1 分层记忆（迁移桥，逐步退役） |
| `tools/memory.py` | 主动记忆工具（memory_add/forget，v2 写穿） |
| `prompt_loader.py` | 系统提示词组装（agent.md + 模式专用） |
| `main.py` | 装配（实例 + WS 链路 + 记忆/压缩接线 + Phase⑤ REST 接口） |

---

## 9. 常见注意点

- **`threading.Lock` 不可重入**：`memory_store` / `memory_fs` 任何持锁方法都**不得**
  再调用同样本实例里持锁的另一个方法（曾多次死锁）；内部改用无锁私有读者。
- **压缩不丢历史**：压缩失败只打日志继续跑；摘要模型未返回或字段缺省会判 invalid，
  沿用旧检查点。
- **不承诺前端强同步**：后端状态通知（`_sync_backend_state`）只作去重广播，
  前端以自己状态为准。
- **临时验证脚本**：`backend/tests/_rt*.py` 等为开发期探针，非正式单测，
  可随时删除，不作回归保障。
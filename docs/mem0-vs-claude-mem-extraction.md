# mem0和claude-mem的tool use 记忆抽取处理对照




mem0 把 tool use 记进本地证据库，但抽取时几乎不带工具 I/O，平台 LLM 抽的是"人和 Claude 的对话"，工具信息只剩路径和失败命令；claude-mem 把每个工具的完整 I/O 实时喂给 observer，工具调用本身就是抽取原料，但 observer 结构上看不到主会话助手推理。
---

## 工具调用的捕获

两者都在 `PostToolUse` hook捕获tool use，mem0把tool use保存在本地数据库等待进一步处理，claude-mem 直接送 worker 待实时抽取。

### mem0
 memory_core.py:1371-1384：

```python
def record_tool(store, hook_input, *, failed=False):
    session_id = _session_id(hook_input)
    repo = store.repo_for_session(session_id, hook_input.get("cwd"))
    payload = tool_payload(hook_input, failed=failed)
    if payload.get("path"):
        payload["repo_path"] = _repo_relative_path(repo, str(payload["path"]))
    store.record_event(repo, session_id, "tool_failure" if failed else "tool_result", payload)
```

`tool_payload`按工具类型精选字段，也不是全量 I/O：

| 工具 | 记录字段 | 不记录 |
|---|---|---|
| Read | `path` | 文件内容 |
| Write/Edit/MultiEdit/NotebookEdit | `path` + `mutation_chars`（改动字符数） | 新旧内容 |
| Bash | `command`（≤2000, `MAX_COMMAND_CHARS`）+ `command_kind`（test/build/shell）+ `result_preview`（≤2500, `_tool_result_preview`） | 完整 stdout |
| Grep/Glob/WebSearch/WebFetch | `pattern`/`path`/`query`/`url` | 匹配结果 |
| 其它 | `input_keys`（键名 top20） | 值 |
| 所有 | `agent_role`（`"main"` 或有 agent_id 时 `"sidekick"`）、`failed`、`duration_ms` | — |

`record_tool` 纯本地写。捕获阶段不发送到平台。

### claude-mem

和 mem0 的差别：不按工具类型精选字段，`tool_input`/`tool_response` 整体传入worker

---

## 从工具调用到抽取输入

### mem0

flush 时`flush_session`方法调用 `build_episode`。`build_episode` 把工具事件收进 `structured`：commands[-30:]

commands里面的信息部分添加到semantic_evidence中，会经历大幅裁剪



```python
def build_semantic_evidence(structured: dict[str, Any]) -> str:
    """Format changed paths for memory extraction.

    Test and build results remain in the local evidence store for diagnostics,
    but are not useful repository knowledge by default and should not steer
    memory extraction toward transient verification details.
    """
    modified_paths = [
        bounded(path, 500) for path in structured.get("files_modified", [])[:20]
    ]
    commands = structured.get("commands") or []
    if not any(command.get("status") == "failed" for command in commands):
        commands = []

    if not modified_paths and not commands:
        return ""

    lines = ["Additional repository details from this session"]
    if modified_paths:
        lines.extend(
            [
                "",
                "Changed paths:",
                *[f"- {path}" for path in modified_paths],
            ]
        )
    if commands:
        lines.extend(["", "Commands run in this session:", *_render_command_lines(commands)])
    return bounded("\n".join(lines), 8000)
```



由 `build_extraction_messages`把evidence拼接到最终发送到平台的messages的末尾：

```python
def build_extraction_messages(structured):
    evidence = build_semantic_evidence(structured)         
    messages = [
        {"role": m["role"], "content": redact(m["content"]).strip()}
        for m in structured["extraction_messages"]           
        if m["role"] in {"user", "assistant"} and m["content"]
    ]
    if evidence:
        for m in reversed(messages):                         #拼接到末尾，role是assistant
            if m["role"] == "assistant":
                m["content"] = f"{m['content']}\n\n{evidence}"
                break
        else:
            messages.append({"role": "assistant", "content": evidence})
    return messages
```

mem0 记了很多tool_resule，但发给平台的 messages 里 tool 信息只剩改动路径和失败命令。成功命令、Read 内容、Grep 匹配、searches 全被丢弃。


flush POST 的body 里没有任何 tool I/O 字段。会话内容唯一载体是 `messages`，而 messages 里 tool 信息只有 `build_semantic_evidence` 的路径和失败命令。平台 LLM 拿到 messages 后内部怎么抽，闭源不可证。但本地代码可证发出去的包里没有那些 tool 细节，平台无从获取未发送的内容。

### claude-mem：每个工具的完整 I/O 就是抽取输入

worker 收到 POST 后经 `ingestObservation`（§4 六道过滤）→ `queueObservation`（`SessionManager.ts:170-186`）→ `SessionMessageBuffer` 排队 → `ClaudeProvider.createMessageGenerator`（`ClaudeProvider.ts:544`）逐个 `yield` 给 observer LLM。

`ClaudeProvider.ts:594-648` observation 分支：

```ts
if (message.type === 'observation') {
  // ... recycle 检查 ...
  const optimized = compressField
    ? await optimizeObservationFields({toolInput: message.tool_input, toolOutput: message.tool_response}, ...)
    : {toolInput: message.tool_input, toolOutput: message.tool_response};
  const obsPrompt = buildObservationPrompt({               // L625
    tool_name: message.tool_name!,
    tool_input: JSON.stringify(optimized.toolInput),
    tool_output: JSON.stringify(optimized.toolOutput),
    created_at_epoch: Date.now(),
    cwd: message.cwd
  });
  session.conversationHistory.push({ role: 'user', content: obsPrompt });  // L635
  yield { type: 'user', message: { role: 'user', content: obsPrompt }, ... };  // L639
}
```

`queueObservation` 构造的 `PendingMessage`（`SessionManager.ts:176-186`）不设 `last_assistant_message`；`worker-types.ts:105-114` 的 `ObservationData` 根本没有该字段。所以逐工具抽取输入就是 `tool_name + tool_input + tool_output + cwd`，没有主会话助手推理。主会话助手文本只在 Stop 时经 `queueSummarize`（`SessionManager.ts:208-211`，带 `last_assistant_message`）→ `buildSummaryPrompt`（`ClaudeProvider.ts:650`）进入，而且产 `<summary>` 不产 `<observation>`。

和 mem0 相反，claude-mem 的 tool 抽取输入就是工具 I/O 本身，不经"对话描述"中转。N 个工具调用 → M 条 observation（`SessionMessageBuffer.ts:22-38` "memory agent batches N tool-uses into M observations"），在 RAM 里批处理，到 400k 字符回收（`CLAUDE_MEM_OBSERVER_MAX_CONVERSATION_CHARS=400000`）。

---

## tool use 抽取提示词

### mem0：本地无 tool-use 专门提示词，平台闭源

mem0 的抽取 system prompt 在云端平台，本地不可见。flush POST 的 body 只带指令文本（非完整 prompt）：

- `agent_custom_instructions` = `PROJECT_MEMORY_INSTRUCTIONS`（L74）："Save concise repository facts... A completed change should produce one memory explaining the resulting behavior, where it is implemented when useful, and any important constraints or reasoning."
- `custom_instructions` = `PERSONAL_MEMORY_INSTRUCTIONS`（L86）："...Do not save repository facts, project decisions, commands, or what was built."
- `custom_categories` = `CODING_MEMORY_CATEGORIES`（L92）：含 `project_knowledge` 等
- `infer: True`

本地对 tool use 的"提示词"实际只有 `build_semantic_evidence` 的渲染格式（`_render_command_lines` L1487-1494：`- [status/kind] command — result_preview≤500`）和注释里那句"not useful by default"的价值判断。后者决定成功命令被丢弃。

### claude-mem：`buildObservationPrompt` + `code.json` 常驻指令

逐工具包裹器 `buildObservationPrompt`（`prompts.ts:156-190`），逐字：

```
<observed_from_primary_session>
  <what_happened>${tool_name}</what_happened>
  <occurred_at>${ISO timestamp}</occurred_at>
  <working_directory>${cwd}</working_directory>
  <parameters>${truncateObservationField(tool_input)}</parameters>
  <outcome>${truncateObservationField(tool_output)}</outcome>
</observed_from_primary_session>

If a <parameters> or <outcome> block above contains an "<elided chars=... />" marker,
that field was truncated to fit the observer's context window. Describe only what
you can see in the kept portion and do not infer details about the elided range.

Return either one or more <observation>...</observation> blocks, or
<skip_summary reason="noise" /> if this tool use should be skipped.
Concrete debugging findings from logs, queue state, database rows, session routing,
or code-path inspection count as durable discoveries and should be recorded.
Never reply with prose such as "Skipping", "No substantive tool executions", or
any explanation outside XML. Non-XML text is discarded.
```

`truncateObservationField`（L142-153）：每字段 16k 字符（`OBS_PROMPT_FIELD_MAX_CHARS`），超限取 60% head + 30% tail + `<elided>` 标记。注释 L126-132：防一个超大 Read 结果（#2468 报过 130k 字符文件）撑爆 128k 上下文。

常驻指令在 init 时由 `buildInitPrompt`（L64-90）注入，SDK 会话全程有效。来自 `plugin/modes/code.json`，和 tool use 相关的几段：

- `system_identity`（L108）："Record what was LEARNED/BUILT/FIXED/DEPLOYED/CONFIGURED, not what you (the observer) are doing."
- `recording_focus`（L111）："Focus on durable technical signal... Concrete debugging or investigative findings from logs, traces, queue state, database rows, and code-path inspection"；✅ GOOD "Authentication now supports OAuth2..."；❌ BAD "Analyzed authentication implementation and stored findings"
- `skip_guidance`（L112）是 tool use 的过滤器提示词："Skip: empty status checks / package installations with no errors / simple file listings with no follow-on finding / repetitive operations / file research that comes back empty"
- 输出 schema（`observationSkeleton` L24-62）：9 类 type（bugfix/feature/refactor/change/discovery/decision/security_alert/security_note/sensitive）× 7 concept（how-it-works/why-it-exists/what-changed/problem-solution/gotcha/pattern/trade-off）

JWT Write 工具的完整抽取输入示例（observer 服务端 messages 最新 user 轮）：

```
<observed_from_primary_session>
  <what_happened>Write</what_happened>
  <occurred_at>2026-09-12T17:21:33.000Z</occurred_at>
  <working_directory>/tmp/exp</working_directory>
  <parameters>{"file_path":"/tmp/exp/jwt_auth.py","content":"def _sign_rs256(signing_input, private_key):\n    return private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())\n..."}</parameters>
  <outcome>The file /tmp/exp/jwt_auth.py has been updated.</outcome>
</observed_from_primary_session>

If a <parameters> or <outcome> block above contains an "<elided chars=... />" marker...
Return either one or more <observation>...</observation> blocks, or <skip_summary reason="noise" />...
```
observer 据此加常驻指令回 `<observation><type>feature</type><title>JWT RS256 signing implemented</title>...<files_modified><file>/tmp/exp/jwt_auth.py</file></files_modified></observation>` 或 `<skip_summary reason="noise"/>`。















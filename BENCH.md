# SWE-ContextBench 跑分框架 + 通用记忆插件

复现 SWEContextBench 论文 No-Context / Free Context / Free Summary 对照实验,
**真实目标**:横向对比不同记忆库后端(Mem0 / 自研 / trivial 基线)的**记忆策略**。

## 三层分离(只让记忆策略层变)
```
① agent 行为层   SKILL.md(何时 recall)              【固定】
② 工具/管道层     memory-bridge(MCP 工具 + hook CLI)【固定,零智能】
③ 记忆策略层     可换后端(Mem0 / 自研 / trivial)    【唯一变量】
```
换后端只改 `--backend`,agent 行为零变化 → 分数差异纯反映记忆策略。

## 组件
- `plugins/memory-bridge/` — 通用桥包。MCP 工具镜像 mem0(`add_memory`/
  `search_memories`/...),CLI(serve-mcp/add/search/ingest/summarize),后端
  统一 AML Add/Search 契约。含 `trivial`(BM25 文件持久化,开箱即用)、
  `mem0`、`custom`(你的库适配点)。
- `plugins/memory-plugin/` — Claude Code 插件束:`SKILL.md`(先搜后修,不存新问题)+
  mem0 式生命周期 hooks(PreToolUse 阻断原生记忆 + SessionStart 只读注入;写回 hook
  已移除以保持固定记忆池)+ `baseline_hygiene.json`(禁原生记忆 + AskUserQuestion)。
- `bench/` — 编排器 CLI:`ingest`/`run`/`eval`/`matrix`,生成 Harbor task.toml
  + JobConfig,封装 `evaluation.sh` 判分。

## 快速跑通(无需 Harbor/外部 API)
```bash
cd plugins/memory-bridge && uv venv .venv && . .venv/bin/activate && uv pip install -e .
# 灌入 + 检索(trivial 后端,纯 stdlib)
memory-bridge ingest --from "../../cases/SWEContextBench Lite Past Experience/*.jsonl" \
  --backend trivial --store-path /tmp/mem.jsonl --limit 20
memory-bridge search "async response handler" --user-id django --top-k 3 \
  --backend trivial --store-path /tmp/mem.jsonl
```

## 完整矩阵(Harbor + API key)
```bash
cd bench && uv venv .venv && . .venv/bin/activate && uv pip install -e .
export SWECB_ROOT=$(git rev-parse --show-toplevel)
export ANTHROPIC_API_KEY=...

# No-Context 基线
bench run --setting no-context --backend trivial --limit 2
bench eval no_context_trivial_sonnet-4-5_lite

# Free Context (先灌入再跑)
bench ingest --setting free-context --backend trivial --store-path /tmp/mem.jsonl
bench run --setting free-context --backend trivial --store-path /tmp/mem.jsonl --limit 2

# 矩阵:setting × backend × model
bench matrix --config matrix.yaml
```

## 换记忆库后端
1. 你的库实现 AML Add/Search HTTP → `bench ... --backend custom --backend-url http://your-svc:8000`
2. 或 Python 协议 → 在 `plugins/memory-bridge/src/memory_bridge/backends/custom.py` 用
   `load_custom_class("module:Class")` 注册。
SKILL/工具面/hooks 全不变,只换后端。

## 实验控制(已落地)
- **No-Context 不挂任何插件**,仅基线卫生(permissions 禁原生记忆 + AskUserQuestion)。
- **禁止 Claude 写自己记忆**:`baseline_hygiene.json` permissions.deny +
  `pretool_block_native_memory.sh` PreToolUse hook 双保险。
- **user_id = repo** 隔离检索范围(同仓库经验)。
- **Free Summary** 用 gpt-4o-mini 生成 ~217 token 摘要,缓存幂等。
- **固定记忆池(对齐论文)**:Free 模式只保留 `PreToolUse`(拦原生记忆)+ `SessionStart`
  (只读注入)两个 hook;PostToolUse/Stop 写回 hook 已移除——避免前一个任务的解法污染
  后一个任务的检索。SKILL 指令为"先搜后修,**不要把新问题存入记忆**"(论文 Supermemory 范式)。
- **token 消耗**从 harbor 自带的 `trajectory.json` 的 `final_metrics` 解析,**无需自建代理**。

## 一键跑实验:`run.py`(workbuddy-bench 风格)
`run.py` 是统一的实验编排器:YAML profile + CLI 覆盖,并发跑 agent,实时进度+token 统计,
事后 batch 判分,输出 per-repo + token + cost 汇总。

```bash
# 用 bench 的 venv 跑(这样 bench.* / memory_bridge 都能 import)
cd /home/neverland/workspace/SWEContextBench

# No-Context 基线(profile 默认)
bench/.venv/bin/python run.py

# Free Context(先灌 300 条轨迹,再跑 99 任务;自动判分)
bench/.venv/bin/python run.py --profile bench/profiles/free-context.yaml

# Free Summary(gpt-4o-mini 摘要池)
bench/.venv/bin/python run.py --profile bench/profiles/free-summary.yaml

# 烟雾测试:只跑 2 个任务,不判分
bench/.venv/bin/python run.py --limit 2 --no-eval

# 调并发 / 指定任务
bench/.venv/bin/python run.py -n 8
bench/.venv/bin/python run.py --tasks django__django-11776,sklearn__scikit-learn-13241

# 续跑上次中断的运行(自动找同配置最新一次 run)
bench/.venv/bin/python run.py --continue
# 或指定某次 run_id 续跑
bench/.venv/bin/python run.py --run-id no-context_trivial_glm-5.2_lite__20260907-194127-95e9
```

**run_id 设计**:默认每次运行唯一 —— `<setting>_<backend>_<model>_<subset>__<时间戳-随机>` ,
所以同配置跑多次互不覆盖,结果在 `runs/<run_id>/` 和 `predictions/<run_id>/` 里累积。
- 续跑某次:`--continue`(自动找同 base 最新的 run)或 `--run-id <id>`。
- `--resume`(默认 on)只在当前 run_id 下跳过已有 patch;新 run(带时间戳)从零开始。
- 想覆盖/命名:直接 `--run-id <name>`。

产出(在 `runs/<run_id>/` 下):
- **实时**:每 5s 一行 `▶ done/total · running · tok in/out (cache) · cost · elapsed`,
  每个任务完成一行 `✓ iid  status  steps  in/cache/out · cost · dur`。
- `summary.json` — resolve 率、per-repo 明细、token/cost 总计、resolved_ids。
- `results.csv` — 每个任务的 patch/resolved/token/cost/duration。
- `run.log` — 全部进度行。
- predictions 按运行隔离:`predictions/<run_id>/<id>_preds.json`(避免跨设置串)。
- SWE-bench 判分报告:`<run_id>.json`(evaluation.sh 产出)。

> **Free 模式 bringup 待办**:`task.toml` 已 bind-mount 插件目录 + memory store 到容器,
> 但 MCP server 命令是 `uvx memory-bridge`,而 memory-bridge 是本地包未发到 PyPI。
> 跑 Free 前需先确认 uvx 能取到本地包(或改为 mount 源码 + `python -m memory_bridge.cli
> serve-mcp`),并做一次单任务烟雾测试。


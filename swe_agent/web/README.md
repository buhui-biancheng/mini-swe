# Mini-SWE Web 控制台

本地浏览器实时观察 FSM 修复过程：状态流转、工具调用、以及 **AI 在代码依赖图上
读取/移动的节点实时点亮**。

```
python -m swe_agent.web [--project DIR] [--port 7777]
```

打开 http://localhost:7777 。在右侧聊天框输入 `bug文件路径 [测试命令]`（如
`examples/bug_return_value.py python -m pytest test_return_value.py`），回车即可
发起一次修复，全程实时可见。

## 前端来源与契约

前端借用了 [waku-agent](https://github.com/ShenSeanChen/waku-agent) 的 dashboard
（MIT 协议），见 `third_party/waku-agent/`（本地仅作参考，不随项目提交）。

waku dashboard 的契约是**纯静态 + 文件驱动**，所以我们几乎没有改它的 JS：

- 前端是无构建的纯静态文件（`static/`），浏览器每 5s 轮询 `/api/data`，
  每 450ms 轮询 `/api/events?cursor=N` 读 trace JSONL 新行。
- 事件双写：JSONL trace 用 `type` 字段（驱动实时亮灯），SSE 用 `kind` 字段
  （驱动聊天 dock 流式渲染）。
- 图拓扑契约：`d.graph.workflows = [{name, nodes:[{name,kind}], edges:[{src,dst}]}]`，
  `node_start/node_end` 事件按 `node` 字段点亮 SVG 节点。

## 本模块结构

| 文件 | 职责 |
|---|---|
| `server.py` | 标准库 HTTP 服务器 + 全部端点（/api/data、/api/events、/api/chat/stream 等） |
| `runner.py` | spawn `python -m swe_agent.main fix ... --fsm`，解析 stdout 标记 → 事件 |
| `graphdata.py` | `graph.json` → waku 拓扑结构；工具调用参数 → 图节点解析 |
| `payload.py` | 构造 /api/data 载荷（turns/统计/会话/图拓扑） |
| `static/` | waku dashboard 前端（裁剪：去掉 Memory/Tools/Settings/Arena；Overview = FSM 状态流转图 + 代码依赖图） |
| `static/js/fsm.js` | **FSM 状态流转图**：8 状态盒 + 转移边（来自 agent_fsm.py TRANSITIONS），state 事件实时点亮 |

## 实时机制怎么工作

1. 聊天框提交任务 → `FixRunner` spawn FSM 子进程。
2. 子进程 stdout 标记（`[STATE]`/`[TOOL]`/`[TEST]`/`[SUCCESS]` 等）被逐行解析，
   翻译成事件：`state`（FSM 状态流转，含 attempt）→ `graph_start`（点亮 INIT 上下文节点）
   → 每个工具调用触达的文件 `node_start/node_end`（高亮跟随 AI）→ `tool` → `turn_end`/`done`。
   success/fail 由 `[SUCCESS]`/`[FAIL]` 补发（FSM 不打印这两个状态的 `[STATE]`）。
3. 事件同时写入 `~/.swe_agent_web/traces/<date>.jsonl` 并推给 SSE。
4. 浏览器 450ms 轮询读到新事件：
   - `state` → `animateFSM` 点亮 FSM 状态盒 + 动画刚走过的转移边
   - 图事件 → `animateGraphStage` 用 `[data-node="g-code-graph-<node>"]` 点亮代码图节点

运行数据（trace、state.db 聊天记录）存在 `~/.swe_agent_web/`，可用
`SWE_WEB_HOME` 环境变量改位置。

## 已知限制（v1）

- 一次只跑一个修复任务（并发任务返回"忙"）。
- LLM 层未接流式 token（FSM 本身非流式），实时感靠事件级推送。
- 代码图是**力导向散点（Obsidian 式，焦点+上下文）**：全图淡背景小点，当前被读取的节点 + 其 1 跳邻居放大带标签，其余淡出——任何规模不丢节点（AI 读到谁就点亮谁）；仅 >400 节点按入度裁剪（护栏）；全名在 tooltip。
- 图上下文只在 INIT/回滚时注入，轮内"读取"主要来自工具触达文件 → 节点。

"""Mini-SWE web 控制台：本地浏览器实时观察 FSM 修复过程。

前端借用 waku-agent dashboard 的纯静态契约（无构建、轮询 JSONL 亮灯），
后端把我们 FSM 的 stdout 标记翻译成它认识的事件流。
"""

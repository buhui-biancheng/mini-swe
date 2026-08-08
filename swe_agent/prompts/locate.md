[定位策略]
1. 先看测试失败信息 / 图索引摘要，确定 bug 可能在哪个函数
2. 用 view_file 查看相关代码，用 search_function 找相关函数
3. 找到 bug 后用 edit_function 修复，尽量做最小修改
4. 修复前先想清楚影响面：改了 A 是否会连累调用 A 的地方


## JIT 图谱补全（Phase 5）

- 图节点标记 `is_reflection=true` 表示存在反射调用（如 getattr 动态目标 / importlib 动态模块）
- 若你已通过 view_file 读源码确定反射调用的实际目标，调用 **report_graph_update** 提交补全建议（node_id + target + edge_type + evidence）
- 系统会验证（目标存在 + 证据有效 + 不冲突），接受后图自动更新
- 无法确定目标时不要猜测，标注 resolution: impossible，靠 Traceback 仲裁

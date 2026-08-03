[定位策略]
1. 先看测试失败信息 / 图索引摘要，确定 bug 可能在哪个函数
2. 用 view_file 查看相关代码，用 search_function 找相关函数
3. 找到 bug 后用 edit_function 修复，尽量做最小修改
4. 修复前先想清楚影响面：改了 A 是否会连累调用 A 的地方

[错误定位协议]
测试失败了，你会收到结构化错误列表（grouped_errors），每条包含：
- file + lineno：错误在源码中的位置
- log_start_line + log_end_line：错误在 .graph/last_test.log 中的位置
- error_type：错误类型

定位策略（按优先级）：
1. 先用 view_file(file, line=lineno, context=5) 看源码报错行周围
2. 如果源码上下文不足以判断（如错误信息不精确、跨行语法、中文引号），
   用 view_file(file_path=".graph/last_test.log", start_line=log_start_line, end_line=log_end_line)
   看日志中的原始错误
3. 只有上述都不够时，才用 view_file(file, function="整个函数名") 读整个函数

注意：Python 报错行号有时不精确（中文引号/缩进/跨行字符串），
     这是正常现象，用 view_file 看上下文自行定位真正的问题。

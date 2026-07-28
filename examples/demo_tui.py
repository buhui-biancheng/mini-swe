"""TUI 演示脚本：展示 TUI 的各种功能。"""

import time
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from swe_agent.tui.app import ChatApp, run_tui


def demo_run():
    """演示运行。"""
    app = ChatApp()

    # 模拟 Agent 工作流程
    def simulate_agent():
        time.sleep(1)

        # 设置状态
        app.set_status("locate", step=1, token_usage=0)

        # 添加思考链
        app.add_thinking(
            "我需要先查看 greet 函数的源码，看看哪里有问题。\n"
            "从骨架来看，greet 函数在第 1-4 行。\n"
            "让我用 expand_function 查看完整源码...",
            collapsed=False,
        )
        time.sleep(0.5)

        # 工具调用
        app.add_tool_call("expand_function", {"name": "greet"}, success=True, duration=0.2)
        app.add_tool_result("def greet(name):\n    return msg")
        time.sleep(0.5)

        # 继续思考
        app.add_thinking(
            "找到了！第 4 行 return msg 使用了未定义变量。\n"
            "应该是 return message，让我修复它...",
            collapsed=False,
        )
        time.sleep(0.5)

        # 编辑
        app.set_status("patch", step=2, token_usage=150)
        app.add_tool_call("edit_function", {"file_path": "bug.py", "line": 4}, success=True, duration=0.1)
        app.add_code_diff("return msg", "return message")
        time.sleep(0.5)

        # 测试
        app.set_status("test", step=3, token_usage=200)
        app.add_tool_call("run_test", {"command": "pytest"}, success=True, duration=1.5)
        app.add_tool_result("2 passed")
        time.sleep(0.5)

        # 完成
        app.set_status("success", step=3, token_usage=200)
        app.add_ai_message("修复完成！共尝试 1 次。")

    # 启动模拟线程
    import threading
    thread = threading.Thread(target=simulate_agent, daemon=True)
    thread.start()

    # 运行 TUI
    app.run()


if __name__ == "__main__":
    demo_run()

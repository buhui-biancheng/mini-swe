from pydantic import BaseModel, Field, ValidationError
from typing import Any, Optional


class SearchFunctionArgs(BaseModel):
    """搜索项目骨架中的函数。"""
    name: str = Field(description="要搜索的函数名")


class ExpandFunctionArgs(BaseModel):
    """展开查看指定函数的完整源码。"""
    file_path: str = Field(description="Python 文件路径")
    func_name: str = Field(description="函数名，类方法格式为 ClassName.method_name")


class EditFunctionArgs(BaseModel):
    """编辑文件中的指定行范围。"""
    file_path: str = Field(description="要编辑的文件路径")
    start_line: int = Field(description="起始行号（从 1 开始）")
    end_line: int = Field(description="结束行号（包含）")
    new_code: str = Field(description="替换的新代码")


class RunTestArgs(BaseModel):
    """在沙盒中运行测试命令。"""
    command: str = Field(description="要执行的测试命令，如 'pytest test.py'")


class RunCommandArgs(BaseModel):
    """在宿主机上运行终端命令。"""
    command: str = Field(description="要执行的终端命令，如 'ls -la'")


# 工具名称到 Schema 的映射
TOOL_SCHEMAS = {
    "search_function": SearchFunctionArgs,
    "expand_function": ExpandFunctionArgs,
    "edit_function": EditFunctionArgs,
    "run_test": RunTestArgs,
    "run_command": RunCommandArgs,
}


def validate_tool_args(tool_name: str, args: dict[str, Any]) -> tuple[bool, Optional[str], Optional[dict[str, Any]]]:
    """校验工具参数。

    Args:
        tool_name: 工具名称
        args: 工具参数

    Returns:
        (是否成功, 错误信息, 校验后的参数)
    """
    schema_class = TOOL_SCHEMAS.get(tool_name)
    if not schema_class:
        return False, f"未知工具: {tool_name}", None

    try:
        validated = schema_class(**args)
        return True, None, validated.model_dump()
    except ValidationError as e:
        error_msg = f"参数校验失败: {e.errors()[0]['msg']}"
        return False, error_msg, None


# Function Calling 工具定义（OpenAI 格式）
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_function",
            "description": "在项目骨架中搜索函数名，返回匹配的函数及其所在文件和行号",
            "parameters": SearchFunctionArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "expand_function",
            "description": "查看指定函数的完整源码",
            "parameters": ExpandFunctionArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_function",
            "description": "编辑文件中的指定行范围，用新代码替换",
            "parameters": EditFunctionArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_test",
            "description": "在 Docker 沙盒中运行测试命令，返回 stdout/stderr/exit_code",
            "parameters": RunTestArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在宿主机上运行终端命令（如 ls, cat, pwd），返回 stdout/stderr/exit_code",
            "parameters": RunCommandArgs.model_json_schema(),
        },
    },
]

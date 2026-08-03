from pydantic import BaseModel, Field, ValidationError
from typing import Any, Optional


class SearchFunctionArgs(BaseModel):
    """搜索项目骨架中的函数。"""
    name: str = Field(description="要搜索的函数名")


class ViewFileArgs(BaseModel):
    """查看文件（三模式，Phase 2 模块 D3）。

    模式 1（function）：读整个函数（吸收原 expand_function）
    模式 2（line + context）：报错行周围
    模式 3（start_line + end_line）：精确行范围（可读 .graph/last_test.log）
    """
    file_path: str = Field(description="要查看的文件路径")
    function: Optional[str] = Field(description="模式 1：函数名", default=None)
    line: Optional[int] = Field(description="模式 2：目标行号", default=None)
    context: int = Field(description="模式 2：行号前后附加的上下文行数（默认 0）", default=0)
    start_line: Optional[int] = Field(description="模式 3：起始行号（从 1 开始）", default=None)
    end_line: Optional[int] = Field(description="模式 3：结束行号（包含），默认到文件末尾", default=None)


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


# 工具名称到 Schema 的映射（Phase 2 简化：expand 并入 view_file，6 → 5）
TOOL_SCHEMAS = {
    "search_function": SearchFunctionArgs,
    "view_file": ViewFileArgs,
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


# Function Calling 工具定义（OpenAI 格式，Phase 2 简化：5 工具）
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
            "name": "view_file",
            "description": "查看文件源码，三模式：function='函数名' 读整个函数；line=行号,context=N 读报错行周围；start_line+end_line 精确读行范围（可读 .graph/last_test.log 日志）",
            "parameters": ViewFileArgs.model_json_schema(),
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

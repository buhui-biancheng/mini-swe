from pydantic import BaseModel, Field


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
]

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
    """编辑文件（三种模式任选其一）：
    模式A（推荐，低摩擦）：old_string→new_string 精确替换，old_string 必须唯一且精确匹配
    模式B：start_line + end_line + new_code 按行范围替换
    模式C（插入）：insert_after（行号，在该行之后插入）+ new_code
    """
    file_path: str = Field(description="要编辑的文件路径")
    old_string: Optional[str] = Field(description="模式A：要替换的原文（必须精确匹配且唯一）", default=None)
    new_string: Optional[str] = Field(description="模式A：替换成的新文本（缺省表示删除 old_string）", default=None)
    start_line: Optional[int] = Field(description="模式B：起始行号（从 1 开始）", default=None)
    end_line: Optional[int] = Field(description="模式B：结束行号（包含）", default=None)
    new_code: Optional[str] = Field(description="模式B/C：替换或插入的新代码", default=None)
    insert_after: Optional[int] = Field(description="模式C：在该行号之后插入 new_code", default=None)


class RunTestArgs(BaseModel):
    """在沙盒中运行测试命令。"""
    command: str = Field(description="要执行的测试命令，如 'pytest test.py'")


class WriteFileArgs(BaseModel):
    """写文件（新建或整文件覆写，2026-08-21 借鉴 Claude Code Write 契约）。

    用于创建新文件或整体重写小文件；修改现有代码请用 edit_function（精准编辑）。
    content 必须是文件的完整内容（不是片段）。
    """
    file_path: str = Field(description="要写入的文件路径（不存在则创建，自动建父目录）")
    content: str = Field(description="文件的完整内容（整文件创建/覆写，不是片段）")


class RunCommandArgs(BaseModel):
    """在宿主机上运行终端命令。"""
    command: str = Field(description="要执行的终端命令，如 'ls -la'")


class ReportGraphUpdateArgs(BaseModel):
    """JIT 图谱补全（Phase 5）：AI 提交反射调用补全建议。"""
    node_id: str = Field(description="反射节点 id（is_reflection=true 的节点）")
    target: str = Field(description="补全的目标节点 id（实际调用目标）")
    edge_type: str = Field(description="边类型（call/data/import 等）")
    evidence: str = Field(description="证据说明（读了哪个文件哪段源码，如何确定目标）")


class PlanItem(BaseModel):
    """计划中的一项任务。"""
    task: str = Field(description="任务描述")
    done: bool = Field(description="是否已完成", default=False)


class SetPlanArgs(BaseModel):
    """声明/更新修复计划（2026-08-16：治本——让 agent 枚举全部待办并逐项覆盖）。

    若问题涉及多个特性/多处修复（如 'several features'、'all X'），必须声明完整计划，
    逐项完成并把 done 置 true，提交前复查无未完成项。每次调用替换整个计划。
    """
    plan: list[PlanItem] = Field(description="完整计划列表（每次调用替换）")


# 工具名称到 Schema 的映射（Phase 2 简化：expand 并入 view_file，6 → 5）
TOOL_SCHEMAS = {
    "search_function": SearchFunctionArgs,
    "view_file": ViewFileArgs,
    "edit_function": EditFunctionArgs,
    "write_file": WriteFileArgs,  # 2026-08-21 写文件（新建/整文件覆写）
    "run_test": RunTestArgs,
    "run_command": RunCommandArgs,
    "report_graph_update": ReportGraphUpdateArgs,  # Phase 5 JIT
    "set_plan": SetPlanArgs,  # 2026-08-16 计划清单（治本：多目标全覆盖）
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
# 2026-08-21 工具集精简：report_graph_update（JIT 反射补全，SWE-bench 静态代码
# 0 使用）不再暴露给 LLM——TOOL_SCHEMAS 保留（registry 内部实现与测试仍可用）。
# search_function 保留（flask 类符号名型实例实测使用：flask4074 12 次）。
# 7 工具 = 读×2/写/改/终端×2/方案。
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
            "description": "编辑文件中的代码。三种模式：① 模式A（推荐）old_string（原文，必须精确匹配且唯一）+ new_string（新文本）精确替换，无需行号；② 模式B start_line+end_line+new_code 行范围替换；③ 模式C insert_after（行号）+new_code 在该行后插入",
            "parameters": EditFunctionArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写文件：新建文件或整文件覆写（content 必须是完整内容）。只用于创建新文件/整体重写小文件；修改现有代码用 edit_function",
            "parameters": WriteFileArgs.model_json_schema(),
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
            "description": "在宿主机上运行终端命令（如 ls, cat, pwd, grep），返回 stdout/stderr/exit_code",
            "parameters": RunCommandArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_plan",
            "description": "声明/更新你的修复计划（task 列表 + 每项 done）。若问题涉及多个特性/多处修复，必须先声明完整清单、逐项完成并把 done 置 true、提交前复查无未完成项。每次调用替换整个计划",
            "parameters": SetPlanArgs.model_json_schema(),
        },
    },
]

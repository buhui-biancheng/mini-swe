"""工具函数模块。"""


def calculate_area(length: float, width: float) -> float:
    """计算矩形面积。

    Args:
        length: 长度
        width: 宽度

    Returns:
        面积
    """
    return length * width


def format_result(value: float, precision: int = 2) -> str:
    """格式化结果。

    Args:
        value: 数值
        precision: 小数精度

    Returns:
        格式化后的字符串
    """
    return f"{value:.{precision}f}"

"""计算器模块。"""

from utils import calculate_area, format_result


class Calculator:
    """计算器类。"""

    def __init__(self):
        self.history = []

    def compute_rectangle_area(self, length: float, width: float) -> str:
        """计算矩形面积并格式化结果。

        Args:
            length: 长度
            width: 宽度

        Returns:
            格式化的面积结果
        """
        area = calculate_area(length, width)
        result = format_result(area)
        self.history.append(result)
        return result

    def compute_circle_area(self, radius: float) -> str:
        """计算圆形面积。

        Args:
            radius: 半径

        Returns:
            格式化的面积结果
        """
        # Bug: pi 应该从 math 模块导入，但这里使用了未定义的变量
        area = pi * radius ** 2  # 错误：pi 未定义
        result = format_result(area)
        self.history.append(result)
        return result

    def get_history(self) -> list[str]:
        """获取计算历史。"""
        return self.history.copy()

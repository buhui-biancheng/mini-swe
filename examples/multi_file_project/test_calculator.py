"""计算器测试。"""

from calculator import Calculator


def test_compute_rectangle_area():
    """测试矩形面积计算。"""
    calc = Calculator()
    result = calc.compute_rectangle_area(3, 4)
    assert result == "12.00"


def test_compute_circle_area():
    """测试圆形面积计算。"""
    calc = Calculator()
    result = calc.compute_circle_area(5)
    # 预期结果: pi * 25 ≈ 78.54
    expected = f"{3.141592653589793 * 25:.2f}"
    assert result == expected


def test_history():
    """测试计算历史。"""
    calc = Calculator()
    calc.compute_rectangle_area(3, 4)
    calc.compute_circle_area(5)
    history = calc.get_history()
    assert len(history) == 2

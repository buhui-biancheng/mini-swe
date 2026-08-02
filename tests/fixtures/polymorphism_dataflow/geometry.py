"""多态全笼罩 + 局部变量持有类实例后调方法 fixture。"""


class Shape:
    def area(self):
        return 0


class Circle(Shape):
    def area(self):
        return 3.14


class Rect(Shape):
    def area(self):
        return 1.0


def calc(shape):
    # 未知类型 → 多态全笼罩：3 个 area 都建边
    return shape.area()


def make_circle():
    # 局部变量持有类实例 → 精确解析到 Circle.area
    c = Circle()
    return c.area()

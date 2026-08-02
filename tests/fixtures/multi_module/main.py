"""模块别名导入 + 属性链调用 fixture。"""

import pkg.math_ops as mo


def run():
    return mo.add(1, 2)


def run2():
    return mo.multiply(3, 4)

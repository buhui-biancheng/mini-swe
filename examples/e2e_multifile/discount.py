"""折扣模块：根据购买数量返回折扣率。"""


def get_discount(quantity: int) -> float:
    """返回折扣率。

    返回值是"比例"（0.9 表示 9 折），不是金额。
    - 数量 ≥ 10：8 折
    - 数量 ≥ 5：9 折
    - 其他：原价
    """
    if quantity >= 10:
        return 0.8
    if quantity >= 5:
        return 0.9
    return 1.0

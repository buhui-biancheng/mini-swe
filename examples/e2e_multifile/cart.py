"""购物车模块：聚合多个商品的定价（症状在这里体现）。"""

from pricing import compute_price


class Cart:
    """购物车。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, float, int]] = []  # (名称, 单价, 数量)

    def add(self, name: str, unit_price: float, quantity: int = 1) -> None:
        """加入一个商品。"""
        self.items.append((name, unit_price, quantity))

    def total(self) -> float:
        """购物车总价：逐项调用 pricing.compute_price 求和。"""
        return round(sum(
            compute_price(price, qty) for _, price, qty in self.items
        ), 2)

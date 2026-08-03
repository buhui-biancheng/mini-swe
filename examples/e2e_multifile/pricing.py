"""定价模块：计算商品含税总价（含折扣）。"""

from discount import get_discount

TAX_RATE = 0.08


def compute_price(base_price: float, quantity: int = 1) -> float:
    """计算含税总价。

    总价 = 单价 × 数量 × 折扣率 × (1 + 税率)

    ⚠️ Bug 在下面这一行：折扣率（如 0.9）被当成金额直接减去了，
    应该是「乘」折扣率。症状在 cart.py 的购物车总价上体现。
    """
    subtotal = base_price * quantity
    discount = get_discount(quantity)
    discounted = subtotal - discount  # BUG: 应为 subtotal * discount
    return round(discounted * (1 + TAX_RATE), 2)

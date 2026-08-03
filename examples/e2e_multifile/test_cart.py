"""购物车端到端测试（多文件跨文件追踪场景）。"""

from cart import Cart
from pricing import compute_price


def test_single_item_price():
    """单价 10 元买 1 件：10 × 1 × 1.0 × 1.08 = 10.8"""
    assert compute_price(10, 1) == 10.8


def test_bulk_discount_applied():
    """买 5 件打 9 折：10 × 5 × 0.9 × 1.08 = 48.6"""
    assert compute_price(10, 5) == 48.6


def test_cart_total():
    """购物车总价：苹果 10 元×2 + 香蕉 5 元×3 → (20 + 15) × 1.08 = 37.8"""
    cart = Cart()
    cart.add("apple", 10, 2)
    cart.add("banana", 5, 3)
    assert cart.total() == 37.8

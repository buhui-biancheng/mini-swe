"""局部变量遮蔽全局名 fixture。"""

CONFIG = {"debug": False}


def read():
    return CONFIG           # 全局引用 → 建 global 边


def shadow():
    CONFIG = 42             # 本地赋值遮蔽全局名
    return CONFIG           # 不建 global 边

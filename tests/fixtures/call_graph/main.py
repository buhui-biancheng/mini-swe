"""调用链 + 数据流级别 2 + 嵌套调用 fixture。"""

from utils import helper


def run():
    x = compute()
    result = helper(x)
    return result


def compute():
    return 42


def nested():
    return helper(compute())

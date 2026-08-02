"""分支数据流全笼罩 fixture。"""


def a():
    return 1


def b():
    return 2


def c(x):
    return x


def pick(flag):
    if flag:
        x = a()            # x 可能来源 a
    else:
        x = b()            # 也可能来源 b
    return c(x)            # → a→c、b→c 两条数据边（全笼罩）

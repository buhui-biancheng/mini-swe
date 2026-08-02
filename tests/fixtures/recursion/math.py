"""递归 + 相互递归 fixture（验证去环 BFS 不挂死）。"""


def fact(n):
    if n <= 1:
        return 1
    return n * fact(n - 1)   # 自环 A→A


def is_even(n):
    return is_odd(n - 1)     # 相互递归


def is_odd(n):
    return is_even(n - 1)

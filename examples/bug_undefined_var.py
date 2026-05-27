def greet(name):
    """返回问候语。"""
    message = f"Hello, {name}!"
    return msg  # BUG: 应该是 message，不是 msg


def farewell(name):
    """返回告别语。"""
    return f"Goodbye, {name}!"

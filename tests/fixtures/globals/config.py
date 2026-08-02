"""全局变量 fixture。"""

CONFIG = {"theme": "dark", "debug": False}


def load():
    return CONFIG


def get_debug():
    return CONFIG.get("debug")

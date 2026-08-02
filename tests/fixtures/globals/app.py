"""跨模块全局变量引用 fixture。"""

from config import CONFIG


def setup():
    CONFIG.update({"debug": True})


def read_config():
    return CONFIG

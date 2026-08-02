"""反射 fixture：importlib 动态加载打标签。"""

import importlib


def load_module(name):
    return importlib.import_module(name)


def load_literal():
    return importlib.import_module("services")

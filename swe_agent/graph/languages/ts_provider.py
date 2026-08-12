# -*- coding: utf-8 -*-
"""Tree-sitter 通用解析器：函数/类/方法/全局/导入/调用（2026-08-08）。

按语言定义关键词表提取结构；调用提取按语言特化（JS/TS 用
call_expression）。Python 仍走 ast 路径不经过这里。
"""

import re

from tree_sitter import Language, Parser

from .base import RawFile, RawSymbol, RawCall, RawImport, SymbolProvider

# ========== 语言注册 ==========

_JS_KEYWORDS = {
    "function": ("function_declaration", "method_definition", "arrow_function",
                 "generator_function_declaration"),
    "class": "class_declaration",
    "method": "method_definition",
    "call": "call_expression",
}

_LANG_CFG = {
    "javascript": _JS_KEYWORDS,
    "typescript": _JS_KEYWORDS,
}


class TreeSitterProvider(SymbolProvider):
    """基于 py-tree-sitter 的通用解析器。"""

    def __init__(self, lang: str):
        self.lang = lang
        self._cfg = _LANG_CFG[lang]
        if lang == "javascript":
            from tree_sitter_javascript import language
        elif lang == "typescript":
            from tree_sitter_typescript import language_typescript as language
        else:
            raise ValueError("未支持语言: %s" % lang)
        self._language = Language(language())
        self._parser = Parser(self._language)

    # ---------- 解析入口 ----------

    def parse(self, source: str, rel_path: str) -> RawFile:
        raw = RawFile(rel_path=rel_path, source=source)
        tree = self._parser.parse(bytes(source, "utf-8"))
        root = tree.root_node
        if root.has_error:
            raw.error = "tree-sitter 解析含语法错误节点"
        self._collect_symbols(root, source, raw)
        self._collect_imports(root, source, raw)
        self._collect_calls(root, source, raw)
        return raw

    # ---------- 符号 ----------

    def _collect_symbols(self, root, source: str, raw: RawFile) -> None:
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == self._cfg["class"]:
                self._collect_class(node, source, raw)
                continue  # 类内方法归类符号；不重复当函数
            if node.type in self._cfg["function"]:
                self._collect_function(node, source, raw, class_name=None)
            for c in node.children:
                stack.append(c)

    def _collect_class(self, node, source: str, raw: RawFile) -> None:
        name = self._node_text(node.child_by_field_name("name"), source)
        cls = RawSymbol(kind="class", name=name,
                        lineno=node.start_point[0] + 1,
                        end_lineno=node.end_point[0] + 1)
        body = node.child_by_field_name("body")
        if body:
            stack = [body]
            while stack:
                n = stack.pop()
                if n.type in (self._cfg["method"],):
                    mname = self._node_text(n.child_by_field_name("name"), source)
                    if mname:
                        cls.methods.append(mname)
                        raw.symbols.append(RawSymbol(
                            kind="function", name=name + "." + mname,
                            lineno=n.start_point[0] + 1,
                            end_lineno=n.end_point[0] + 1, class_name=name))
                for c in n.children:
                    stack.append(c)
        raw.symbols.append(cls)

    def _collect_function(self, node, source: str, raw: RawFile, class_name) -> None:
        name = self._node_text(node.child_by_field_name("name"), source)
        if not name:
            return
        raw.symbols.append(RawSymbol(
            kind="function", name=name,
            lineno=node.start_point[0] + 1,
            end_lineno=node.end_point[0] + 1, class_name=class_name))

    # ---------- 导入 ----------

    def _collect_imports(self, root, source: str, raw: RawFile) -> None:
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "import_statement":
                self._parse_import_statement(node, source, raw)
            elif node.type == "call_expression":
                fn = node.child_by_field_name("function")
                if fn and self._node_text(fn, source).strip() == "require":
                    args = node.child_by_field_name("arguments")
                    if args and args.named_children:
                        m = self._node_text(args.named_children[0], source).strip('"'"'"'')
                        if m:
                            raw.imports.append(RawImport(
                                kind="module", module=m,
                                local=m.split("/")[-1].split(".")[0]))
            for c in node.children:
                stack.append(c)

    def _parse_import_statement(self, node, source: str, raw: RawFile) -> None:
        txt = self._node_text(node, source)
        m = re.search(r"from\s+[\"']([^\"']+)[\"']", txt)
        if not m:
            m = re.search(r"require\([\"']([^\"']+)[\"']\)", txt)
        if not m:
            return
        mod = m.group(1)
        sm = re.search(r"import\s+\{([^}]+)\}", txt)
        if sm:
            for part in sm.group(1).split(","):
                part = part.strip()
                if not part:
                    continue
                local = part.split(" as ")[-1].strip() if " as " in part else part
                sym = part.split(" as ")[0].strip()
                raw.imports.append(RawImport(
                    kind="symbol", module=mod, symbol=sym, local=local))
            return
        dm = re.search(r"import\s+([A-Za-z_$][\w$]*)", txt)
        if dm:
            raw.imports.append(RawImport(
                kind="symbol", module=mod, symbol="default", local=dm.group(1)))
        elif "import" in txt and "from" not in txt:
            raw.imports.append(RawImport(kind="module", module=mod))

    # ---------- 调用 ----------

    def _collect_calls(self, root, source: str, raw: RawFile) -> None:
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in self._cfg["function"]:
                self._calls_in_function(node, source, raw)
            for c in node.children:
                stack.append(c)

    def _calls_in_function(self, fnode, source: str, raw: RawFile) -> None:
        fname = self._node_text(fnode.child_by_field_name("name"), source)
        if not fname:
            return
        calls = []
        stack = [fnode]
        while stack:
            node = stack.pop()
            if node.type == self._cfg["call"]:
                fn = node.child_by_field_name("function")
                if fn:
                    callee = self._node_text(fn, source).strip()
                    args = node.child_by_field_name("arguments")
                    calls.append(RawCall(
                        callee=callee, lineno=node.start_point[0] + 1,
                        args_count=len(args.named_children) if args else 0))
            for c in node.children:
                stack.append(c)
        if calls:
            raw.calls.setdefault(fname, []).extend(calls)

    # ---------- 工具 ----------

    @staticmethod
    def _node_text(node, source: str) -> str:
        if node is None:
            return ""
        return source[node.start_byte:node.end_byte]


# ========== 注册表 ==========

_EXT_TO_LANG = {
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}

SUPPORTED_EXTS = frozenset([".py"] + list(_EXT_TO_LANG.keys()))

SUPPORTED_EXTS = frozenset([".py"] + list(_EXT_TO_LANG.keys()))

_PROVIDERS = {}


def provider_for(rel_path: str):
    """按文件扩展名返回 Tree-sitter 解析器（None = 不支持）。"""
    ext = rel_path.rsplit(".", 1)[-1]
    lang = _EXT_TO_LANG.get("." + ext)
    if not lang:
        return None
    if lang not in _PROVIDERS:
        _PROVIDERS[lang] = TreeSitterProvider(lang)
    return _PROVIDERS[lang]
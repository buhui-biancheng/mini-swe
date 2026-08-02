"""GraphBuilder：AST 扫描全代码库，构建加权有向图。

构建内容：
1. 节点提取：文件 / 类 / 函数方法 / 全局变量 / IO 资源
2. 调用边 + 导入边 + 继承边
3. 数据流边（级别 1 嵌套调用天然覆盖 + 级别 2 函数内赋值链）
4. 全局变量数据边（模块级赋值 → 函数体 Name 引用匹配）
5. AST 盲区处理：多态全笼罩 / importlib 全笼罩 / 反射打标签

确定性保证：纯语法静态扫描，不依赖运行时；边去重、遍历有序。
"""

import ast
import os
from dataclasses import dataclass, field
from typing import Optional

from .config import AgentConfig
from .models import (
    Edge,
    EdgeType,
    GraphData,
    GraphMeta,
    Node,
    NodeType,
)


@dataclass
class _Symbol:
    """节点符号信息（扫描中间态）。"""
    kind: str  # "file" | "class" | "function" | "global"
    node_id: str
    name: str
    class_name: Optional[str] = None
    lineno: int = 0
    end_lineno: int = 0
    is_async: bool = False
    in_degree: int = 0
    is_reflection: bool = False


@dataclass
class _ImportBinding:
    """导入绑定：局部名 → 目标。"""
    kind: str  # "module" | "symbol"
    module_path: str  # 目标模块路径（已解析为绝对模块路径）
    symbol: Optional[str] = None  # from-import 的符号名


@dataclass
class _FileInfo:
    """单个文件的扫描信息。"""
    rel_path: str
    abs_path: str
    source: str
    tree: ast.Module
    module_path: str
    symbols: list = field(default_factory=list)
    symbols_by_name: dict = field(default_factory=dict)  # name -> [_Symbol]
    class_methods: dict = field(default_factory=dict)    # 类 node_id -> [方法名]
    global_names: set = field(default_factory=set)       # 模块级赋值名
    imports: dict = field(default_factory=dict)          # 局部名 -> _ImportBinding
    imported_module_paths: list = field(default_factory=list)  # 已导入的模块路径（含前缀）
    error: Optional[str] = None


class GraphBuilder:
    """AST 扫描建图。"""

    def __init__(self, code_dir: str, config: Optional[AgentConfig] = None):
        self.code_dir = os.path.abspath(code_dir)
        self.config = config or AgentConfig()
        self._files: dict[str, _FileInfo] = {}       # rel_path -> info
        self._method_index: dict[str, list[str]] = {}  # 方法名 -> [node_id]（全笼罩用）
        self._all_node_ids: set = set()
        self._edges: list = []                        # (source, target, EdgeType)
        self._edge_keys: set = set()

    # ========== 公共入口 ==========

    @property
    def is_loaded(self) -> bool:
        """Builder 是否已持有全量符号表（供增量更新前置判断）。"""
        return bool(self._files)

    def build(self) -> GraphData:
        """全量建图。"""
        self._files.clear()
        self._method_index.clear()
        self._all_node_ids.clear()
        self._edges.clear()
        self._edge_keys.clear()

        self._scan_files()
        self._build_method_index()
        self._build_edges()
        self._compute_in_degree()
        return self._to_graph()

    def update(self, changed_files: list[str]) -> Optional[GraphData]:
        """增量更新：只重新解析变更文件。

        注意：单文件增量无法完整重算跨文件边（导入目标/多态全笼罩），
        因此采用"全图 + 变更文件覆盖"策略：重新解析变更文件，
        移除其作为 source 的边并重建。跨文件引用仍依赖既有符号表。
        """
        changed = [os.path.relpath(f, self.code_dir) if os.path.isabs(f) else f
                   for f in changed_files]
        for rel in changed:
            abs_path = os.path.join(self.code_dir, rel)
            if not os.path.exists(abs_path):
                # 文件被删除 → 移除旧符号
                old = self._files.pop(rel, None)
                if old:
                    for sym in old.symbols:
                        self._all_node_ids.discard(sym.node_id)
                continue
            self._reparse_file(rel)
        self._rebuild_all_edges()
        self._compute_in_degree()
        return self._to_graph()

    def _to_graph(self) -> GraphData:
        """符号表 → GraphData。"""
        graph = GraphData(
            meta=GraphMeta(
                git_commit=self._get_git_head(),
                code_dir=self.code_dir,
            ),
            nodes={},
            edges=[],
        )
        for fi in self._files.values():
            for sym in fi.symbols:
                graph.nodes[sym.node_id] = Node(
                    node_id=sym.node_id,
                    file=fi.rel_path,
                    name=sym.name,
                    node_type=self._symbol_type(sym.kind),
                    lineno=sym.lineno,
                    end_lineno=sym.end_lineno,
                    in_degree=sym.in_degree,
                    is_reflection=sym.is_reflection,
                )
        graph.edges = [Edge(source=s, target=t, edge_type=tp)
                       for s, t, tp in self._edges]
        graph.refresh_stats()
        return graph

    # ========== 扫描 ==========

    def _scan_files(self) -> None:
        """递归扫描所有 .py 文件。"""
        for dirpath, dirnames, filenames in os.walk(self.code_dir):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith('.') and d != '__pycache__' and d != 'venv'
            ]
            for filename in filenames:
                if not filename.endswith('.py'):
                    continue
                abs_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(abs_path, self.code_dir)
                self._parse_file(rel_path)

    def _parse_file(self, rel_path: str) -> None:
        """解析单个文件，提取符号与导入。"""
        abs_path = os.path.join(self.code_dir, rel_path)
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                source = f.read()
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError) as e:
            fi = _FileInfo(
                rel_path=rel_path, abs_path=abs_path, source="",
                tree=ast.Module(body=[]), module_path=self._module_path(rel_path),
                error=str(e),
            )
            fi.symbols.append(_Symbol(
                kind="file", node_id=rel_path, name=os.path.basename(rel_path),
                lineno=1, end_lineno=1,
            ))
            self._all_node_ids.add(rel_path)
            self._files[rel_path] = fi
            return

        fi = _FileInfo(
            rel_path=rel_path,
            abs_path=abs_path,
            source=source,
            tree=tree,
            module_path=self._module_path(rel_path),
        )
        fi.symbols.append(_Symbol(
            kind="file", node_id=rel_path, name=os.path.basename(rel_path),
            lineno=1, end_lineno=len(source.splitlines()),
        ))
        self._all_node_ids.add(rel_path)

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sym = self._function_symbol(fi, node, class_name=None)
                fi.symbols.append(sym)
                fi.symbols_by_name.setdefault(node.name, []).append(sym)
            elif isinstance(node, ast.ClassDef):
                class_sym = _Symbol(
                    kind="class", node_id=f"{rel_path}::{node.name}",
                    name=node.name, lineno=node.lineno,
                    end_lineno=self._end_line(node),
                )
                fi.symbols.append(class_sym)
                fi.symbols_by_name.setdefault(node.name, []).append(class_sym)
                self._all_node_ids.add(class_sym.node_id)
                fi.class_methods.setdefault(class_sym.node_id, [])
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        m_sym = self._function_symbol(fi, child, class_name=node.name)
                        fi.symbols.append(m_sym)
                        fi.symbols_by_name.setdefault(
                            f"{node.name}.{child.name}", []
                        ).append(m_sym)
                        fi.class_methods[class_sym.node_id].append(child.name)
            # 模块级赋值 → 全局变量（创建全局节点）
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and not t.id.startswith("_"):
                        fi.global_names.add(t.id)
                        g_sym = _Symbol(
                            kind="global",
                            node_id=f"{rel_path}::{t.id}",
                            name=t.id,
                            lineno=node.lineno,
                            end_lineno=node.end_lineno or node.lineno,
                        )
                        fi.symbols.append(g_sym)
                        self._all_node_ids.add(g_sym.node_id)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                if isinstance(node.target, ast.Name):
                    fi.global_names.add(node.target.id)
                    g_sym = _Symbol(
                        kind="global",
                        node_id=f"{rel_path}::{node.target.id}",
                        name=node.target.id,
                        lineno=node.lineno,
                        end_lineno=node.end_lineno or node.lineno,
                    )
                    fi.symbols.append(g_sym)
                    self._all_node_ids.add(g_sym.node_id)

        # 导入处理
        self._collect_imports(fi, tree)
        self._files[rel_path] = fi

    def _function_symbol(self, fi: _FileInfo, node, class_name: Optional[str]) -> _Symbol:
        """提取函数/方法符号。"""
        name = f"{class_name}.{node.name}" if class_name else node.name
        sym = _Symbol(
            kind="function",
            node_id=f"{fi.rel_path}::{name}",
            name=name,
            class_name=class_name,
            lineno=node.lineno,
            end_lineno=self._end_line(node),
            is_async=isinstance(node, ast.AsyncFunctionDef),
        )
        self._all_node_ids.add(sym.node_id)
        return sym

    def _collect_imports(self, fi: _FileInfo, tree: ast.Module) -> None:
        """收集导入绑定。"""
        pkg_prefix = fi.module_path.rsplit(".", 1)[0] if "." in fi.module_path else ""

        def resolve_relative(base: str) -> Optional[str]:
            """把相对导入基座解析为绝对模块路径。"""
            if not base or base == ".":
                return pkg_prefix or fi.module_path
            dots = len(base) - len(base.lstrip("."))
            parts = base.lstrip(".").split(".")
            mod = (pkg_prefix.split(".")[:dots or 1] or [])[: dots or 1]
            # 向上跳 dots-1 层再拼 parts
            depth = dots - 1
            prefix = pkg_prefix.split(".")
            if depth >= 0:
                prefix = prefix[: max(0, len(prefix) - depth)]
            return ".".join(prefix + parts)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
                    local = alias.asname or mod.split(".")[0]
                    fi.imports[local] = _ImportBinding(
                        kind="module", module_path=mod,
                    )
                    # 记录所有前缀模块路径（供属性链解析）
                    parts = mod.split(".")
                    for i in range(1, len(parts) + 1):
                        p = ".".join(parts[:i])
                        if p not in fi.imported_module_paths:
                            fi.imported_module_paths.append(p)
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    base = "." * node.level + (node.module or "")
                    mod = resolve_relative(base)
                else:
                    mod = node.module or ""
                for alias in node.names:
                    local = alias.asname or alias.name
                    fi.imports[local] = _ImportBinding(
                        kind="symbol", module_path=mod, symbol=alias.name,
                    )
                    if mod not in fi.imported_module_paths:
                        fi.imported_module_paths.append(mod)

    def _reparse_file(self, rel_path: str) -> None:
        """重新解析变更文件（增量更新）。"""
        old = self._files.get(rel_path)
        if old:
            for sym in old.symbols:
                self._all_node_ids.discard(sym.node_id)
        self._parse_file(rel_path)

    # ========== 辅助 ==========

    @staticmethod
    def _module_path(rel_path: str) -> str:
        """文件相对路径 → 模块路径。"""
        base = rel_path[:-3] if rel_path.endswith(".py") else rel_path
        if base.endswith("__init__"):
            base = base.rsplit("/", 1)[0]
        return base.replace("/", ".")

    def _module_to_file(self, module_path: str) -> Optional[str]:
        """模块路径 → 仓库相对文件路径。"""
        rel_dir = module_path.replace(".", "/")
        for cand in (f"{rel_dir}.py", f"{rel_dir}/__init__.py"):
            abs_cand = os.path.join(self.code_dir, cand)
            if os.path.exists(abs_cand):
                return cand
        return None

    def _end_line(self, node: ast.AST) -> int:
        if hasattr(node, 'end_lineno') and node.end_lineno:
            return node.end_lineno
        max_line = getattr(node, 'lineno', 1)
        for child in ast.walk(node):
            if hasattr(child, 'lineno'):
                max_line = max(max_line, child.lineno)
        return max_line

    def _get_git_head(self) -> str:
        try:
            import subprocess
            r = subprocess.run(
                ["git", "-C", self.code_dir, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return r.stdout.strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _symbol_type(kind: str) -> NodeType:
        return {
            "file": NodeType.FILE,
            "class": NodeType.CLASS,
            "function": NodeType.FUNCTION,
            "global": NodeType.GLOBAL,
        }.get(kind, NodeType.FUNCTION)

    def _symbol_node_type(self, node_id: str) -> Optional[NodeType]:
        """按 node_id 查找符号的节点类型。"""
        for fi in self._files.values():
            for sym in fi.symbols:
                if sym.node_id == node_id:
                    return self._symbol_type(sym.kind)
        return None

    def _build_method_index(self) -> None:
        """全局方法名索引（多态全笼罩用）。"""
        self._method_index.clear()
        for fi in self._files.values():
            for cls_id, methods in fi.class_methods.items():
                for m in methods:
                    method_node = f"{cls_id}.{m}"
                    self._method_index.setdefault(m, []).append(method_node)

    def _add_edge(self, source: str, target: str, edge_type: EdgeType) -> None:
        """去重添加边。

        保留自环（A→A 递归）：去环由 GraphIndex 的 visited 集合处理，
        边本身应存在以反映递归调用关系。
        """
        key = (source, target, edge_type)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self._edges.append((source, target, edge_type))

    # ========== 建边 ==========

    def _build_edges(self) -> None:
        for fi in self._files.values():
            if fi.error:
                continue
            # 导入边
            self._build_import_edges(fi)
            # 文件内符号关系：继承 + 调用 + 数据流 + 全局引用
            for sym in fi.symbols:
                if sym.kind == "class":
                    self._build_class_edges(fi, sym)
                elif sym.kind == "function":
                    self._build_function_edges(fi, sym)

    def _build_import_edges(self, fi: _FileInfo) -> None:
        """导入边：文件 → 目标模块文件 / 目标符号。"""
        seen = set()
        for binding in fi.imports.values():
            mod_file = self._module_to_file(binding.module_path)
            if mod_file:
                self._add_edge(fi.rel_path, mod_file, EdgeType.IMPORT)
                seen.add(mod_file)
            if binding.kind == "symbol" and binding.symbol:
                if mod_file:
                    target = f"{mod_file}::{binding.symbol}"
                    if target in self._all_node_ids:
                        self._add_edge(fi.rel_path, target, EdgeType.IMPORT)

    def _build_class_edges(self, fi: _FileInfo, sym: _Symbol) -> None:
        """继承边 + 类体内直接方法调用。"""
        cls_node = next((n for n in fi.tree.body
                         if isinstance(n, ast.ClassDef) and n.name == sym.name), None)
        if not cls_node:
            return
        for base in cls_node.bases:
            targets = self._resolve_expr_name_targets(fi, None, base)
            for t in targets:
                if t != sym.node_id:
                    self._add_edge(sym.node_id, t, EdgeType.INHERIT)

    def _build_function_edges(self, fi: _FileInfo, sym: _Symbol) -> None:
        """函数体内的调用边 + 数据流边 + 全局引用边 + 反射标记。"""
        func_node = self._find_function_node(fi, sym)
        if not func_node:
            return

        is_reflection = [False]

        # 收集局部遮蔽名（参数 + 赋值目标）
        shadowed = self._collect_local_names(func_node, sym)

        # ---- 全局变量引用边（独立 pass）----
        self._build_global_ref_edges(fi, sym, func_node, shadowed)

        # ---- 调用 + 数据流（有序 walk）----
        var_sources: dict[str, set] = {}
        for node, depth in self._walk_statements(func_node, 0):
            if isinstance(node, ast.Call):
                self._handle_call(fi, sym, node, var_sources, shadowed, is_reflection)
            elif isinstance(node, ast.Assign):
                self._handle_assign(fi, sym, node, var_sources, depth)
            elif isinstance(node, (ast.AugAssign, ast.Delete)):
                for t in node.targets if isinstance(node, ast.Delete) else [node.target]:
                    if isinstance(t, ast.Name):
                        var_sources.pop(t.id, None)

        if is_reflection[0]:
            for s in fi.symbols:
                if s.node_id == sym.node_id:
                    s.is_reflection = True

    def _find_function_node(self, fi: _FileInfo, sym: _Symbol) -> Optional[ast.FunctionDef]:
        """根据符号定位 AST 节点（跳过已处理过的）。"""
        for node in ast.walk(fi.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.lineno == sym.lineno and node.end_lineno == sym.end_lineno:
                    return node
        return None

    def _collect_local_names(self, func_node: ast.FunctionDef, sym: _Symbol) -> set:
        """收集函数内被遮蔽的局部名（参数 + 赋值目标）。"""
        names = set()
        if func_node.args.posonlyargs:
            for a in func_node.args.posonlyargs:
                names.add(a.arg)
        if func_node.args.args:
            for a in func_node.args.args:
                names.add(a.arg)
        if func_node.args.kwonlyargs:
            for a in func_node.args.kwonlyargs:
                names.add(a.arg)
        if func_node.args.vararg:
            names.add(func_node.args.vararg.arg)
        if func_node.args.kwarg:
            names.add(func_node.args.kwarg.arg)
        for node in ast.walk(func_node):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                if isinstance(node.target, ast.Name):
                    names.add(node.target.id)
        return names

    def _build_global_ref_edges(self, fi: _FileInfo, sym: _Symbol,
                                func_node, shadowed: set) -> None:
        """全局变量引用边：函数体 Name 匹配全局变量。"""
        for node in ast.walk(func_node):
            if not isinstance(node, ast.Name) or node.id in shadowed:
                continue
            if not isinstance(node.ctx, ast.Load):
                continue
            name = node.id
            # 同文件全局
            if name in fi.global_names:
                self._add_edge(sym.node_id, f"{fi.rel_path}::{name}", EdgeType.GLOBAL)
            # 导入的符号是全局变量（函数/类由调用边覆盖，不建 global 边）
            binding = fi.imports.get(name)
            if binding and binding.kind == "symbol":
                mod_file = self._module_to_file(binding.module_path)
                if mod_file:
                    target = f"{mod_file}::{binding.symbol}"
                    if target in self._all_node_ids:
                        target_node_type = self._symbol_node_type(target)
                        if target_node_type == NodeType.GLOBAL:
                            self._add_edge(sym.node_id, target, EdgeType.GLOBAL)

    # ---- 有序遍历 ----

    def _walk_statements(self, node, depth: int):
        """按源码顺序遍历，产出 (node, 分支深度)。

        赋值语句也递归进入，以便发现 value 中的嵌套调用（x = f() 的 f 调用边）。
        """
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Assign, ast.AugAssign, ast.Delete)):
                yield child, depth
                yield from self._walk_statements(child, depth)
            elif isinstance(child, ast.Call):
                yield child, depth
                yield from self._walk_statements(child, depth)
            else:
                if isinstance(child, (ast.For, ast.AsyncFor, ast.While,
                                      ast.If, ast.Try, ast.With, ast.AsyncWith,
                                      ast.comprehension, ast.match_case)):
                    new_depth = depth + 1
                else:
                    new_depth = depth
                yield from self._walk_statements(child, new_depth)

    # ---- 调用处理 ----

    def _handle_call(self, fi: _FileInfo, sym: _Symbol, node: ast.Call,
                     var_sources: dict, shadowed: set, is_reflection: list) -> None:
        """处理单个 Call：调用边 + 数据流边 + 反射标记。"""
        targets = self._resolve_call_targets(fi, sym, node.func, var_sources, shadowed)
        for t in targets:
            self._add_edge(sym.node_id, t, EdgeType.CALL)

        # 反射检测
        callee_name = self._callee_name(node.func)
        # getattr 的直接调用或立即调用 getattr(...)()
        getattr_args = None
        if callee_name == "getattr":
            getattr_args = node.args
        elif (isinstance(node.func, ast.Call)
              and self._callee_name(node.func.func) == "getattr"):
            getattr_args = node.func.args
        if getattr_args is not None:
            if len(getattr_args) >= 2 and isinstance(getattr_args[1], ast.Constant):
                # 硬编码字符串 → 全笼罩该名字的方法
                attr_name = getattr_args[1].value
                for t in self._method_full_coverage(attr_name):
                    self._add_edge(sym.node_id, t, EdgeType.CALL)
            elif len(getattr_args) >= 2:
                # 动态字符串 → 打标签
                is_reflection[0] = True
        if callee_name in ("importlib.import_module", "__import__"):
            if node.args and isinstance(node.args[0], ast.Constant):
                mod_file = self._module_to_file(str(node.args[0].value))
                if mod_file:
                    self._add_edge(sym.node_id, mod_file, EdgeType.IMPORT)
            else:
                is_reflection[0] = True

        # IO 边
        if callee_name in self.config.io_keywords or any(
            callee_name.endswith(k) for k in (".read", ".write", ".readlines", ".writelines")
        ):
            self._add_edge(sym.node_id, f"__io__::{callee_name}", EdgeType.IO)

        # 数据流边：参数是变量（有来源）或嵌套调用
        for arg in list(node.args) + [k.value for k in node.keywords]:
            if isinstance(arg, ast.Name) and arg.id in var_sources:
                for src in var_sources[arg.id]:
                    for t in targets:
                        self._add_edge(src, t, EdgeType.DATA)
            elif isinstance(arg, ast.Call):
                inner_targets = self._resolve_call_targets(
                    fi, sym, arg.func, var_sources, shadowed)
                for inner in inner_targets:
                    for t in targets:
                        self._add_edge(inner, t, EdgeType.DATA)

    def _handle_assign(self, fi: _FileInfo, sym: _Symbol, node: ast.Assign,
                       var_sources: dict, depth: int) -> None:
        """处理赋值：更新变量来源表。"""
        value = node.value
        for t in node.targets:
            if not isinstance(t, ast.Name):
                continue
            if isinstance(value, ast.Call):
                new_sources = set(self._resolve_call_targets(
                    fi, sym, value.func, var_sources, set()))
                if depth > 0 and t.id in var_sources:
                    var_sources[t.id] |= new_sources  # 分支全笼罩
                else:
                    var_sources[t.id] = new_sources
            elif isinstance(value, ast.Name) and value.id in var_sources:
                if depth > 0 and t.id in var_sources:
                    var_sources[t.id] |= var_sources[value.id]
                else:
                    var_sources[t.id] = set(var_sources[value.id])
            else:
                var_sources[t.id] = set()  # 非调用赋值 → 来源清空

    # ---- 调用目标解析 ----

    def _callee_name(self, func) -> str:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            base = self._callee_name(func.value)
            return f"{base}.{func.attr}" if base else func.attr
        if isinstance(func, ast.Call):
            # getattr(obj, name)() 这类立即调用，取内部函数名
            return self._callee_name(func.func)
        return ""

    def _resolve_call_targets(self, fi: _FileInfo, sym: _Symbol, callee,
                              var_sources: dict, shadowed: set) -> list:
        """解析调用目标节点 id 列表。"""
        if isinstance(callee, ast.Name):
            return self._resolve_name_targets(fi, sym, callee.id, var_sources, shadowed)
        if isinstance(callee, ast.Attribute):
            return self._resolve_attribute_targets(fi, sym, callee, var_sources, shadowed)
        return []

    def _resolve_name_targets(self, fi: _FileInfo, sym: _Symbol, name: str,
                              var_sources: dict, shadowed: set) -> list:
        """解析 Name 形式的调用目标。"""
        targets = []
        # 1. 同文件函数 / 类
        for local in fi.symbols_by_name.get(name, []):
            if local.kind in ("function", "class"):
                targets.append(local.node_id)
        # 2. 导入绑定
        binding = fi.imports.get(name)
        if binding:
            mod_file = self._module_to_file(binding.module_path)
            if binding.kind == "module" and mod_file:
                targets.append(mod_file)
            elif binding.kind == "symbol" and mod_file and binding.symbol:
                target = f"{mod_file}::{binding.symbol}"
                if target in self._all_node_ids:
                    targets.append(target)
        # 3. 局部变量持有函数引用（f = some_func; f()）
        if name in var_sources:
            for src in var_sources[name]:
                if src in self._all_node_ids:
                    targets.append(src)
        return list(dict.fromkeys(targets))

    def _resolve_attribute_targets(self, fi: _FileInfo, sym: _Symbol, callee: ast.Attribute,
                                   var_sources: dict, shadowed: set) -> list:
        """解析 Attribute 形式的调用目标（含多态全笼罩）。"""
        # 取链上各部分
        parts = []
        node: ast.AST = callee
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        final_attr = callee.attr

        # 链根是 Name
        if isinstance(node, ast.Name):
            root = node.id
            parts.reverse()
            dotted = ".".join([root] + parts)

            # self/cls → 当前类方法
            if root in ("self", "cls") and sym.class_name:
                target = f"{fi.rel_path}::{sym.class_name}.{final_attr}"
                if target in self._all_node_ids:
                    return [target]

            # 同文件类静态方法：ClassName.method()
            if root in [s.name for s in fi.symbols_by_name.get(root, [])
                        if s.kind == "class"]:
                target = f"{fi.rel_path}::{root}.{final_attr}"
                if target in self._all_node_ids:
                    return [target]

            # root 是模块别名（import a.b as c; c.func() → 用 module_path 解析）
            binding = fi.imports.get(root)
            if binding and binding.kind == "module":
                # parts 不含 root（如 ['add'] 或 ['b','c']），remainder 是模块后的属性链
                remainder = ".".join(parts)
                mod_file = self._module_to_file(binding.module_path)
                if mod_file:
                    if remainder:
                        resolved = self._resolve_module_symbol(mod_file, remainder)
                        if resolved:
                            return resolved
                    else:
                        return [mod_file]

            # 模块属性链解析（最长前缀匹配导入的模块路径）
            match = None
            for mp in sorted(fi.imported_module_paths, key=len, reverse=True):
                if dotted == mp or dotted.startswith(mp + "."):
                    match = mp
                    break
            if match:
                mod_file = self._module_to_file(match)
                if mod_file:
                    remainder = dotted[len(match):].lstrip(".")
                    if remainder:
                        resolved = self._resolve_module_symbol(mod_file, remainder)
                        if resolved:
                            return resolved
                    else:
                        return [mod_file]

            # 局部变量持有对象的方法（全笼罩：var_sources 里的类名）
            if root in var_sources:
                found = []
                for src in var_sources[root]:
                    # src 形如 "file.py::Circle"（类节点），方法节点是 "file.py::Circle.area"
                    target = f"{src}.{final_attr}"
                    if target in self._all_node_ids:
                        found.append(target)
                if found:
                    return found

        # super().method() → 父类方法
        if isinstance(node, ast.Call) and self._callee_name(node.func) == "super":
            parent_targets = self._resolve_super_targets(fi, sym, final_attr)
            if parent_targets:
                return parent_targets

        # 兜底：多态全笼罩（方法名 → 所有类中同名方法）
        return self._method_full_coverage(final_attr)

    def _resolve_module_symbol(self, mod_file: str, remainder: str) -> list:
        """模块内符号解析：module.file 的剩余路径。"""
        # 尝试直接 node_id（函数/类/全局）
        direct = f"{mod_file}::{remainder}"
        if direct in self._all_node_ids:
            return [direct]
        # 尝试类方法全路径（如 Config.method）
        if "." in remainder:
            cls_part, _, method = remainder.rpartition(".")
            cls_id = f"{mod_file}::{cls_part}"
            if cls_id in self._all_node_ids:
                target = f"{cls_id}.{method}"
                if target in self._all_node_ids:
                    return [target]
        return []

    def _resolve_super_targets(self, fi: _FileInfo, sym: _Symbol, method: str) -> list:
        """super().method() → 父类同名方法。"""
        if not sym.class_name:
            return []
        cls_node = next((n for n in fi.tree.body
                         if isinstance(n, ast.ClassDef) and n.name == sym.class_name), None)
        if not cls_node:
            return []
        result = []
        for base in cls_node.bases:
            base_targets = self._resolve_expr_name_targets(fi, None, base)
            for bt in base_targets:
                target = f"{bt}.{method}"
                if target in self._all_node_ids:
                    result.append(target)
        return result

    def _resolve_expr_name_targets(self, fi: _FileInfo, sym: Optional[_Symbol],
                                   expr) -> list:
        """解析任意表达式对应的节点 id（继承基类 / 类名引用等）。"""
        if isinstance(expr, ast.Name):
            out = []
            for s in fi.symbols_by_name.get(expr.id, []):
                if s.kind == "class":
                    out.append(s.node_id)
            binding = fi.imports.get(expr.id)
            if binding and binding.kind == "symbol":
                mod_file = self._module_to_file(binding.module_path)
                if mod_file:
                    target = f"{mod_file}::{binding.symbol}"
                    if target in self._all_node_ids:
                        out.append(target)
            return out
        if isinstance(expr, ast.Attribute):
            parts = []
            node: ast.AST = expr
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                parts.reverse()
                dotted = ".".join([node.id] + parts)
                for mp in sorted(fi.imported_module_paths, key=len, reverse=True):
                    if dotted == mp or dotted.startswith(mp + "."):
                        mod_file = self._module_to_file(mp)
                        if mod_file:
                            remainder = dotted[len(mp):].lstrip(".")
                            return self._resolve_module_symbol(mod_file, remainder)
        return []

    def _method_full_coverage(self, method: str) -> list:
        """多态全笼罩：所有定义该方法的类方法节点。"""
        return self._method_index.get(method, [])[:self.config.max_polymorphism_edges]

    # ========== 入度 ==========

    def _compute_in_degree(self) -> None:
        """统计入度并写回节点符号（重新构建时随 build 落盘）。"""
        in_degree: dict[str, int] = {}
        for _, target, tp in self._edges:
            if tp in (EdgeType.IMPORT, EdgeType.IO):
                continue  # 导入/IO 边不计入函数入度
            in_degree[target] = in_degree.get(target, 0) + 1
        # 写回符号表（供 build/update 落盘）
        for fi in self._files.values():
            for sym in fi.symbols:
                sym.in_degree = in_degree.get(sym.node_id, 0)

    # ========== 增量更新 ==========

    def _rebuild_all_edges(self) -> None:
        """增量更新后重建所有边。"""
        self._method_index.clear()
        self._build_method_index()
        self._edges.clear()
        self._edge_keys.clear()
        self._build_edges()

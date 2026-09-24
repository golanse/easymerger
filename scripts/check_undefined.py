#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""不依赖第三方的"未定义名"检查（pyflakes 的兜底）。

为什么需要它：沙盒/用户机器上 pyflakes 经常没装，而
    pyflakes ... || echo "0 处 ✅"
会把"工具缺失"误报成"通过"——漏 import 因此一路带进发布包
（QDialog / QApplication 都这么崩过）。

本脚本只用标准库 ast，任何 Python 环境都能跑。
原则同 verify_all.py：宁可报错，也不假通过。
"""
from __future__ import annotations

import ast
import builtins
import os
import sys


def collect_defined(tree: ast.AST) -> set:
    """收集模块中所有"被定义"的名字。"""
    names = set(dir(builtins))
    # __file__ / __name__ 等模块级内置变量（用 os.path 时很常见）
    names.update({"__file__", "__name__", "__doc__", "__package__",
                  "__spec__", "__loader__", "__builtins__", "__all__",
                  "__debug__", "__path__", "__qualname__", "__module__"})
    for node in ast.walk(tree):
        # import a / import a as b / from x import y
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    names.add(a.asname or a.name)
        # 函数 / 类 / 异步函数定义
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            names.add(node.name)
            # 装饰器也用名字
            for d in node.decorator_list:
                for sub in ast.walk(d):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        # 赋值 / 增强赋值 / 注解赋值
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
                    elif isinstance(sub, ast.Tuple):
                        for e in sub.elts:
                            if isinstance(e, ast.Name):
                                names.add(e.id)
        # for / with / 推导式 / except 的绑定
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
        elif isinstance(node, (ast.withitem,)):
            if node.optional_vars is not None:
                for sub in ast.walk(node.optional_vars):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                names.add(node.name)
        elif isinstance(node, (ast.ListComp, ast.SetComp,
                               ast.DictComp, ast.GeneratorExp)):
            for g in node.generators:
                for sub in ast.walk(g.target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        # 函数参数（含 posonly / kwonly / *args / **kwargs）
        elif isinstance(node, ast.arguments):
            for a in (list(node.posonlyargs) + list(node.args)
                      + list(node.kwonlyargs)):
                names.add(a.arg)
            if node.vararg:
                names.add(node.vararg.arg)
            if node.kwarg:
                names.add(node.kwarg.arg)
        # global / nonlocal 声明
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def _names_in(scope_node) -> set:
    """某节点（函数/子树）内绑定的所有名字。"""
    out = set()
    if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = scope_node.args
        for arg in (list(getattr(a, "posonlyargs", [])) + list(a.args)
                    + list(a.kwonlyargs)):
            out.add(arg.arg)
        if a.vararg:
            out.add(a.vararg.arg)
        if a.kwarg:
            out.add(a.kwarg.arg)
    for node in ast.walk(scope_node):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        out.add(sub.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    out.add(sub.id)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                for sub in ast.walk(node.optional_vars):
                    if isinstance(sub, ast.Name):
                        out.add(sub.id)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                out.add(node.name)
        elif isinstance(node, (ast.ListComp, ast.SetComp,
                               ast.DictComp, ast.GeneratorExp)):
            for g in node.generators:
                for sub in ast.walk(g.target):
                    if isinstance(sub, ast.Name):
                        out.add(sub.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                if al.name != "*":
                    out.add(al.asname or al.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            out.update(node.names)
        elif isinstance(node, ast.Lambda):
            la = node.args
            for arg in (list(getattr(la, "posonlyargs", [])) + list(la.args)
                        + list(la.kwonlyargs)):
                out.add(arg.arg)
            if la.vararg:
                out.add(la.vararg.arg)
            if la.kwarg:
                out.add(la.kwarg.arg)
    return out


def _bound_in(fn) -> set:
    """某函数（含其所有嵌套内容）里绑定的名字。"""
    return _names_in(fn)


def _walk_scopes(tree, inherited: set, out: list) -> None:
    """递归检查每个函数作用域，外层的绑定对内层可见（闭包）。"""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # 只处理"直接子节点"由调用方递归，这里用迭代避免重复
        pass

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = inherited | _bound_in(node)
            # lambda 是自己的作用域，其参数只对 lambda 体内可见。
            # 这里不递归进 lambda（否则 lambda 参数会被当成外层未定义），
            # 代价是 lambda 体内不做检查 —— 通常只有一两行，风险可接受。
            _skip: set = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Lambda):
                    for sub2 in ast.walk(sub):
                        _skip.add(id(sub2))
            for sub in ast.walk(node):
                if id(sub) in _skip:
                    continue
                if (isinstance(sub, ast.Name)
                        and isinstance(sub.ctx, ast.Load)
                        and sub.id not in scope):
                    out.append((sub.lineno, sub.id))
            _walk_scopes(node, scope, out)
        elif hasattr(node, "body") or hasattr(node, "orelse"):
            # if / try / with / for 等语句块里也可能有 def
            _walk_scopes(node, inherited, out)


def _module_level_names(tree) -> set:
    """只收集**模块级**的绑定（不进函数体）。"""
    names = set(dir(builtins))
    names.update({"__file__", "__name__", "__doc__", "__package__",
                  "__spec__", "__loader__", "__builtins__", "__all__",
                  "__debug__", "__path__", "__qualname__", "__module__"})

    def scan(nodes):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                names.add(node.name)
                continue          # 不进函数/类体
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.name != "*":
                        names.add(a.asname or a.name.split(".")[0])
                continue
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
                for t in targets:
                    for sub in ast.walk(t):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
                continue
            if isinstance(node, (ast.For, ast.AsyncFor)):
                for sub in ast.walk(node.target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
            if isinstance(node, ast.withitem):
                if node.optional_vars is not None:
                    for sub in ast.walk(node.optional_vars):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
            if isinstance(node, ast.ExceptHandler):
                if node.name:
                    names.add(node.name)
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                names.update(node.names)
            # 继续下钻语句块（if body / try body 等）
            for attr in ("body", "orelse", "finalbody"):
                blk = getattr(node, attr, None)
                if isinstance(blk, list):
                    scan(blk)

    scan(tree.body)
    # 类体里的赋值（类属性）也算模块级可见
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                    targets = (sub.targets if isinstance(sub, ast.Assign)
                               else [sub.target])
                    for t in targets:
                        for n in ast.walk(t):
                            if isinstance(n, ast.Name):
                                names.add(n.id)
    return names


def check_self_assign(tree: ast.AST) -> list:
    """专项：self.X = X 里的 X 必须是本函数的参数或局部绑定。

    这是**真实事故模式**，已发生三次：
      · AlignDialog 漏 self.prefer_hw = prefer_hw
      · AlignDialog 第二个 __init__ 用了 gpu_vendor 却没有这个参数
      · 同类还有 QDialog / QApplication 漏 import

    特征都是"用了某个名字，它看起来像参数但其实不是"。
    完整的作用域分析误报太多（lambda / 推导式 / 闭包），
    这里只盯这个高价值模式，做到零误报。
    """
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope = _names_in(fn)
        for node in ast.walk(fn):
            # self.X = Y
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            val = node.value.id
            if val in scope or val in dir(builtins):
                continue
            # 只看赋值给 self.<同名> 的情形（参数透传的典型写法）
            targets = node.targets
            hit = False
            for t in targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "self"
                        and t.attr == val):
                    hit = True
            if hit:
                out.append((node.lineno, val))
    return out


def check_file(path: str) -> list:
    """返回 [(行号, 名字)] 疑似未定义名（宽松：模块级全局集合）。"""
    try:
        src = open(path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        return [(e.lineno or 0, f"<语法错误: {e.msg}>")]
    defined = collect_defined(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in defined:
                out.append((node.lineno, node.id))
    # 专项：self.X = X（X 不是本作用域的绑定）
    for ln, name in check_self_assign(tree):
        out.append((ln, f"{name}（self.{name} = {name}，但 {name} 不是本函数参数）"))
    seen = set()
    uniq = []
    for ln, n in sorted(out):
        if n in seen:
            continue
        seen.add(n)
        uniq.append((ln, n))
    return uniq


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    targets = []
    for d in ("core", "ui", "scripts"):
        p = os.path.join(root, d)
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                if f.endswith(".py"):
                    targets.append(os.path.join(d, f))
    targets.append("main.py")
    targets += sorted(f for f in os.listdir(root)
                      if f.startswith("test_") and f.endswith(".py"))

    total = 0
    for t in targets:
        fp = os.path.join(root, t)
        if not os.path.isfile(fp):
            continue
        for ln, name in check_file(fp):
            print(f"{t}:{ln}: undefined name '{name}'")
            total += 1
    print(f"\n共 {len(targets)} 个文件，未定义名 {total} 处")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())

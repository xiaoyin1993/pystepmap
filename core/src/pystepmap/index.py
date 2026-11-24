"""项目索引：用 ast 扫描目录，建立模块、函数、类、导入与模块级实例的索引。只读，不执行被分析代码。

模块的点分名相对"源码根"计算（运行时 sys.path 上的目录）。源码根：
- 分析的目录本身；
- 名叫 src、本身不是包（没有 __init__.py）的目录；
- 本身不是包、上一级也不是包的目录，并且它直接包含一个包（有 __init__.py 的子目录，如仓库根下 backend/app 里的
  backend），或者它直接包含的某个模块在项目里别的文件中被按顶层名绝对导入（脚本目录里的 import common）。
配置里给了 source_roots（相对分析目录的路径）时只用它们，不再检测。
每个模块按它所在的每个源码根各登记一个点分名（backend/app/x.py 同时是 app.x 和 backend.app.x），都指向同一个文件；
函数的 key 仍按相对分析目录的路径，不受影响。模块自己的点分名（相对导入按它展开）取包含它的最内层源码根。
分析的目录本身是包（含 __init__.py，如直接分析 backend/app）且是源码根时，它下面的模块还登记在"包名.相对点分名"
下（app.x；目录自己的 __init__.py 就是 app）；包名是目录名，上级目录也含 __init__.py 时继续向上拼（backend.app），
这时包名链的每一段后缀都登记（app.x 和 backend.app.x：运行时 sys.path 上可以是 backend 的上级，也可以是 backend）。
同一个点分名在不同源码根下指向不同文件时：只有一个是所在文件自己的点分名的，登记那一个；否则不登记（记进
ambiguous，解析时记为无法判断，宁可不连线也不连错）。
"""

from __future__ import annotations

import ast
import hashlib
import io
import os
import re
import stat
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .config import ConfigError, ProjectConfig
from .model import fn_key

FuncNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass
class FunctionInfo:
    key: str
    file: str
    qualname: str
    line: int
    end_line: int
    node: FuncNode
    cls: str | None  # 所属类名
    parent: str | None  # 外层函数的 key
    decorators: list[ast.expr]
    doc_title: str  # 文档注释第一行，去掉反引号和 **
    is_async: bool


@dataclass
class ClassInfo:
    name: str
    file: str
    line: int
    bases: list[str]
    decorators: list[ast.expr]
    methods: dict[str, str] = field(default_factory=dict)  # 方法名 -> key
    aliases: dict[str, str] = field(default_factory=dict)  # 类体里 m2 = m1 的别名 -> m1 的 key
    other_bindings: set[str] = field(default_factory=set)  # 类体里除了顶层 def 之外还有别的绑定（赋值、if 里的 def 等）的名字


@dataclass
class ModuleInfo:
    rel: str
    dotted: str
    tree: ast.Module
    lines: list[str]
    imports: dict[str, str] = field(default_factory=dict)  # 本地名 -> 点分全名
    functions: dict[str, str] = field(default_factory=dict)  # 模块级函数名 -> key
    classes: dict[str, ClassInfo] = field(default_factory=dict)
    instances: dict[str, str] = field(default_factory=dict)  # 模块级变量名 -> 类的点分全名
    # 模块级 name = 名字 / 属性 的候选别名 -> 右边（name 在模块里的每次绑定都是这样、右边都一样；其余条件见
    # _module_aliases）；右边是不是项目内函数由解析器判断
    aliases: dict[str, ast.expr] = field(default_factory=dict)
    source_root: str = ""  # 包含它的最内层源码根（相对路径，"" 是分析的目录本身）


@dataclass
class ProjectIndex:
    root: Path
    modules: dict[str, ModuleInfo] = field(default_factory=dict)  # 以相对路径为键
    by_dotted: dict[str, str] = field(default_factory=dict)  # 点分模块名（每个源码根下各一个）-> 相对路径
    functions: dict[str, FunctionInfo] = field(default_factory=dict)  # 以 key 为键
    skipped: list[tuple[str, str]] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)  # 相对路径 -> 内容 sha256
    source_roots: list[str] = field(default_factory=list)  # 相对路径，"" 是分析的目录本身；排序
    ambiguous: set[str] = field(default_factory=set)  # 在不同源码根下指向不同文件、没有登记的点分名
    excluded: dict[str, int] = field(default_factory=dict)  # 实际排除掉的目录名 -> 个数
    package: str = ""  # 分析的目录本身是包、且是源码根时它的点分名（app、pkg.app），否则为空
    classes: list[ClassInfo] = field(default_factory=list)  # 全部类，含嵌套在函数、类里的


def _doc_title(node: FuncNode) -> str:
    doc = ast.get_docstring(node)
    if not doc:
        return ""
    first = doc.strip().splitlines()[0] if doc.strip() else ""
    return first.replace("`", "").replace("**", "").strip()


def _dotted_name(rel: str, source_root: str = "") -> str:
    """rel 相对源码根 source_root 的点分名（source_root 为 "" 时相对分析的目录）。"""
    parts = rel[len(source_root) + 1 if source_root else 0: -3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _under(rel: str, source_root: str) -> bool:
    return not source_root or rel.startswith(source_root + "/")


def _parent(d: str) -> str:
    return d.rpartition("/")[0]


def _absolute_import_tops(tree: ast.Module) -> set[str]:
    """模块里（含函数体里）绝对导入的顶层名：import a.b → a，from a.b import c → a。"""
    tops: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            tops.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            tops.add(n.module.split(".")[0])
    return tops


def _detect_source_roots(files: list[str], trees: dict[str, ast.Module]) -> list[str]:
    """按模块说明里的规则检测源码根。files：全部 .py 文件（含解析失败的）；trees：解析成功的。"""
    packages = {_parent(f) for f in files if f.rpartition("/")[2] == "__init__.py"}
    modules_in: dict[str, list[str]] = {}  # 目录 -> 直接在其中的模块文件（不含 __init__.py）
    dirs: set[str] = set()
    for f in files:
        d, name = f.rpartition("/")[::2]
        if name != "__init__.py":
            modules_in.setdefault(d, []).append(f)
        while d and d not in dirs:
            dirs.add(d)
            d = _parent(d)
    importers: dict[str, set[str]] = {}  # 顶层名 -> 按这个名字绝对导入它的文件
    for rel, tree in trees.items():
        for top in _absolute_import_tops(tree):
            importers.setdefault(top, set()).add(rel)
    roots = {""}
    for d in dirs:
        if d in packages:
            continue
        if d.rpartition("/")[2] == "src":
            roots.add(d)
            continue
        if _parent(d) in packages:  # 包里没有 __init__.py 的子目录是命名空间子包，不是源码根
            continue
        has_package = any(_parent(p) == d for p in packages)
        imported = any(importers.get(f.rpartition("/")[2][:-3], set()) - {f} for f in modules_in.get(d, ()))
        if has_package or imported:
            roots.add(d)
    return sorted(roots)


def _configured_source_roots(root: Path, given: list[str]) -> list[str]:
    out: set[str] = set()
    for raw in given:
        p = PurePosixPath(raw)
        if p.is_absolute() or ".." in p.parts:
            raise ConfigError(f"source_roots 里的路径必须在项目目录之内：{raw}")
        rel = "" if str(p) == "." else str(p)
        if not (root / rel).is_dir():
            raise ConfigError(f"source_roots 里的目录不存在：{raw}")
        out.add(rel)
    return sorted(out)


def _package_name(root: Path) -> str:
    """root 本身是包（含 __init__.py）时它的点分名：目录名，上级目录也是包时继续向上拼，直到不是包为止；不是包时为空。
    只看文件在不在，不读内容。"""
    parts: list[str] = []
    d = root
    while d.name.isidentifier() and (d / "__init__.py").is_file():
        parts.append(d.name)
        d = d.parent
    return ".".join(reversed(parts))


def _package_prefixes(package: str) -> list[str]:
    """包名链的每一段后缀，由短到长：backend.app → app、backend.app；空串没有。"""
    parts = package.split(".") if package else []
    return [".".join(parts[i:]) for i in reversed(range(len(parts)))]


def _register(index: ProjectIndex) -> None:
    """按每个源码根登记点分名（见模块说明）。"""
    names: dict[str, dict[str, bool]] = {}  # 点分名 -> {相对路径: 是否是这个文件自己的点分名}

    def add(name: str, rel: str, own: bool) -> None:
        files = names.setdefault(name, {})
        files[rel] = files.get(rel, False) or own

    prefixes = _package_prefixes(index.package)
    for rel, mod in index.modules.items():
        for sr in index.source_roots:
            if _under(rel, sr):
                name = _dotted_name(rel, sr)
                if name:  # 源码根自己的 __init__.py 没有点分名
                    add(name, rel, name == mod.dotted)
                if not sr:  # 分析的目录本身是包：同时登记在包名链的每一段后缀下，它的 __init__.py 就是包本身
                    for prefix in prefixes:
                        add(f"{prefix}.{name}" if name else prefix, rel, name == mod.dotted)
    for name, files in names.items():
        own = [rel for rel, is_own in files.items() if is_own]
        pool = own or list(files)
        if len(pool) == 1:
            index.by_dotted[name] = pool[0]
        else:
            index.ambiguous.add(name)


def _attr_chain(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


def import_bindings(n: ast.Import | ast.ImportFrom, mod: ModuleInfo) -> list[tuple[str, str]]:
    """一条 import 语句绑定的 (本地名, 点分全名)。星号导入、越界的相对导入不绑定。"""
    if isinstance(n, ast.Import):
        return [(a.asname, a.name) if a.asname else (a.name.split(".")[0],) * 2 for a in n.names]
    if n.level:
        package = mod.dotted if mod.rel.endswith("__init__.py") else mod.dotted.rpartition(".")[0]
        pkg_parts = package.split(".") if package else []
        up = n.level - 1
        if up > len(pkg_parts):
            return []
        base = ".".join(pkg_parts[: len(pkg_parts) - up] + ([n.module] if n.module else []))
    else:
        base = n.module or ""
    return [(a.asname or a.name, f"{base}.{a.name}" if base else a.name) for a in n.names if a.name != "*"]


def _collect_imports(mod: ModuleInfo) -> None:
    """整个模块（含函数体里）的 import；同一个本地名只记第一次（广度优先，模块级的先到）。"""
    for n in ast.walk(mod.tree):
        if isinstance(n, ast.Import | ast.ImportFrom):
            for name, target in import_bindings(n, mod):
                mod.imports.setdefault(name, target)


def _scope_bindings(stmts: list[ast.stmt]) -> dict[str, list[ast.expr | None]]:
    """一个作用域（模块或类体）里每个名字的全部绑定，按出现顺序：name = 名字 / 属性（可以连写 a = b = f、带注解）
    给出右边；其余绑定（def、class、import、别的赋值、for / with / except / match 的目标、del 等）为 None。
    进入 if / try 等复合语句，不进入嵌套函数、类、lambda 的体。"""
    out: dict[str, list[ast.expr | None]] = {}

    def add(name: str, value: ast.expr | None) -> None:
        out.setdefault(name, []).append(value)

    def visit(n: ast.AST) -> None:
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            add(n.name, None)
            return
        if isinstance(n, ast.Lambda):
            return
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Name | ast.Attribute) \
                and all(isinstance(t, ast.Name) for t in n.targets):
            for t in n.targets:
                add(t.id, n.value)
            return
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if n.value is not None:  # 只有注解、没有值的不绑定
                add(n.target.id, n.value if isinstance(n.value, ast.Name | ast.Attribute) else None)
            return
        if isinstance(n, ast.Import | ast.ImportFrom):
            for a in n.names:
                add(a.asname or a.name.split(".")[0], None)
            return
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            add(n.id, None)
        elif isinstance(n, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and n.name:
            add(n.name, None)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            add(n.rest, None)
        for c in ast.iter_child_nodes(n):
            visit(c)

    for st in stmts:
        visit(st)
    return out


def _alias_candidates(bindings: dict[str, list[ast.expr | None]]) -> dict[str, ast.expr]:
    """每次绑定都是 name = 同一个名字 / 属性 的名字 -> 右边。赋值两次但右边不同、或还有别的绑定的不算。"""
    out: dict[str, ast.expr] = {}
    for name, values in bindings.items():
        first = values[0]
        if first is not None and all(v is not None and ast.dump(v) == ast.dump(first) for v in values[1:]):
            out[name] = first
    return out


def _root_name(e: ast.expr) -> str | None:
    """名字或属性链最左边的名字：f → f，mod.sub.f → mod。"""
    while isinstance(e, ast.Attribute):
        e = e.value
    return e.id if isinstance(e, ast.Name) else None


def _module_aliases(tree: ast.Module) -> dict[str, ast.expr]:
    """模块级别名的候选（见 ModuleInfo.aliases）。有模块级的 from x import * 时一个都不算（它能绑定任何名字）；
    在函数里声明 global 的名字不算（函数里会改写）；右边最左边的名字在模块里不是恰好绑定一次的不算（try 里导入、
    except 里再定义的同名函数，定义了两次的函数）。"""
    bindings = _scope_bindings(tree.body)
    if "*" in bindings:  # from x import * 在 _scope_bindings 里绑定的名字是 *
        return {}
    rebound = {name for n in ast.walk(tree) if isinstance(n, ast.Global) for name in n.names}
    out: dict[str, ast.expr] = {}
    for name, rhs in _alias_candidates(bindings).items():
        root = _root_name(rhs)
        if name not in rebound and root not in rebound and len(bindings.get(root, ())) == 1:
            out[name] = rhs
    return out


def _collect_class_body(info: ClassInfo, body: list[ast.stmt], def_lines: dict[str, int]) -> None:
    """类体里的别名和别的绑定。m2 = m1：m1 是这个类上只定义了一次、定义写在这句之前的方法（或这样的别名）时，m2
    记为它的别名。def_lines：顶层方法名 -> def 所在行。"""
    bindings = _scope_bindings(body)
    top_defs: dict[str, int] = {}
    for st in body:
        if isinstance(st, ast.FunctionDef | ast.AsyncFunctionDef):
            top_defs[st.name] = top_defs.get(st.name, 0) + 1
    info.other_bindings = {name for name, values in bindings.items() if len(values) > top_defs.get(name, 0)}
    candidates = _alias_candidates(bindings)

    def target(name: str, before: int, depth: int) -> str | None:
        """name 在第 before 行时指向的方法 key。"""
        if name in info.methods:
            ok = len(bindings.get(name, ())) == 1 and def_lines[name] < before
            return info.methods[name] if ok else None
        rhs = candidates.get(name)
        if isinstance(rhs, ast.Name) and rhs.lineno < before and depth < 10:
            return target(rhs.id, rhs.lineno, depth + 1)
        return None

    for name, rhs in candidates.items():
        key = target(rhs.id, rhs.lineno, 0) if isinstance(rhs, ast.Name) else None
        if key:
            info.aliases[name] = key


def _collect_defs(mod: ModuleInfo, index: ProjectIndex) -> None:
    def add_function(node: FuncNode, prefix: str, cls: str | None, parent: str | None, top: bool) -> str:
        qual = prefix + node.name
        key = fn_key(mod.rel, qual, node.lineno)
        index.functions[key] = FunctionInfo(
            key=key, file=mod.rel, qualname=qual, line=node.lineno,
            end_line=node.end_lineno or node.lineno, node=node, cls=cls, parent=parent,
            decorators=list(node.decorator_list), doc_title=_doc_title(node),
            is_async=isinstance(node, ast.AsyncFunctionDef))
        if top:
            mod.functions[node.name] = key
        walk(node.body, qual + ".<locals>.", None, key, False)
        return key

    def add_class(node: ast.ClassDef, prefix: str, parent: str | None, top: bool) -> None:
        qual = prefix + node.name
        info = ClassInfo(name=node.name, file=mod.rel, line=node.lineno,
                         bases=[ast.unparse(b) for b in node.bases], decorators=list(node.decorator_list))
        index.classes.append(info)
        if top:
            mod.classes[node.name] = info
        def_lines: dict[str, int] = {}
        for st in node.body:
            if isinstance(st, ast.FunctionDef | ast.AsyncFunctionDef):
                key = add_function(st, qual + ".", node.name, parent, False)
                info.methods[st.name] = key
                def_lines[st.name] = st.lineno
            else:
                walk([st], qual + ".", node.name, parent, False)
        _collect_class_body(info, node.body, def_lines)

    def walk(stmts: list[ast.stmt], prefix: str, cls: str | None, parent: str | None, top: bool) -> None:
        for st in stmts:
            if isinstance(st, ast.FunctionDef | ast.AsyncFunctionDef):
                add_function(st, prefix, cls, parent, top)
            elif isinstance(st, ast.ClassDef):
                add_class(st, prefix, parent, top)
            else:
                # if / try / with / for 等复合语句里的定义
                for child in ast.iter_child_nodes(st):
                    if isinstance(child, ast.stmt):
                        walk([child], prefix, cls, parent, top)
                    elif isinstance(child, ast.excepthandler | ast.match_case):
                        walk(child.body, prefix, cls, parent, top)

    walk(mod.tree.body, "", None, None, True)


def _collect_instances(mod: ModuleInfo) -> None:
    for st in mod.tree.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            target, value = st.targets[0].id, st.value
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
            target, value = st.target.id, st.value
        else:
            continue
        if not isinstance(value, ast.Call):
            continue
        chain = _attr_chain(value.func)
        if not chain:
            continue
        head, rest = chain[0], chain[1:]
        if not rest and head in mod.classes:
            mod.instances[target] = f"{mod.dotted}.{head}"
        elif head in mod.imports:
            mod.instances[target] = ".".join([mod.imports[head], *rest])


def source_lines(text: str) -> list[str]:
    """源码按解析器的规则分行：只在 \\r\\n、\\r、\\n 处分（str.splitlines 还会在换页符、\\x1c、\\u2028 等处分，行号
    就错了）；末尾的换行不多出一个空行。"""
    lines = re.split(r"\r\n|\r|\n", text)
    if lines[-1] == "":
        lines.pop()
    return lines


def decode_source(raw: bytes) -> str:
    """源文件的文本：按 PEP 263 的编码声明或 BOM 解码（没有时是 UTF-8），去掉 BOM。"""
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding)


def _iter_py_files(root: Path, exclude: set[str], excluded: dict[str, int]):
    """root 下的 .py 文件，按路径排序；跳过的排除目录按名字计数进 excluded。符号链接的目录不进入（os.walk 默认）。"""
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            if d in exclude:
                excluded[d] = excluded.get(d, 0) + 1
        dirnames[:] = sorted(d for d in dirnames if d not in exclude)
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


LINK_OUTSIDE = "符号链接指向项目之外"
NOT_REGULAR = "不是普通文件"
LINK_NOT_PY = "符号链接指向的不是 .py 文件"
# 这些路径不是项目里的源文件，从不打开：pystepmap check 把它们列成警告，不算"解析不了"
NOT_SOURCE_REASONS = frozenset({LINK_OUTSIDE, NOT_REGULAR, LINK_NOT_PY})


def _unreadable_reason(path: Path, root: Path) -> str | None:
    """不能当源码读的 .py 路径的原因：符号链接指向分析目录之外（LINK_OUTSIDE）；解析后不是普通文件（FIFO、设备等，
    NOT_REGULAR）；项目之内的链接指向的不是 .py 文件（LINK_NOT_PY：读源码的接口只给 .py 文件，分析了也看不到源码）。
    这样的路径从不打开：读 FIFO 会一直等，读 /dev/zero 读不完。root 是解析过的绝对路径。项目之内指向 .py 普通文件的
    符号链接照常读（按链接自己的路径登记）；失效的链接（目标在项目之内但不存在）返回 None，交给后面的读取照常报
    "无法解析"。"""
    real = None
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            real = os.path.realpath(path)
            if os.path.commonpath([real, str(root)]) != str(root):
                return LINK_OUTSIDE
            st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return NOT_REGULAR
    if real is not None and not real.endswith(".py"):
        return LINK_NOT_PY
    return None


def iter_source_files(root: Path, config: ProjectConfig, excluded: dict[str, int] | None = None
                      ) -> Iterator[tuple[str, Path | None, str]]:
    """按 build_index 的规则列出 root 下每个 .py，按路径排序：(相对路径, 可读时的路径 | None, 不读的原因)。
    排除目录（config.exclude_dirs，任何一层）不进入，跳过的按名字计数进 excluded（给出时）；符号链接的目录不进入。
    不读的路径（见 _unreadable_reason）给 (相对路径, None, 原因)，从不打开；其余给 (相对路径, 路径, "")，读不读得出来
    由调用方处理。root 先解析成绝对路径（和 build_index 一样）。"""
    root = Path(root).resolve()
    for path in _iter_py_files(root, set(config.exclude_dirs), {} if excluded is None else excluded):
        rel = path.relative_to(root).as_posix()
        unreadable = _unreadable_reason(path, root)
        yield (rel, None, unreadable) if unreadable else (rel, path, "")


def build_index(root: Path, config: ProjectConfig) -> ProjectIndex:
    root = Path(root).resolve()  # 图里记的根目录总是绝对路径：核对时从别的工作目录也能找到源码
    index = ProjectIndex(root=root)
    configured = _configured_source_roots(root, config.source_roots) if config.source_roots is not None else None
    files: list[str] = []
    parsed: dict[str, tuple[ast.Module, str]] = {}
    for rel, path, unreadable in iter_source_files(root, config, index.excluded):
        if path is None:
            index.skipped.append((rel, unreadable))
            continue
        files.append(rel)
        try:
            raw = path.read_bytes()
            index.hashes[rel] = hashlib.sha256(raw).hexdigest()
            tree = ast.parse(raw, filename=rel)  # 字节：编码声明、BOM 由解析器处理
            text = decode_source(raw)
        except UnicodeDecodeError as e:
            index.skipped.append((rel, f"无法按声明的编码解码：{e.reason}"))
            continue
        except (SyntaxError, ValueError, RecursionError, OSError) as e:
            index.skipped.append((rel, f"无法解析：{type(e).__name__}: {e}"))
            continue
        parsed[rel] = (tree, text)
    roots = configured if configured is not None else _detect_source_roots(files, {r: t for r, (t, _) in parsed.items()})
    index.source_roots = roots
    index.package = _package_name(root) if "" in roots else ""
    for rel, (tree, text) in parsed.items():
        own = max((sr for sr in roots if _under(rel, sr)), key=len, default="")  # 包含它的最内层源码根
        mod = ModuleInfo(rel=rel, dotted=_dotted_name(rel, own), tree=tree, lines=source_lines(text), source_root=own)
        _collect_imports(mod)
        _collect_defs(mod, index)
        _collect_instances(mod)
        mod.aliases = _module_aliases(tree)
        index.modules[rel] = mod
    _register(index)
    return index

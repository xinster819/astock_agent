"""分层依赖约束 —— 让架构自己看住自己。

`astock/__init__.py` 声明了依赖方向：

    cli / ops
      └─ pipeline
           └─ guards
           └─ strategy
                └─ data
                     └─ core
                          └─ runtime

文档写下的约束不会自己生效。这个仓库之所以会退化成 40 个平铺模块互相乱引，
正是因为没有任何东西在阻止它。本文件把约束变成**可执行的**：
新增一条回指依赖会让测试红，而不是等到半年后重构时才被发现。

顺带钉住两条更硬的规矩：
  · core / runtime 不得 import 上层，也不得碰网络
  · 任何模块都不得在 import 期做 IO（这是 broker.py 当年的原罪）
"""
import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "astock"

#: 每层允许依赖的层（含自身）。越靠下越基础。
ALLOWED = {
    "runtime":   {"runtime"},
    "core":      {"core", "runtime"},
    "data":      {"data", "core", "runtime"},
    "strategy":  {"strategy", "data", "core", "runtime"},
    "guards":    {"guards", "data", "core", "runtime"},
    "pipeline":  {"pipeline", "guards", "strategy", "data", "core", "runtime"},
    "reporting": {"reporting", "guards", "strategy", "data", "core", "runtime"},
    "ops":       {"ops", "reporting", "pipeline", "guards", "strategy", "data", "core", "runtime"},
    "cli":       {"cli", "ops", "reporting", "pipeline", "guards", "strategy", "data", "core", "runtime"},
}

NETWORK_MODULES = {"urllib", "http", "socket", "requests", "akshare"}


def _modules():
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE)
        layer = rel.parts[0] if len(rel.parts) > 1 else None
        if layer:
            yield layer, path


def _imported_astock_layers(tree):
    """收集该文件 import 到的 astock 子包（含函数体内的延迟 import）。"""
    layers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == "astock" and len(parts) > 1:
                layers.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "astock" and len(parts) > 1:
                    layers.add(parts[1])
    return layers


ALL_MODULES = list(_modules())


@pytest.mark.parametrize("layer,path", ALL_MODULES,
                         ids=[f"{lay}/{p.name}" for lay, p in ALL_MODULES])
def test_module_only_imports_allowed_layers(layer, path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    used = _imported_astock_layers(tree)
    forbidden = used - ALLOWED[layer]
    assert not forbidden, (
        f"{path.relative_to(PACKAGE.parent)} 位于 {layer} 层，"
        f"却依赖了上层 {sorted(forbidden)}。依赖只允许自上而下——"
        f"要么把被依赖的东西下沉，要么用回调把控制权交还给上层。"
    )


@pytest.mark.parametrize("layer,path", [(lay, p) for lay, p in ALL_MODULES
                                        if lay in ("core", "runtime")],
                         ids=[f"{lay}/{p.name}" for lay, p in ALL_MODULES
                              if lay in ("core", "runtime")])
def test_core_and_runtime_stay_offline(layer, path):
    """钱的逻辑和基础设施不许联网。

    撮合规则一旦能发请求，就再也没法在内存里确定性地测它了。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & NETWORK_MODULES), \
        f"{path.name} 引入了网络库 {sorted(imported & NETWORK_MODULES)}"


@pytest.mark.parametrize("layer,path", ALL_MODULES,
                         ids=[f"{lay}/{p.name}" for lay, p in ALL_MODULES])
def test_no_io_at_import_time(layer, path):
    """import 期不得建目录、开文件、读环境变量决定路径。

    这是 broker.py 当年的原罪：`os.makedirs` 与 `os.environ.get("ASTOCK_GROUP")`
    在模块顶层执行，把账本路径钉死在 import 那一刻。后果是同一进程内碰不了
    第二个账户、测试必须 reload 模块，以及——唯一动钱的模块一直没有测试。
    """
    forbidden = {"makedirs", "mkdir", "open", "remove", "rename", "touch"}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in tree.body:                       # 只看模块顶层，函数体内不管
        for inner in ast.walk(node):
            if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                break
            if isinstance(inner, ast.Call):
                fn = inner.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in forbidden:
                    offenders.append(f"{name}() @ line {inner.lineno}")
    assert not offenders, (
        f"{path.name} 在 import 期做了 IO：{offenders}。"
        f"路径解析请放进函数，建目录用显式的 ensure_dirs()。"
    )


def test_layer_map_covers_every_subpackage():
    """新增一层却忘了在这里登记，测试要能发现。"""
    actual = {p.name for p in PACKAGE.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    assert actual == set(ALLOWED), f"分层表与实际子包不一致：{actual ^ set(ALLOWED)}"


# ---------------------------------------------------------------------------
# 运行期兼容性
# ---------------------------------------------------------------------------

MIN_PYTHON = (3, 9)     # 与 pyproject 的 requires-python 一致


@pytest.mark.parametrize("layer,path", ALL_MODULES,
                         ids=[f"{lay}/{p.name}" for lay, p in ALL_MODULES])
def test_parses_on_the_minimum_supported_python(layer, path):
    """按 `requires-python` 声明的下限解析。

    声明支持 3.9 而只在 3.11 上跑过，等于没声明——调度机上装的是哪个版本
    不由我们决定。CI 里有一条 3.9 的 job，但本地 `make check` 不会跑到，
    所以这里补一道：新写的 3.10+ 语法当场就红，而不是等推上去才发现。
    """
    try:
        ast.parse(path.read_text(encoding="utf-8"), feature_version=MIN_PYTHON)
    except SyntaxError as exc:
        pytest.fail(f"{path.name}:{exc.lineno} 在 Python "
                    f"{'.'.join(map(str, MIN_PYTHON))} 下无法解析：{exc.msg}")


@pytest.mark.parametrize("layer,path", ALL_MODULES,
                         ids=[f"{lay}/{p.name}" for lay, p in ALL_MODULES])
def test_pep604_unions_are_guarded_by_future_import(layer, path):
    """`X | None` 写在函数签名里会在**定义时**求值，3.9 上直接 TypeError。

    `from __future__ import annotations` 把注解变成字符串，从而安全。
    语法解析查不出这一条——它是合法语法，只是运行期会炸。
    """
    source = path.read_text(encoding="utf-8")
    if "from __future__ import annotations" in source:
        return

    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        annotations = [arg.annotation for arg in node.args.args if arg.annotation]
        if node.returns:
            annotations.append(node.returns)
        offenders += [a.lineno for a in annotations
                      if isinstance(a, ast.BinOp) and isinstance(a.op, ast.BitOr)]

    assert not offenders, (
        f"{path.name} 第 {offenders} 行用了 `X | Y` 注解但没有 "
        f"`from __future__ import annotations`，在 Python "
        f"{'.'.join(map(str, MIN_PYTHON))} 上会在 import 期抛 TypeError。"
    )

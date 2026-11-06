"""Compile public Python and enforce the Client's real dependency boundaries."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]

import ast
import json

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "agent_brain_client"
SOURCE_PREFIX = "src/agent_brain_client/"


def import_targets(tree, module):
    """Include relative, qualified, from-package and literal dynamic imports."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name
        elif isinstance(node, ast.ImportFrom):
            target = node.module or ""
            if node.level:
                parent = module.split(".")[:-1]
                parent = parent[: len(parent) - node.level + 1]
                target = ".".join([*parent, *([target] if target else [])])
            yield node, target
            for alias in node.names:
                if alias.name == "*":
                    raise ValueError(
                        f"Explicit dependency imports required: {module}:{node.lineno}"
                    )
                yield node, target + "." + alias.name
        elif isinstance(node, ast.Call):
            function = node.func
            dynamic = (
                isinstance(function, ast.Name)
                and function.id == "__import__"
                or isinstance(function, ast.Attribute)
                and function.attr == "import_module"
            )
            if dynamic:
                if (
                    not node.args
                    or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)
                ):
                    raise ValueError(
                        f"Literal dependency import required: {module}:{node.lineno}"
                    )
                yield node, node.args[0].value


def check_layer_boundary(name, source):
    if not name.startswith(SOURCE_PREFIX):
        return
    relative = name[len(SOURCE_PREFIX) :]
    layer = relative.split("/")[0]
    module = PACKAGE + "." + relative.removesuffix(".py").replace("/", ".")
    for node, target in import_targets(ast.parse(source, filename=name), module):
        parts = target.split(".")
        forbidden = parts[0] in {"dev", "bootstrap", "tests"}
        if parts[0] == PACKAGE and len(parts) > 1:
            dependency = parts[1]
            if layer == "domain":
                forbidden |= dependency != "domain"
            elif layer == "application":
                forbidden |= dependency not in {"domain", "application"}
            elif layer == "adapters":
                forbidden |= dependency in {"interfaces", "runtime"}
            if relative.startswith("adapters/harnesses/codex/"):
                forbidden |= target.startswith(
                    PACKAGE + ".adapters.harnesses.codebuddy"
                )
            if relative.startswith("adapters/harnesses/codebuddy/"):
                forbidden |= target.startswith(PACKAGE + ".adapters.harnesses.codex")
            if relative.startswith("adapters/platforms/macos/"):
                forbidden |= target.startswith(PACKAGE + ".adapters.platforms.windows")
            if relative.startswith("adapters/platforms/windows/"):
                forbidden |= target.startswith(PACKAGE + ".adapters.platforms.macos")
            if relative.startswith("adapters/storage/"):
                forbidden |= target.startswith(PACKAGE + ".adapters.harnesses")
        if layer in {"domain", "application"}:
            forbidden |= parts[0] in {
                "os",
                "socket",
                "subprocess",
                "ctypes",
                "urllib",
                "http",
                "fcntl",
                "msvcrt",
            }
        if forbidden:
            raise ValueError(
                f"Client dependency boundary crossed: {name}:{node.lineno}: {target}"
            )
    if layer in {"domain", "application"}:
        for node in ast.walk(ast.parse(source, filename=name)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {
                    "read_bytes",
                    "write_bytes",
                    "read_text",
                    "write_text",
                    "exists",
                    "iterdir",
                    "mkdir",
                    "unlink",
                    "touch",
                    "stat",
                    "lstat",
                    "glob",
                    "rglob",
                    "is_file",
                    "is_dir",
                }
            ):
                raise ValueError(
                    f"Client dependency boundary requires a storage port: {name}:{node.lineno}"
                )


def check_platform_boundary(name, source):
    if name == SOURCE_PREFIX + "adapters/platforms/filesystem.py" or name.startswith(
        "tests/"
    ):
        return
    tree = ast.parse(source, filename=name)
    modules, functions = {}, set()
    prohibited = {
        "platform": {"machine", "system", "uname", "architecture"},
        "os": {"uname"},
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in prohibited:
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in prohibited:
            for alias in node.names:
                if alias.name == "*":
                    functions.update(prohibited[node.module])
                elif alias.name in prohibited[node.module]:
                    functions.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        direct = isinstance(called, ast.Name) and called.id in functions
        attribute = (
            isinstance(called, ast.Attribute)
            and isinstance(called.value, ast.Name)
            and called.attr in prohibited.get(modules.get(called.value.id, ""), set())
        )
        if direct or attribute:
            raise ValueError(f"Shared platform detector required: {name}:{node.lineno}")


def check():
    count = 0
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if any(
            part in {".git", ".local", "__pycache__", ".mypy_cache", ".ruff_cache"}
            for part in relative.parts
        ):
            continue
        if path.is_symlink():
            raise ValueError("Linked public source")
        name = relative.as_posix()
        source = path.read_bytes()
        compile(source, name, "exec")
        check_platform_boundary(name, source)
        check_layer_boundary(name, source)
        count += 1
    return {
        "status": "source-syntax-and-boundaries-checked",
        "python_files": count,
        "bytecode_written": False,
    }


if __name__ == "__main__":
    print(json.dumps(check()))

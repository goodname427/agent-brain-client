"""Compile every repository Python source without bytecode or external imports."""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_platform_boundary(name, source):
    """Architecture discovery has one implementation, shared by every caller."""
    if name == 'scripts/brain_lock.py' or name.split('/')[0] == 'tests':
        return
    tree = ast.parse(source, filename=name)
    modules, functions = {}, set()
    prohibited = {'platform': {'machine', 'system', 'uname', 'architecture'}, 'os': {'uname'}}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in prohibited:
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in prohibited:
            for alias in node.names:
                if alias.name == '*':
                    functions.update(prohibited[node.module])
                elif alias.name in prohibited[node.module]:
                    functions.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        direct = isinstance(called, ast.Name) and called.id in functions
        attribute = (isinstance(called, ast.Attribute) and isinstance(called.value, ast.Name)
                     and called.attr in prohibited.get(modules.get(called.value.id, ''), set()))
        if direct or attribute:
            raise ValueError(f'Shared platform detector required: {name}:{node.lineno}')


def check():
    count = 0
    for path in sorted(ROOT.rglob("*.py")):
        if any(
            part in (".git", ".local", "__pycache__", ".mypy_cache", ".ruff_cache")
            for part in path.relative_to(ROOT).parts
        ):
            continue
        if path.is_symlink():
            raise ValueError("Linked public source")
        name = path.relative_to(ROOT).as_posix()
        source = path.read_bytes()
        compile(source, name, "exec")
        check_platform_boundary(name, source)
        count += 1
    return {
        "status": "source-syntax-checked",
        "python_files": count,
        "bytecode_written": False,
    }


if __name__ == "__main__":
    print(json.dumps(check()))

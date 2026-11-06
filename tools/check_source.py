"""Compile every repository Python source without bytecode or external imports."""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_layer_boundary(name, source):
    """Shared leaf libraries cannot acquire host, installer or developer imports."""
    path = Path(name)
    shared = (path.parent.as_posix() == 'scripts' and
              (path.stem.startswith('brain_') or path.stem in
               {'client_release', 'client_pairing', 'https_content',
                'ssh_content', 'system_credentials', 'remote_mcp', 'global_memory_scope'}))
    codebuddy = name.startswith('adapters/codebuddy/')
    if not (shared or codebuddy):
        return
    native = {file.stem for file in (ROOT / 'adapters').rglob('*.py')}
    commands = {file.stem for directory in ('tools', 'installer')
                for file in (ROOT / directory).glob('*.py')}
    forbidden = (native | commands | {'tools', 'installer', 'adapters'}) if shared else {
        'codex_connect', 'content_cache', 'project_memory'}
    tree = ast.parse(source, filename=name)
    for node in ast.walk(tree):
        imports = []
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module or '', *(alias.name for alias in node.names)]
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            function = node.func
            if ((isinstance(function, ast.Name) and function.id == '__import__') or
                    (isinstance(function, ast.Attribute) and function.attr == 'import_module')):
                if isinstance(node.args[0].value, str):
                    imports = [node.args[0].value]
        if any(set(module.split('.')) & forbidden for module in imports):
            raise ValueError(f"Client dependency boundary crossed: {name}:{getattr(node, 'lineno', 0)}")


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
        check_layer_boundary(name, source)
        count += 1
    return {
        "status": "source-syntax-checked",
        "python_files": count,
        "bytecode_written": False,
    }


if __name__ == "__main__":
    print(json.dumps(check()))

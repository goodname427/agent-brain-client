"""Required Python runtime and scoped manifest extensions."""

from pathlib import Path, PurePosixPath
import re
import subprocess


MANIFEST_PYTHON_LAYOUT = 1

_RUNTIME_ROOT = PurePosixPath("src/agent_brain_client")

# Entry paths used by the installer, fixed MCP and native Hook, plus the
# credential helper whose source pin is part of Windows enrollment.
_ENTRY_FILES = (
    "adapters/platforms/windows/native.py",
    "adapters/storage/content.py",
    "interfaces/cli/codebuddy.py",
    "interfaces/cli/collection.py",
    "interfaces/cli/delivery.py",
    "interfaces/cli/install.py",
    "interfaces/cli/project.py",
    "interfaces/mcp/content.py",
    "interfaces/mcp/launcher.py",
    "interfaces/mcp/topics.py",
    "runtime/launcher.py",
)
PYTHON_FILES = tuple(sorted({
    *(_RUNTIME_ROOT.joinpath(name).as_posix() for name in _ENTRY_FILES),
    *((parent / "__init__.py").as_posix()
      for name in _ENTRY_FILES for parent in (_RUNTIME_ROOT / name).parents
      if parent.is_relative_to(_RUNTIME_ROOT)),
}))

_EXTENSION_MODULE = re.compile(
    r"src/agent_brain_client/(?:(?:application|domain|runtime|interfaces|"
    r"adapters)/(?:[a-z_][a-z0-9_]*/)*[a-z_][a-z0-9_]*\.py|__init__\.py)"
)
_RESERVED_WINDOWS_NAME = re.compile(r"(?:aux|con|nul|prn|com[1-9]|lpt[1-9])")


def ordinary_python_module(name, members):
    if not isinstance(name, str) or not _EXTENSION_MODULE.fullmatch(name):
        return False
    path = PurePosixPath(name)
    if any(
        _RESERVED_WINDOWS_NAME.fullmatch(part.removesuffix(".py"))
        for part in path.parts
    ):
        return False
    if (path.with_suffix("") / "__init__.py").as_posix() in members:
        return False
    for parent in path.parents:
        if parent == _RUNTIME_ROOT:
            break
        if (
            (parent / "__init__.py").as_posix() not in members
            or parent.with_suffix(".py").as_posix() in members
        ):
            return False
    return True


def runtime_python_members(names, *, required=None):
    """Keep the existing installed layout and validate all optional members."""
    members = set(names)
    required = set(PYTHON_FILES if required is None else required)
    if not required <= members or any(
        not ordinary_python_module(name, members) for name in members - required
    ):
        raise ValueError("Python runtime members outside reviewed layout")
    return tuple(sorted(members))


def source_python_members(source, *, commit=None):
    """Read Git membership, or the contents of an already trusted source archive."""
    source = Path(source).resolve()
    if commit:
        rows = subprocess.check_output(
            ["git", "ls-tree", "-rz", commit or "HEAD", "--", str(_RUNTIME_ROOT)],
            cwd=source, timeout=60,
        )
        names = []
        for row in filter(None, rows.split(b"\0")):
            metadata, raw_name = row.split(b"\t", 1)
            mode, kind, _ = metadata.split()
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                raise ValueError("Linked or external Python source")
            if raw_name.endswith(b".py"):
                names.append(raw_name.decode("utf-8"))
    else:
        names = []
        for path in (source / str(_RUNTIME_ROOT)).rglob("*"):
            if path.is_symlink() or path.resolve() != path:
                raise ValueError("Linked or nonregular Python source")
            if path.suffix == ".py":
                if not path.is_file():
                    raise ValueError("Linked or nonregular Python source")
                names.append(path.relative_to(source).as_posix())
    return runtime_python_members(names)

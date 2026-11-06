"""Actual package boundaries, package completeness and import isolation."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dev.checks.source import check_layer_boundary
from agent_brain_client.adapters.packages.client import ClientPackage, UpdateBlocked
from agent_brain_client.adapters.packages.layout import PYTHON_FILES

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "src/agent_brain_client/"


class Architecture(unittest.TestCase):
    def test_forbidden_edges_cannot_hide_in_qualified_relative_or_dynamic_imports(self):
        vectors = [
            ("domain/example.py", "import agent_brain_client.adapters.server.https"),
            ("domain/example.py", "from agent_brain_client import adapters"),
            ("domain/example.py", "from ..adapters.server import https"),
            ("domain/example.py", "import os"),
            (
                "adapters/platforms/windows/example.py",
                "from system_credentials import unique",
            ),
            ("application/example.py", "import subprocess"),
            ("application/example.py", "cache.read_bytes()"),
            ("application/example.py", "cache.exists()"),
            (
                "application/example.py",
                "importlib.import_module('agent_brain_client.runtime.collection')",
            ),
            (
                "application/example.py",
                "__import__('agent_brain_client.adapters.storage.content')",
            ),
            (
                "adapters/storage/example.py",
                "from agent_brain_client.adapters.harnesses.codex import content",
            ),
            (
                "adapters/harnesses/codebuddy/example.py",
                "from agent_brain_client.adapters.harnesses.codex import configuration",
            ),
            (
                "adapters/harnesses/codex/example.py",
                "import agent_brain_client.adapters.harnesses.codebuddy.events",
            ),
            (
                "adapters/server/example.py",
                "from agent_brain_client.interfaces.mcp import content",
            ),
            ("interfaces/example.py", "from dev.build import package"),
            (
                "adapters/platforms/macos/example.py",
                "from agent_brain_client.adapters.platforms.windows import native",
            ),
            (
                "adapters/platforms/windows/example.py",
                "from agent_brain_client.adapters.platforms.macos import keychain",
            ),
        ]
        for name, source in vectors:
            with (
                self.subTest(name=name, source=source),
                self.assertRaisesRegex(ValueError, "dependency boundary"),
            ):
                check_layer_boundary(PREFIX + name, source)

    def test_layer_dependencies_are_allowed_in_their_actual_direction(self):
        for name, source in [
            ("domain/example.py", "from .events import event"),
            ("application/example.py", "from agent_brain_client.domain import content"),
            (
                "adapters/server/example.py",
                "from agent_brain_client.application import control",
            ),
            (
                "interfaces/example.py",
                "from agent_brain_client.application.tools import catalog",
            ),
            (
                "runtime/example.py",
                "from agent_brain_client.adapters.harnesses.codex import content",
            ),
        ]:
            check_layer_boundary(PREFIX + name, source)

    def test_importing_shared_use_cases_neither_loads_a_host_nor_changes_search_paths(
        self,
    ):
        code = """import json,sys
sys.path.insert(0,sys.argv[1])
before=list(sys.path)
from agent_brain_client.domain import contract
from agent_brain_client.application.memory import capture,recall
from agent_brain_client.application.queue import outbox
from agent_brain_client.application.tools import catalog
assert sys.path==before
assert not any(n.startswith(('agent_brain_client.adapters','agent_brain_client.runtime','agent_brain_client.interfaces')) for n in sys.modules)
print(json.dumps({'host_loaded':False,'search_path_changed':False}))
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", code, str(ROOT / "src")],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"host_loaded": False, "search_path_changed": False},
        )

    def test_every_runtime_module_is_in_the_exact_package_inventory(self):
        actual = {
            p.relative_to(ROOT).as_posix()
            for p in (ROOT / "src/agent_brain_client").rglob("*.py")
        }
        self.assertEqual(set(PYTHON_FILES), actual)
        self.assertEqual(len(PYTHON_FILES), len(actual))

    def test_old_and_mixed_executable_layouts_require_a_new_installation(self):
        package = ClientPackage()
        for files in (
            {"scripts/remote_mcp.py": "a" * 64},
            {name: "a" * 64 for name in package.files[:-1]},
        ):
            with self.assertRaises(UpdateBlocked):
                package.package_files({"files": files})


if __name__ == "__main__":
    unittest.main()

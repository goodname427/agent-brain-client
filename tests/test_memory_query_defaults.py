"""Packaged worker defaults, current source metadata and immutable retries."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_brain_client.interfaces.mcp.content import ContentServer
from agent_brain_client.adapters.storage.origins import discover as current_origin
from agent_brain_client.interfaces.mcp.server import serve
import test_content_outbox as fixture

ROOT = Path(__file__).resolve().parents[1]


class QueryDefaults(unittest.TestCase):
    def test_exported_package_query_runs_without_development_tools(self):
        sys.path.insert(0, str(ROOT / "src"))
        from dev.build.package import export

        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "package"
            export(
                ROOT,
                package,
                https_content_candidate=True,
                target_platform="win32-amd64",
            )
            self.assertFalse((package / "tools").exists())
            script = """import sys,json
sys.path[:0]=[sys.argv[1]+'/adapters/codex',sys.argv[1]+'/scripts']
from agent_brain_client.interfaces.mcp.content import ContentServer
class Adapter:
 def methods(self):return ['memory_query']
 def recall(self,request):return {'request_id':request['request_id'],'result':request['params']}
server=ContentServer(Adapter())
for arguments in ({'query':'虚构测试项目 最新代号'}, {'query':'虚构查询','limit':10,'origin':{'kind':'project','cwd':'C:/Synthetic/app','project_root':'C:/Synthetic/app','repository_root':None}}):
 result=server.invoke('memory_query',arguments)
 assert not result['isError'],result
 params=result['structuredContent']['result']
 assert params['limit']==arguments.get('limit',5)
 assert params['recall']=={}
 assert params['origin']['kind']=='non-project' or params['origin']['repository_root'] is not None
 assert params['session_id'].startswith('reader-')
assert 'global_memory' not in sys.modules
"""
            run = subprocess.run(
                [sys.executable, "-I", "-B", "-c", script, str(package)],
                cwd=package,
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_valid_json_execution_failure_is_not_a_parse_error(self):
        class Server:
            def handle(self, request):
                raise RuntimeError("FICTIONAL_PRIVATE_DIAGNOSTIC")

        source = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
        output = io.StringIO()
        serve(Server(), source, output)
        reply = json.loads(output.getvalue())
        self.assertEqual(reply["error"]["code"], -32603)
        self.assertNotIn("FICTIONAL_PRIVATE", output.getvalue())

    def test_origin_uses_git_worktree_metadata_and_nonproject_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "repository"
            repo.mkdir()
            (repo / ".git").mkdir()
            work = root / "worktree"
            work.mkdir()
            (work / ".git").write_text("gitdir: " + str(repo / ".git/worktrees/topic"))
            with patch("agent_brain_client.interfaces.mcp.content.Path.cwd", return_value=work):
                self.assertEqual(current_origin()["repository_root"], str(repo))
            plain = root / "plain"
            plain.mkdir()
            with patch("agent_brain_client.interfaces.mcp.content.Path.cwd", return_value=plain):
                self.assertEqual(current_origin()["kind"], "non-project")


class WriteProvenance(unittest.TestCase):
    def setUp(self):
        self.f = fixture.Outbox("test_same_id_concurrent_retry_only_uploads_once")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.server = ContentServer(self.f.adapter)
        self.request = self.f.request()

    def invoke(self):
        return self.server.invoke(
            "content_put",
            dict(self.request["params"], request_id=self.request["request_id"]),
        )

    def test_context_is_current_and_frozen_before_send_and_across_restart(self):
        origin = {
            "kind": "project",
            "cwd": "C:/Synthetic/app",
            "project_root": "C:/Synthetic/app",
            "repository_root": "C:/Synthetic/app",
        }
        self.f.cloud.offline = True
        with (
            patch("agent_brain_client.interfaces.mcp.content.current_origin", return_value=origin),
            patch.dict(os.environ, {"CODEX_THREAD_ID": "fictional-windows-session"}),
        ):
            result = self.invoke()
        self.assertTrue(result["isError"])
        saved = self.f.adapter.inspect_queue(request_id=self.request["request_id"])[
            "request"
        ]
        self.assertEqual(
            saved["params"]["source_context"],
            {"origin": origin, "session_id": "fictional-windows-session"},
        )
        self.f.cloud.offline = False
        self.f.adapter = self.f.restart()
        self.server = ContentServer(self.f.adapter)
        with patch(
            "agent_brain_client.interfaces.mcp.content.current_origin", side_effect=OSError("Fictional missing cwd")
        ):
            self.assertFalse(self.invoke()["isError"])
        self.assertEqual(
            next(r for r in self.f.cloud.calls if r["method"] == "content_put"), saved
        )
        self.assertEqual(self.f.cloud.puts, 1)

    def test_legacy_pending_retry_keeps_original_payload(self):
        self.f.cloud.offline = True
        self.f.adapter.invoke(self.request)
        self.f.cloud.offline = False
        self.assertFalse(self.invoke()["isError"])
        self.assertEqual(
            next(r for r in self.f.cloud.calls if r["method"] == "content_put"),
            self.request,
        )

    def test_explicit_host_context_is_retained_and_unknown_session_stays_null(self):
        self.request["params"]["source_context"] = {"origin": None, "session_id": None}
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "not used"}):
            self.assertFalse(self.invoke()["isError"])
        saved = self.f.adapter.inspect_queue(request_id=self.request["request_id"])[
            "request"
        ]
        self.assertEqual(
            saved["params"]["source_context"], {"origin": None, "session_id": None}
        )


if __name__ == "__main__":
    unittest.main()

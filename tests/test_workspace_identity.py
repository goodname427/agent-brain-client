"""Synthetic repository identity and immutable L0 delivery tests."""

from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from agent_brain_client.adapters.server.collection import CollectionClient
from agent_brain_client.adapters.storage.origins import discover
from agent_brain_client.adapters.storage.workspace_identity import remote_identity, workspace_project
from agent_brain_client.application.memory.l0 import event_source_project, stage_source_project
from agent_brain_client.domain.events import l0_request
from agent_brain_client.domain.scopes import namespace


PROJECT = {
    "repository": "github.com/synthetic/project-a",
    "id": "git-sha256:5dab25d7ae2666ed57940bd42bcfb28737306db37df66f98b5ccd4d204f537ec",
}


class WorkspaceIdentity(unittest.TestCase):
    def test_only_normalized_network_repositories_have_identities(self):
        for remote in (
            "https://TRANSIENT-EXAMPLE:FAKE-ONLY@GitHub.com/Synthetic/Project-A.git/",
            "ssh://git@github.com:22/synthetic/project-a",
            "git@github.com:Synthetic/Project-A.git",
        ):
            self.assertEqual(remote_identity(remote), PROJECT["repository"])
        for remote in (
            "file:///synthetic/project-a", "../project-a", "C:/synthetic/project-a",
            "https://example.invalid/team/project-a?query=value",
            "ssh://git@example.invalid:2222/team/project-a",
            "https://example.invalid/team/../project-a",
        ):
            self.assertIsNone(remote_identity(remote))
        self.assertEqual(remote_identity("https://example.invalid/Team/Project"), "example.invalid/Team/Project")

    @unittest.skipUnless(shutil.which("git"), "Git required for synthetic linked worktree")
    def test_same_repo_across_checkout_and_worktree_and_unknown_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            a, b, plain, linked = [parent / name for name in ("a/check", "b/check", "ordinary", "linked")]
            for path in (a, b, plain):
                path.mkdir(parents=True)
            self.git(a, "init")
            self.git(a, "remote", "add", "origin", "git@github.com:synthetic/project-a.git")
            self.git(a, "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture")
            self.git(a, "worktree", "add", "--detach", str(linked), "HEAD")
            self.git(b, "init")
            self.assertIsNone(workspace_project(discover(b)))
            self.git(b, "remote", "add", "origin", "https://github.com/Synthetic/Project-A")
            self.assertEqual(workspace_project(discover(a)), PROJECT)
            self.assertEqual(workspace_project(discover(b)), PROJECT)
            self.assertEqual(workspace_project(discover(linked)), PROJECT)
            self.git(b, "remote", "set-url", "origin", "https://github.com/synthetic/project-b")
            self.assertNotEqual(workspace_project(discover(b)), PROJECT)
            self.git(b, "config", "--add", "remote.origin.url", "https://github.com/synthetic/project-a")
            self.assertIsNone(workspace_project(discover(b)))
            self.assertIsNone(workspace_project(discover(plain)))

    @staticmethod
    def git(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, timeout=10)

    def test_l0_retry_retains_original_and_negotiates_optional_origin(self):
        local = {"kind": "project", "cwd": "/synthetic/one", "project_root": "/synthetic/one",
                 "repository_root": "/synthetic/one"}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        event = {
            "contract": "agentbrain.event.v1", "event_id": "synthetic-event",
            "project_id": namespace("synthetic", "a" * 32), "session_id": "synthetic-session",
            "occurred_at": now, "messages": [{"role": "user", "content": "Synthetic original"}],
        }
        request = l0_request(event, "synthetic-request")
        for first_supported in (True, False):
            files = {}
            queue = SimpleNamespace(_load=files.get, _save=files.__setitem__)
            self.assertIsNone(event_source_project(queue, request))
            stage_source_project(queue, request, PROJECT)
            client = object.__new__(CollectionClient)
            client.value = {"scope": {"schema": 1, "profile_id": "a" * 32, "all_projects": True,
                                      "include_roots": [], "exclude_roots": [],
                                      "non_project_roots": [], "recall_personal": True}}
            client.local, client.actor = local, "synthetic"
            client.params = lambda session: {"origin": local, "session_id": session}
            client.capabilities = {"project-origin-v1"} if first_supported else set()
            client.event_source_project = lambda req: event_source_project(queue, req)
            client.wire = Mock(return_value={"error": {"code": "transport_unconfirmed"}})
            client.request(request)
            first = client.wire.call_args.args[1]
            client.capabilities = set() if first_supported else {"project-origin-v1"}
            client.request(request)
            retry = client.wire.call_args.args[1]
            self.assertEqual(client.wire.call_args.args[2], request["request_id"])
            self.assertEqual(first["origin"], dict(local, project=PROJECT) if first_supported else local)
            self.assertEqual(retry["origin"], local if first_supported else dict(local, project=PROJECT))
            self.assertEqual(first["event"], event)
            self.assertEqual(retry["event"], event)
            self.assertEqual(event_source_project(queue, request), PROJECT)
            self.assertEqual(request["params"], {"kind": "memory", "id": event["event_id"], "event": event})

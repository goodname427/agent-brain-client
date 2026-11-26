"""Synthetic repository identity and immutable L0 delivery tests."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_brain_client.adapters.server.collection import CollectionClient
from agent_brain_client.adapters.harnesses.codebuddy.events import selected_workspace
from agent_brain_client.adapters.storage.origins import discover
from agent_brain_client.adapters.storage.workspace_identity import host_workspace, remote_identity, workspace_project
from agent_brain_client.application.memory.l0 import (
    event_source_project, event_workspace_source, stage_source_project, stage_workspace_source,
)
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
            first = host_workspace(str(a), str(a)).source("fictional-device-a")
            other = host_workspace(str(linked), str(linked)).source("fictional-device-b")
            self.assertEqual(first["portable_id"], other["portable_id"])
            self.assertEqual(first["portable_id"], PROJECT["id"])
            self.assertNotEqual(first["id"], other["id"])
            child = a / "selected-folder"
            child.mkdir()
            workspace = host_workspace(str(child), str(child))
            self.assertEqual(discover(child)["project_root"], str(a.resolve()))
            self.assertIsNone(workspace.git_project)
            self.assertEqual(workspace.source("fictional-device-a")["display_name"], child.name)
            self.git(b, "remote", "set-url", "origin", "https://github.com/synthetic/project-b")
            self.assertNotEqual(workspace_project(discover(b)), PROJECT)
            self.git(b, "config", "--add", "remote.origin.url", "https://github.com/synthetic/project-a")
            self.assertIsNone(workspace_project(discover(b)))
            self.assertIsNone(workspace_project(discover(plain)))

    @staticmethod
    def git(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, timeout=10)

    def test_plain_workspace_local_ids_and_unknown_root(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"P4PORT": "", "P4CLIENT": ""}):
            top = Path(tmp).resolve()
            a, b = top / "a" / "same-name", top / "b" / "same-name"
            leaf = a / "task"
            leaf.mkdir(parents=True)
            b.mkdir(parents=True)
            workspace = host_workspace(str(a), str(leaf))
            first = workspace.source("fictional-device-a")
            self.assertEqual(first, host_workspace(str(a), str(a)).source("fictional-device-a"))
            self.assertEqual(first["display_name"], "same-name")
            self.assertNotIn("portable_id", first)
            self.assertNotIn(str(top), json.dumps(first))
            self.assertNotEqual(first["id"], workspace.source("fictional-device-b")["id"])
            self.assertNotEqual(first["id"], host_workspace(str(b), str(b)).source("fictional-device-a")["id"])
            self.assertIsNone(host_workspace(str(b), str(leaf)))
            self.assertIsNone(host_workspace(str(top / "missing"), str(a)))

    def test_codebuddy_reads_native_project_dir_before_event_cwd(self):
        payload = {"cwd": r"C:\synthetic\selected\task"}
        with patch.dict(os.environ, {"CODEBUDDY_PROJECT_DIR": "/c/synthetic/selected"}), patch.object(os, "name", "nt"):
            self.assertEqual(selected_workspace(payload), "c:/synthetic/selected")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(selected_workspace(payload), payload["cwd"])

    def test_svn_uses_working_copy_uuid_and_exact_project_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            top = Path(tmp).resolve()
            a, b = top / "copy-a", top / "copy-b"
            projects = [a / "first", b / "first", b / "second"]
            for project in projects:
                project.mkdir(parents=True)
            for copy in (a, b):
                (copy / ".svn").mkdir()

            def info(command, **_):
                self.assertNotIn("-r", command)
                self.assertIn("--non-interactive", command)
                self.assertIn("--config-dir", command)
                project = Path(command[-1][:-1])
                return (
                    '<info><entry kind="dir" path="' + str(project) + '">'
                    '<relative-url>^/trunk/' + project.name + '</relative-url>'
                    '<repository><uuid>12345678-abcd-1234-abcd-123456789abc</uuid></repository>'
                    '<wc-info><wcroot-abspath>' + str(project.parent)
                    + '</wcroot-abspath><schedule>normal</schedule></wc-info>'
                    '</entry></info>'
                ).encode()

            adapter = "agent_brain_client.adapters.storage.workspace_identity"
            with patch(adapter + "._executable", return_value=top / "svn"), patch(adapter + "._metadata", side_effect=info):
                sources = [host_workspace(str(project), str(project)).source(str(i)) for i, project in enumerate(projects)]
            self.assertTrue(sources[0]["portable_id"].startswith("svn-sha256:"))
            self.assertEqual(sources[0]["portable_id"], sources[1]["portable_id"])
            self.assertNotEqual(sources[1]["portable_id"], sources[2]["portable_id"])
            with patch(adapter + "._executable", return_value=None):
                self.assertNotIn("portable_id", host_workspace(str(projects[0]), str(projects[0])).source("fictional"))

    def test_p4_uses_existing_connection_and_exact_stream_and_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            top = Path(tmp).resolve()
            a, b = top / "copy-a", top / "copy-b"
            for root in (a, b):
                root.mkdir()

            def identity(root, client, *, stream="//depot/main", extra="", server="synthetic-primary"):
                def read(command, *, env, **_):
                    self.assertEqual(command[1:6], ["-ztag", "-p", "ssl:p4.synthetic.invalid:1666", "-c", client])
                    self.assertNotIn("P4PASSWD", env)
                    if command[6:] == ["info"]:
                        return (
                            f"... clientName {client}\n... serverAddress p4.synthetic.invalid:1666\n"
                            f"... serverID {server}\n"
                        ).encode()
                    self.assertEqual(command[6:], ["client", "-o", client])
                    return (
                        f"... Client {client}\n... Root {root}\n... Update fictional\n"
                        f"... Stream {stream}\n... View0 {stream}/... //{client}/...\n" + extra
                    ).encode()

                adapter = "agent_brain_client.adapters.storage.workspace_identity"
                with (
                    patch.dict(os.environ, {
                        "P4PORT": "ssl:p4.synthetic.invalid:1666", "P4CLIENT": client, "P4PASSWD": "fictional",
                    }),
                    patch(adapter + "._executable", return_value=top / "p4"),
                    patch(adapter + "._metadata", side_effect=read),
                ):
                    return host_workspace(str(root), str(root)).source(client)

            first, other = identity(a, "workstation-a"), identity(b, "workstation-b")
            self.assertTrue(first["portable_id"].startswith("p4-sha256:"))
            self.assertEqual(first["portable_id"], other["portable_id"])
            self.assertNotEqual(first["portable_id"], identity(b, "workstation-b", stream="//depot/dev")["portable_id"])
            self.assertNotEqual(first["portable_id"], identity(b, "workstation-b", server="different-instance")["portable_id"])
            self.assertNotIn("portable_id", identity(b, "workstation-b", server=""))
            self.assertNotIn("portable_id", identity(b, "workstation-b", extra="... View1 //alternate/overlap/... //workstation-b/...\n"))

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

    def test_plain_workspace_leaves_l0_request_and_legacy_wire_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"P4PORT": "", "P4CLIENT": ""}):
            selected = Path(tmp).resolve() / "selected-a"
            selected.mkdir()
            local = discover(selected)
            scope = {"schema": 1, "profile_id": "a" * 32, "all_non_projects": True,
                     "include_roots": [], "exclude_roots": [], "non_project_roots": [], "recall_personal": True}
            files = {}
            queue = SimpleNamespace(_load=files.get, _save=files.__setitem__, workspace_device="fictional-device")
            workspace = host_workspace(str(selected), str(selected))
            client = object.__new__(CollectionClient)
            client.value, client.local, client.actor = {"scope": scope}, local, "synthetic"
            client.params = lambda session: {"origin": local, "session_id": session}
            client.event_workspace_source = lambda request: event_workspace_source(queue, request)
            client.event_source_project = lambda request: event_source_project(queue, request)
            client.wire = Mock(return_value={"error": {"code": "transport_unconfirmed"}})
            for i, content in enumerate(("Project A decided.", "Project B decided.", "I prefer shorter replies.")):
                event = {
                    "contract": "agentbrain.event.v1", "event_id": f"synthetic-event-{i}",
                    "project_id": namespace(client.actor, scope["profile_id"]), "session_id": "synthetic-session",
                    "occurred_at": "2026-01-01T00:00:00Z", "messages": [{"role": "user", "content": content}],
                }
                request = l0_request(event, f"synthetic-request-{i}")
                raw = json.dumps(request)
                stage_workspace_source(queue, request, workspace)
                source = workspace.source(queue.workspace_device)
                self.assertEqual(event_workspace_source(queue, request), source)
                self.assertIsNone(event_source_project(queue, request))
                for capabilities, origin in (
                    ({"project-origin-v1", "final-context-v1"}, local),
                    ({"workspace-origin-v1"}, dict(local, workspace=source)),
                ):
                    client.capabilities = capabilities
                    client.request(request)
                    method, params, rid = client.wire.call_args.args
                    self.assertEqual(method, "memory_global_event_submit")
                    self.assertEqual(params, {"origin": origin, "session_id": event["session_id"], "event": event})
                    self.assertEqual(rid, request["request_id"])
                    self.assertEqual(json.dumps(request), raw)

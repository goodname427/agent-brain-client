"""Synthetic device roots only; portable fault injection is not Windows acceptance."""

import copy
from contextlib import contextmanager
import io
import json
import os
import runpy
import re
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import codex_connect as host
import content_cache as cache
import content_install as ci
import content_mcp
import global_memory as gm
import global_memory_install as hooks
import mac_first_install as wizard
import windows_install_complete as guide
import access_install
from global_memory_scope import digest
from test_content_outbox import Cloud, POLICY, RULE, SKILL
import test_content_install as install_fixture
import test_client_uninstall as lifecycle_fixture


@contextmanager
def inplace_stage(path, raw, mode, expected):
    """Use real data writer and journal, substituting only Win32 handle open."""
    if not Path(path).exists():
        with ORIGINAL_STAGE(path, raw, mode, expected) as commit:
            yield commit
        return
    with Path(path).open("r+b", buffering=0) as file:
        st = os.fstat(file.fileno())
        if file.read() != expected:
            raise ValueError("Test preimage changed")

        def commit():
            host.host_write(file, raw)

        commit.inplace_identity = {"device": st.st_dev, "inode": st.st_ino}
        yield commit


ORIGINAL_STAGE = host.host_stage


def normal_retry(test, home, native):
    """Execute real argparse/early recovery; isolate downstream installation UI."""
    state = home / ".config/agent-brain-client"
    ci.save(
        state / "access-bootstrap/endpoint.json",
        {"schema": 1, "endpoint": "https://synthetic.invalid"},
    )

    def original_install_recovery(*args):
        receipt = ci.private_json(state / "content-install-receipt.json")
        test.assertEqual(receipt["phase"], "complete")
        value = ci.inspect(home)
        test.assertTrue(value.get("registration_verified"), value)
        return value

    with (
        patch.object(Path, "home", return_value=home),
        patch.dict(os.environ, CODEX_HOME=str(native)),
        patch.object(sys, "argv", ["mac_first_install.py"]),
        patch.object(wizard, "native_platform", return_value="win32-amd64"),
        patch.object(
            access_install,
            "resume_applying_registration",
            side_effect=original_install_recovery,
        ),
        patch.object(sys, "stdout", io.StringIO()) as output,
    ):
        code = wizard.main()
    return code, json.loads(output.getvalue())


def torn_hook(test, home, native):
    file = native / "config.toml"
    before = file.read_bytes()
    body = hooks.split_owned(before)[1]
    body = re.sub(
        r"^command = .+$", 'command = "synthetic revised command"', body, flags=re.M
    )

    def tear(stream, raw):
        for offset in range(1, len(raw)):
            try:
                ci.tomllib.loads((raw[:offset] + before[offset:]).decode())
            except (ValueError, UnicodeError):
                break
        else:
            test.fail("A malformed recognized Hook prefix is required")
        stream.seek(0)
        stream.write(raw[:offset])
        stream.flush()
        os.fsync(stream.fileno())
        raise SystemExit("Synthetic Hook interruption")

    with (
        patch.object(host, "host_stage", side_effect=inplace_stage),
        patch.object(host, "host_write", side_effect=tear),
        test.assertRaises(SystemExit),
    ):
        hooks.transact(native, gm.settings_path(home).parent, body)
    with test.assertRaises((ValueError, UnicodeError)):
        ci.tomllib.loads(file.read_bytes().decode())
    return before


class MappingClosure(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.native = self.home / "codex-custom"
        self.native.mkdir()
        self.agents = self.native / "AGENTS.md"
        self.agents.write_bytes(b"User instructions stay.\n")
        self.original = self.agents.read_bytes()
        self.inode = self.agents.stat().st_ino
        self.cloud = Cloud()
        self.policy = copy.deepcopy(POLICY)
        self.policy["ids"]["rule"] = ["synthetic-rule-" + str(i) for i in range(4)]
        self.policy["ids"]["skill"] = [
            "synthetic-" + name for name in ("maintain", "memory", "onboard")
        ]
        self.cloud.grant["ids"] = self.policy["ids"]
        self.cloud.records = {
            ("rule", "synthetic-rule-" + str(i)): dict(
                RULE, enabled=True, trigger={"mode": "always", "patterns": []}
            )
            for i in range(4)
        }
        for name in ("maintain", "memory", "onboard"):
            identity = "synthetic-" + name
            skill = copy.deepcopy(SKILL)
            skill["enabled"] = True
            skill["files"]["SKILL.md"] = skill["files"]["SKILL.md"].replace(
                "synthetic-skill", identity
            )
            self.cloud.records["skill", identity] = skill
        self.cache = self.root / "cache"
        self.patch = patch.object(host, "host_stage", side_effect=inplace_stage)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def adapter(self):
        return cache.ContentAdapter(
            self.home, self.cache, self.policy, self.cloud, codex_home=self.native
        )

    def tear(self, file, raw):
        if (
            file.name == str(self.agents)
            or os.fstat(file.fileno()).st_ino == self.agents.stat().st_ino
        ):
            file.seek(0)
            file.write(raw[: max(1, len(raw) // 2)])
            file.flush()
            os.fsync(file.fileno())
            raise SystemExit("Synthetic process termination")
        host_write(file, raw)

    def interrupt(self):
        with (
            patch.object(host, "host_write", side_effect=self.tear),
            self.assertRaises(SystemExit),
        ):
            self.adapter().refresh()

    def test_four_rules_three_skills_map_in_actual_codex_home_without_private_host_writes(
        self,
    ):
        private = cache.atomic

        def private_only(path, raw, mode):
            self.assertTrue(self.cache == path.parent or self.cache in path.parents)
            return private(path, raw, mode)

        with patch.object(cache, "atomic", side_effect=private_only):
            self.assertEqual(self.adapter().refresh()["status"], "content-refreshed")
        self.assertEqual(self.agents.stat().st_ino, self.inode)
        self.assertIn(self.original, self.agents.read_bytes())
        self.assertEqual(
            len(list((self.home / ".agents/skills").glob("*/SKILL.md"))), 3
        )
        self.assertFalse((self.home / ".codex/AGENTS.md").exists())
        self.assertEqual(self.adapter().refresh()["status"], "content-current")

    def test_partial_mapping_and_partial_rollback_recover_original_object(self):
        self.interrupt()
        with (
            patch.object(host, "host_write", side_effect=self.tear),
            self.assertRaises(SystemExit),
        ):
            self.adapter().refresh()
        self.assertEqual(self.adapter().refresh()["status"], "content-refreshed")
        self.assertEqual(self.agents.stat().st_ino, self.inode)
        self.assertIn(self.original, self.agents.read_bytes())

    def test_foreign_edit_and_object_replacement_are_preserved(self):
        self.interrupt()
        partial = self.agents.read_bytes()
        replacement = self.native / "replacement"
        replacement.write_bytes(partial)
        os.replace(replacement, self.agents)
        result = self.adapter().refresh()
        self.assertEqual(result["status"], "content-recovery-review-required")
        self.assertEqual(self.agents.read_bytes(), partial)
        self.agents.write_bytes(b"Foreign user edit\n")
        self.assertEqual(
            self.adapter().refresh()["status"], "content-recovery-review-required"
        )
        self.assertEqual(self.agents.read_bytes(), b"Foreign user edit\n")

    def test_user_skill_collision_and_postcheck_failure_leave_user_content(self):
        skill = self.home / ".agents/skills/synthetic-memory/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_bytes(b"User skill")
        self.assertEqual(self.adapter().refresh()["status"], "content-refresh-blocked")
        self.assertEqual(skill.read_bytes(), b"User skill")
        self.assertEqual(self.agents.read_bytes(), self.original)
        skill.unlink()
        adapter = self.adapter()
        adapter.health = lambda: (_ for _ in ()).throw(
            OSError("Synthetic postcheck failure")
        )
        self.assertEqual(adapter.refresh()["status"], "content-refresh-blocked")
        self.assertEqual(self.agents.read_bytes(), self.original)
        self.assertEqual(self.agents.stat().st_ino, self.inode)

    def test_initialize_reports_failed_refresh_even_when_tools_are_available(self):
        adapter = self.adapter()
        adapter.refresh = lambda: {
            "status": "content-refresh-blocked",
            "native_changed": False,
            "model_loaded": "unknown",
        }
        server = content_mcp.ContentServer(adapter, "0.5.8")
        value = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"},
                },
            }
        )
        self.assertEqual(value["content_mapping"]["status"], "content-refresh-blocked")
        self.assertIn("content-refresh-blocked", value["instructions"])


host_write = host.host_write


@unittest.skipIf(
    os.name == "nt",
    "Portable Mac fixture; native stale-launcher lifecycle is in test_windows_native",
)
class LauncherClosure(unittest.TestCase):
    def setUp(self):
        self.f = install_fixture.ContentInstall(
            "test_prepare_read_only_then_apply_inspect_reuse_and_single_link_state"
        )
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.publish("0.5.3")
        self.f.package = Path(self.f.channel.update()["active"]["package"])
        self.original_plan = self.f.plan()
        self.assertTrue(
            ci.apply(self.original_plan, self.f.digest(self.original_plan), lambda p: None)[
                "installed"
            ]
        )
        self.f.publish("0.5.7")
        self.f.channel.update()
        self.meta = ci.private_json(
            self.f.home / ".config/agent-brain-client/codex-mcp.json"
        )
        self.before = self.f.channel.verified_active()
        self.f.publish("0.5.8")
        self.channel = self.f.channel

    def handover(self):
        with patch.object(
            wizard,
            "probe_launcher",
            side_effect=lambda plan, active: self.assertEqual(active, self.before),
        ):
            return wizard.windows_launcher_handover(
                self.f.home, channel=self.channel, before=self.before, meta=self.meta
            )

    def test_mac_public_update_hands_over_stale_launcher_before_activation(self):
        self.assertTrue(self.channel.engine.policy['platform'].startswith('darwin-'))
        config = self.f.config.read_text().replace('enabled = true', 'enabled = false')
        self.f.config.write_text(config)
        # This portable fixture uses content-device capabilities. Substitute
        # only the public entry's HTTPS gate; package validation, policy pins,
        # native transaction, offline child probe and activation remain real.
        with patch.object(wizard, 'HTTPS_CONTENT_CAPABILITIES', self.channel.engine.policy['capabilities']):
            value = wizard.manage_update(self.f.home, 'update', channel_factory=lambda *a: self.channel)
        self.assertEqual(value['status'], 'client-update-updated', value)
        self.assertTrue(value['native_changed'])
        self.assertIsNone(value['reload_required'])
        self.assertTrue(value['reload_check']['mcp_registration_changed'])
        self.assertEqual(value['active_package_version'], '0.5.8')
        meta = ci.private_json(self.f.home / '.config/agent-brain-client/codex-mcp.json')
        self.assertNotEqual(meta['package'], self.meta['package'])
        self.assertEqual(meta['release_policy_sha256'], self.meta['release_policy_sha256'])
        self.assertEqual(ci.inspect(self.f.home)['installation_state'], 'verified-disabled')

    def test_handover_archives_only_proven_completed_reinstall_marker(self):
        state = self.f.home / '.config/agent-brain-client/first-install'
        state.mkdir(mode=0o700)
        plan = state / 'synthetic-reinstall-plan.json'
        ci.save(plan, self.original_plan)
        marker = state / 'reinstall-current.json'
        value = {'schema': 1, 'removed_plan_sha256': 'a' * 64,
                 'plan': str(plan), 'plan_sha256': self.meta['plan_sha256']}
        ci.save(marker, value)
        self.assertTrue(self.handover())
        self.assertFalse(marker.exists())
        archived = list(state.glob('prior-reinstall-current-*.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(ci.private_json(archived[0]), value)

    def test_handover_preserves_unconfirmed_reinstall_attempt(self):
        state = self.f.home / '.config/agent-brain-client/first-install'
        state.mkdir(mode=0o700)
        marker = state / 'reinstall-current.json'
        value = {'schema': 1, 'removed_plan_sha256': 'a' * 64,
                 'plan': str(state / 'unconfirmed.json'), 'plan_sha256': 'b' * 64}
        ci.save(marker, value)
        before = self.f.config.read_bytes()
        with self.assertRaises(ValueError):
            self.handover()
        self.assertEqual(ci.private_json(marker), value)
        self.assertEqual(self.f.config.read_bytes(), before)

    def superseded_reinstall(self):
        state = self.f.home / '.config/agent-brain-client/first-install'
        state.mkdir(mode=0o700)
        plan = state / 'synthetic-reinstall-plan.json'
        ci.save(plan, self.original_plan)
        marker = state / 'reinstall-current.json'
        value = {'schema': 1, 'removed_plan_sha256': 'a' * 64,
                 'plan': str(plan), 'plan_sha256': self.meta['plan_sha256']}
        ci.save(marker, value)
        # A later completed native transaction supersedes the reinstall. Its
        # exact registration preimage proves the marker is historical, even
        # when the reviewed policy path changes in that later transaction.
        policy = state / 'synthetic-replacement-policy.json'
        policy.write_bytes(self.f.policy_path.read_bytes())
        policy.chmod(0o600)
        replacement = ci.prepare(self.f.home, self.f.package, policy,
                                 self.f.policy_sha, replace_existing=True)
        self.assertTrue(ci.apply(replacement, self.f.digest(replacement), lambda p: None)['installed'])
        self.meta = ci.private_json(self.f.home / '.config/agent-brain-client/codex-mcp.json')
        self.assertNotEqual(self.meta['plan_sha256'], value['plan_sha256'])
        return marker, value

    def test_handover_archives_exact_superseded_reinstall_predecessor(self):
        marker, value = self.superseded_reinstall()
        before = self.f.config.read_bytes()
        # The evidence check itself is read-only.
        previous = wizard.completed_reinstall_registration(self.f.home, value, self.meta)
        self.assertEqual(previous['plan_sha256'], value['plan_sha256'])
        self.assertEqual(self.f.config.read_bytes(), before)
        self.assertEqual(ci.private_json(marker), value)
        self.assertTrue(self.handover())
        self.assertFalse(marker.exists())
        archive = marker.parent / ('prior-reinstall-current-' + wizard.sha(wizard.canonical(value)) + '.json')
        self.assertEqual(ci.private_json(archive), value)
        self.assertTrue(ci.inspect(self.f.home)['registration_verified'])

    def test_superseded_marker_requires_complete_exact_predecessor_evidence(self):
        marker, value = self.superseded_reinstall()
        receipt = self.f.home / '.config/agent-brain-client/content-install-receipt.json'
        original = ci.private_json(receipt)
        config = self.f.config.read_bytes()
        for change in ('phase', 'plan', 'target', 'image', 'predecessor'):
            altered = copy.deepcopy(original)
            registration = str(receipt.parent / 'codex-mcp.json')
            if change == 'phase':
                altered['phase'] = 'applying'
            if change == 'plan':
                altered['plan_sha256'] = 'b' * 64
            if change == 'target':
                altered['after'][registration] = 'b' * 64
            if change == 'image':
                altered['before'][registration]['sha'] = 'b' * 64
            if change == 'predecessor':
                import base64
                row = altered['before'][registration]
                prior = json.loads(base64.b64decode(row['data']))
                prior['plan_sha256'] = 'b' * 64
                raw = ci.contract.encode(prior)
                row.update(data=base64.b64encode(raw).decode(), sha=ci.sha(raw))
            ci.save(receipt, altered)
            with self.subTest(change=change), self.assertRaises(ValueError):
                wizard.retire_completed_reinstall(self.f.home, self.meta)
            self.assertEqual(ci.private_json(marker), value)
            self.assertEqual(self.f.config.read_bytes(), config)
            self.assertFalse(list(marker.parent.glob('prior-reinstall-current-*.json')))
        ci.save(receipt, original)

    def test_normal_install_retry_dispatches_applying_launcher_before_original_plan(
        self,
    ):
        marker = self.f.home / ".config/agent-brain-client/launcher-handover.json"
        receipt = (
            self.f.home / ".config/agent-brain-client/content-install-receipt.json"
        )
        original_save = ci.save

        def stop(path, value):
            original_save(path, value)
            if path == receipt and value.get("phase") == "applying":
                raise SystemExit("Synthetic launcher apply interruption")

        with patch.object(ci, "save", side_effect=stop), self.assertRaises(SystemExit):
            self.handover()
        self.assertTrue(marker.exists())
        self.assertFalse(ci.inspect(self.f.home).get("installed"))
        updates = []

        def resume(home, action):
            updates.append(action)
            wizard.windows_launcher_handover(
                home, channel_factory=lambda *a: self.channel
            )
            return {"status": "client-update-current", "channel_checked": True}

        with (
            patch.object(wizard, "manage_update", side_effect=resume),
            patch.object(wizard, "probe_launcher", return_value=None),
        ):
            code, value = normal_retry(self, self.f.home, self.f.native)
        self.assertEqual(code, 0, value)
        self.assertTrue(value["installed"])
        self.assertEqual(updates, ["update"])
        self.assertFalse(marker.exists())

    def test_actual_053_validator_rejects_nine_tools_with_057_active_then_handover_repairs(
        self,
    ):
        fixture = Path(__file__).parent / "fixtures/windows_launcher_053"
        pins = json.loads((fixture / "manifest.json").read_bytes())["files"]
        source = fixture / "content_install.py.fixture"
        self.assertEqual(ci.sha(source.read_bytes()), pins[source.name])
        historical = runpy.run_path(str(source))
        self.assertEqual(self.before["version"], "0.5.7")
        registered=ci.tomllib.loads(self.f.config.read_text())["mcp_servers"][ci.NAME]["enabled_tools"]
        self.assertIn('memory_query',registered)
        self.assertIn('content_queue_inspect',registered)
        self.assertEqual(
            historical["inspect"](self.f.home)["reason"], "mcp-binding-changed"
        )
        self.assertTrue(self.handover())
        self.assertTrue(ci.inspect(self.f.home)["registration_verified"])

    def test_trusted_handover_keeps_policy_and_paused_state_then_update_activates(self):
        config = ci.tomllib.loads(self.f.config.read_text())
        config["mcp_servers"][ci.NAME]["enabled"] = False
        self.f.config.write_text(
            self.f.config.read_text().replace("enabled = true", "enabled = false")
        )
        policy = Path(self.meta["release_policy"]).read_bytes()
        self.assertTrue(self.handover())
        self.assertEqual(self.channel.verified_active(), self.before)
        self.assertEqual(
            ci.inspect(self.f.home)["installation_state"], "verified-disabled"
        )
        current = ci.private_json(
            self.f.home / ".config/agent-brain-client/codex-mcp.json"
        )
        self.assertNotEqual(current["package"], self.meta["package"])
        self.assertEqual(
            current["release_policy_sha256"], self.meta["release_policy_sha256"]
        )
        self.assertEqual(Path(current["release_policy"]).read_bytes(), policy)
        self.assertEqual(self.channel.update()["active"]["version"], "0.5.8")

    def test_failed_postcheck_rolls_back_exact_config_and_registration(self):
        before = self.f.config.read_bytes()
        meta = ci.raw(self.f.home / ".config/agent-brain-client/codex-mcp.json")
        calls = []

        def check(plan, active):
            calls.append(True)
            if len(calls) == 3:
                raise OSError("Synthetic postcheck failure")

        with (
            patch.object(wizard, "probe_launcher", side_effect=check),
            self.assertRaises(ValueError),
        ):
            wizard.windows_launcher_handover(
                self.f.home, channel=self.channel, before=self.before, meta=self.meta
            )
        self.assertEqual(self.f.config.read_bytes(), before)
        self.assertEqual(
            ci.raw(self.f.home / ".config/agent-brain-client/codex-mcp.json"), meta
        )
        self.assertFalse(
            (self.f.home / ".config/agent-brain-client/launcher-handover.json").exists()
        )

    def test_exact_candidate_already_active_is_accepted_on_handover_resume(self):
        marker = self.f.home / ".config/agent-brain-client/launcher-handover.json"
        original = ci.save

        def stop(path, value):
            original(path, value)
            if path == marker:
                raise SystemExit("Synthetic stop")

        with patch.object(ci, "save", side_effect=stop), self.assertRaises(SystemExit):
            self.handover()
        self.assertEqual(self.channel.update()["active"]["version"], "0.5.8")
        with patch.object(wizard, "probe_launcher", return_value=None):
            self.assertTrue(
                wizard.windows_launcher_handover(
                    self.f.home, channel_factory=lambda *a: self.channel
                )
            )
        self.assertTrue(ci.inspect(self.f.home)["registration_verified"])

    def test_interrupted_plan_is_resumed_and_does_not_reapprove_or_repair_digest(self):
        original = ci.save
        marker = self.f.home / ".config/agent-brain-client/launcher-handover.json"

        def stop(path, value):
            original(path, value)
            if path == marker:
                raise SystemExit("Synthetic stop before handover")

        with patch.object(ci, "save", side_effect=stop), self.assertRaises(SystemExit):
            self.handover()
        with patch.object(wizard, "probe_launcher", return_value=None):
            self.assertTrue(
                wizard.windows_launcher_handover(
                    self.f.home, channel_factory=lambda *a: self.channel
                )
            )
        self.assertFalse(marker.exists())
        self.assertTrue(ci.inspect(self.f.home)["registration_verified"])


@unittest.skipIf(
    os.name == "nt",
    "Portable Mac fixture; native Hook transaction is in test_windows_native",
)
class SourceClosure(unittest.TestCase):
    def setUp(self):
        self.f = lifecycle_fixture.ClientUninstall(
            "test_cancel_is_read_only_and_status_never_reads_credentials"
        )
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.f.home
        hooks.install(
            SimpleNamespace(
                device_home=str(self.home),
                profile_id=None,
                operator=None,
                operator_sha256=None,
            )
        )

    def answers(self, approve=True):
        return iter(
            [
                "Choose sources",
                str(self.home),
                "",
                "",
                "",
                "Approve" if approve else "Later",
            ]
        )

    def test_normal_entry_reports_mapping_hook_and_source_stages_without_claiming_load(
        self,
    ):
        original = ci.registered_context

        def windows_policy(*args, **kwargs):
            policy, mcp, sha, native = original(*args, **kwargs)
            policy = copy.deepcopy(policy)
            policy["platform"] = "win32-amd64"
            return policy, mcp, sha, native

        with (
            patch.object(ci, "registered_context", side_effect=windows_policy),
            patch.object(sys, "stdout", io.StringIO()),
        ):
            result = guide.complete(
                self.home,
                {"installed": True},
                choose=lambda prompt: "Later",
                refresh=lambda *a: {
                    "status": "content-refreshed",
                    "model_loaded": "unknown",
                },
            )
        self.assertEqual(
            result["installation_stages"]["mapping"]["status"], "content-refreshed"
        )
        self.assertEqual(
            result["installation_stages"]["hook"]["status"], "global-hook-installed"
        )
        self.assertEqual(
            result["installation_stages"]["sources"]["status"],
            "source-selection-required",
        )
        self.assertEqual(
            result["installation_stages"]["worker"], "unobserved"
        )
        self.assertFalse(result["installation_complete"])

    def test_defer_or_cancel_never_reads_sources_or_activates_credentials(self):
        before = gm.settings_path(self.home).read_bytes()
        for answers in (iter(["Later"]),):
            with (
                patch.object(
                    gm, "GlobalClient", side_effect=AssertionError("No credentials")
                ),
                patch.object(gm, "discover", side_effect=AssertionError("No scan")),
                patch.object(sys, "stdout", io.StringIO()),
            ):
                result = guide.source_setup(
                    self.home, choose=lambda prompt: next(answers)
                )
            self.assertFalse(result["collection_enabled"])
        self.assertEqual(gm.settings_path(self.home).read_bytes(), before)

    def test_cancel_after_pool_lookup_preserves_local_settings(self):
        before = gm.settings_path(self.home).read_bytes()
        client = SimpleNamespace(device_id="d" * 32, wire=lambda *a: {"result": {
            "metadata_only": True, "source_device_id": "d" * 32,
            "source_registered": False, "global_profile_id": "e" * 32}})
        answers = self.answers(False)
        with (patch.object(gm, "GlobalClient", return_value=client),
              patch.object(gm, "discover", side_effect=AssertionError("No scan")),
              patch.object(sys, "stdout", io.StringIO())):
            result = guide.source_setup(self.home, choose=lambda prompt: next(answers))
        self.assertFalse(result["collection_enabled"])
        self.assertEqual(gm.settings_path(self.home).read_bytes(), before)

    def test_existing_or_unconfirmed_server_source_is_not_overwritten(self):
        before = gm.settings_path(self.home).read_bytes()
        for response in (
            {"error": {"code": "global_source_device_denied"}},
            {"result": {"metadata_only": True, "source_device_id": "d" * 32,
                        "source_registered": True, "global_profile_id": "e" * 32}},
            {"result": {"metadata_only": True, "source_device_id": "f" * 32,
                        "source_registered": False, "global_profile_id": "e" * 32}},
        ):
            client = SimpleNamespace(device_id="d" * 32, wire=lambda *a: response)
            with (self.subTest(response=response),
                  patch.object(gm, "GlobalClient", return_value=client),
                  patch.object(sys, "stdout", io.StringIO())):
                result = guide.source_setup(self.home, choose=lambda prompt: "Choose sources")
                self.assertEqual(result["status"], "source-server-status-review-required")
                self.assertFalse(result["collection_enabled"])
                self.assertEqual(gm.settings_path(self.home).read_bytes(), before)

    def test_explicit_local_selection_packet_and_authenticated_activation(self):
        events = []

        class Client:
            def __init__(self, value):
                self.value = value
                self.device_id = "d" * 32

            def wire(self, method, params):
                events.append(method)
                if method == 'memory_global_resume':
                    return {'result': {'schema': 1, 'stopped': False}}
                if method == "memory_global_status" and params == {}:
                    return {"result": {"metadata_only": True, "source_device_id": self.device_id,
                                       "source_registered": False, "global_profile_id": "e" * 32}}
                return {
                    "result": {
                        "scope_sha256": digest(self.value["scope"]),
                        "global_profile_id": self.value["scope"]["profile_id"],
                        "source_device_id": self.device_id,
                    }
                }

        answers = self.answers()
        with (
            patch.object(gm, "GlobalClient", Client),
            patch.object(gm, "discover", side_effect=AssertionError("No scan")),
            patch.object(sys, "stdout", io.StringIO()),
        ):
            result = guide.source_setup(self.home, choose=lambda prompt: next(answers))
        self.assertTrue(result["collection_enabled"])
        self.assertTrue(gm.approved(gm.load(self.home)))
        self.assertEqual(events, ["memory_global_status", "memory_global_status", "memory_global_resume"])
        self.assertEqual(gm.load(self.home)["scope"]["profile_id"], "e" * 32)
        self.assertTrue(hooks.installation_status(gm.load(self.home)))
        self.assertTrue(
            (gm.settings_path(self.home).parent / "device-source-packet.json").is_file()
        )

    def test_missing_server_admission_has_packet_specific_todo_and_stays_fenced(self):
        class Client:
            def __init__(self, value):
                self.device_id = "d" * 32

            def wire(self, *args):
                if args == ("memory_global_status", {}):
                    return {"result": {"metadata_only": True, "source_device_id": self.device_id,
                                       "source_registered": False, "global_profile_id": "e" * 32}}
                return {"error": {"code": "source-not-admitted"}}

        answers = self.answers()
        with (
            patch.object(gm, "GlobalClient", Client),
            patch.object(sys, "stdout", io.StringIO()),
        ):
            result = guide.source_setup(self.home, choose=lambda prompt: next(answers))
        self.assertFalse(result["collection_enabled"])
        self.assertFalse(gm.approved(gm.load(self.home)))
        self.assertIn("--activate-packet", result["next_action"])

    def test_hook_torn_write_recovers_and_keeps_user_toml_and_mcp(self):
        config = self.f.native / "config.toml"
        inode = config.stat().st_ino
        original = ci.tomllib.loads(config.read_text())["mcp_servers"]
        body = hooks.split_owned(config.read_bytes())[1].replace(
            "timeout = 120", "timeout = 119"
        )
        with patch.object(host, "host_stage", side_effect=inplace_stage):

            def tear(file, raw):
                file.seek(0)
                file.write(raw[: len(raw) // 2])
                file.flush()
                raise SystemExit("Synthetic Hook interruption")

            with (
                patch.object(host, "host_write", side_effect=tear),
                self.assertRaises(SystemExit),
            ):
                hooks.transact(self.f.native, gm.settings_path(self.home).parent, body)
            hooks.transact(self.f.native, gm.settings_path(self.home).parent, body)
        self.assertEqual(config.stat().st_ino, inode)
        self.assertEqual(ci.tomllib.loads(config.read_text())["mcp_servers"], original)
        self.assertTrue(hooks.installation_status(gm.load(self.home)))

    def test_normal_install_retry_restores_torn_hook_before_registration_parse(self):
        inode = (self.f.native / "config.toml").stat().st_ino
        before = torn_hook(self, self.home, self.f.native)
        self.assertFalse(ci.inspect(self.home).get("registration_verified"))
        with (
            patch.object(host, "host_stage", side_effect=inplace_stage),
            patch.object(
                wizard,
                "manage_update",
                side_effect=AssertionError("No launcher change for Hook recovery"),
            ),
        ):
            code, value = normal_retry(self, self.home, self.f.native)
        self.assertEqual(code, 0, value)
        self.assertTrue(value["installed"])
        self.assertEqual((self.f.native / "config.toml").read_bytes(), before)
        self.assertEqual((self.f.native / "config.toml").stat().st_ino, inode)
        hooks.install(
            SimpleNamespace(
                device_home=str(self.home),
                profile_id=None,
                operator=None,
                operator_sha256=None,
            )
        )
        self.assertTrue(hooks.installation_status(gm.load(self.home)))

    def test_early_hook_recovery_retains_foreign_edits_and_native_binding(self):
        torn_hook(self, self.home, self.f.native)
        file = self.f.native / "config.toml"
        foreign = b"foreign user edit\n"
        file.write_bytes(foreign)
        value = guide.resume_setup(self.home, self.f.native)
        self.assertEqual(value["status"], "windows-setup-recovery-review-required")
        self.assertEqual(file.read_bytes(), foreign)

    def test_hook_reinstall_preserves_existing_identity_scope_and_pause(self):
        value = gm.load(self.home)
        value["scope"]["include_roots"] = [
            {"root": str(self.home), "sharing": "personal"}
        ]
        value.update(
            enabled=True,
            approved_scope_sha256=digest(value["scope"]),
            server_scope_sha256=digest(value["scope"]),
        )
        gm.old.write(gm.settings_path(self.home), value)
        off = gm.settings_path(self.home).parent / "global.off"
        off.touch(mode=0o600)
        result = hooks.install(
            SimpleNamespace(
                device_home=str(self.home),
                profile_id=None,
                operator=None,
                operator_sha256=None,
            )
        )
        self.assertFalse(result["expanded_sources_enabled"])
        self.assertEqual(gm.load(self.home), value)
        self.assertTrue(off.exists())

    def test_replacing_mcp_table_preserves_inline_hook_ownership_marker(self):
        config = self.f.native / "config.toml"
        before = config.read_bytes()
        table = ci.tomllib.loads(before.decode())["mcp_servers"][ci.NAME]
        expected = hooks.split_owned(before)[1]
        rendered = ci.render(before, table, True)
        self.assertEqual(hooks.split_owned(rendered)[1], expected)


if __name__ == "__main__":
    unittest.main()

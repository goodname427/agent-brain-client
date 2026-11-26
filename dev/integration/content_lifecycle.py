"""Exercise the packaged MCP worker against two isolated Server checkouts."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import uuid

CLIENT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CLIENT / "src"))
from agent_brain_client.adapters.packages.build import release
from agent_brain_client.adapters.platforms.filesystem import native_platform
from agent_brain_client.adapters.server.https import profile_metadata
from agent_brain_client.domain.tool_contract import TOOL_CONTRACT_VERSION
from agent_brain_client.domain.validation import encode

CURRENT_SERVER = "e1ec48f4eb2a3ccfa53f8ecdfaf9baba4f7c3e24"
LEGACY_SERVER = "e3639ccd0a9e30a1920cc3d975083e74bd591e8e"
WORKER = Path("src/agent_brain_client/interfaces/mcp/content.py")
RETIRED_TOOLS = {"content_archive", "content_restore", "content_remove"}
ACTIONS = ("archive", "restore")
HARNESSES = ("codex", "codebuddy")
VERSION = "0.8.31"

# Only the subprocess imports Server code. It uses the Server's disposable
# owner enrollment, content policy, Git repository and public device gate.
SERVER = r'''
import copy, io, json, ssl, sys, threading
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server
root = Path(sys.argv[1])
sys.path[:0] = [str(root / p) for p in ("src", "tests", ".")]
from test_owner_scope import OwnerScope
from test_personal_content import RULE, SKILL
from agent_brain_server.interfaces.http.device_api import PublicDeviceAPI
test = OwnerScope("test_complete_scope_and_read_put_only_contract")
test.setUp()
f = test.f
gate = {"schema": 1, "grant": copy.deepcopy(f.grant), "global_per_minute": 250,
        "device_per_minute": 200, "pairing_per_minute": 20, "max_devices": 32}
def restart():
    f.restart()
    f.case.http.device_application = PublicDeviceAPI(f.case.app, gate)
try:
    for kind, value in (("rule", RULE), ("skill", SKILL)):
        status, saved = f.put(kind, dict(value, enabled=False, **(
            {"dependencies": {"commands": [], "python_modules": []}} if kind == "skill" else {}
        )))
        assert status == 200, saved
    restart()
    drop = False
    writes = []
    class Quiet(WSGIRequestHandler):
        def log_message(self, *_args):
            pass
    def handle(environ, start):
        global drop
        environ["PATH_INFO"] = "/v1/device/requests"
        raw = environ["wsgi.input"].read(int(environ["CONTENT_LENGTH"]))
        environ["wsgi.input"] = io.BytesIO(raw)
        request = json.loads(raw)
        if request["method"] in ("content_archive", "content_restore"):
            writes.append([request, environ.get("HTTP_X_AGENT_BRAIN_WRITE_RECEIPT")])
        headers = []
        body = b"".join(f.case.http(environ, lambda *args: headers.extend(args)))
        if drop and request["method"] == "content_archive" and headers[0].startswith("200"):
            drop = False
            body = b"synthetic missing receipt"
            headers = ["200 OK", [("Content-Length", str(len(body)))]]
        start(*headers)
        return [body]
    with make_server("127.0.0.1", 0, handle, handler_class=Quiet) as http:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(sys.argv[2], sys.argv[3])
        http.socket = context.wrap_socket(http.socket, server_side=True)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        print(json.dumps({"endpoint": f"https://127.0.0.1:{http.server_port}",
                          "grant": f.grant, "credential": f.token,
                          "device_id": f.authority.authenticate(f.token)["device_id"]}), flush=True)
        for line in sys.stdin:
            action = json.loads(line)
            if action == "drop":
                drop = True
            if action == "restart":
                restart()
            print(json.dumps({"head": f.case.head(), "writes": writes}), flush=True)
        http.shutdown()
finally:
    test.doCleanups()
'''

# Execute the candidate's real managed stdio entry and HTTPS client. Only the
# Keychain/Credential Manager record is injected, from synthetic stdin.
WORKER_ENTRY = r'''
import json, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
package = Path(sys.argv[1])
sys.path.insert(0, str(package / "src"))
from agent_brain_client.adapters.server import https
from agent_brain_client.interfaces.mcp import content
saved = json.loads(sys.stdin.buffer.readline())
sys.argv = [content.__file__, *sys.argv[2:]]
with patch.object(https, "native_store", return_value=SimpleNamespace(load=lambda: saved)):
    content.main()
'''


def source_pin(path, expected):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()
    if git("rev-parse", "HEAD") != expected or git("status", "--porcelain"):
        raise ValueError(f"Expected a clean synthetic Server checkout at {expected}")


def receive(process):
    line = process.stdout.readline()
    if not line:
        raise RuntimeError("Synthetic process closed its response stream")
    return json.loads(line)


def send(process, value):
    process.stdin.write(json.dumps(value) + "\n")
    process.stdin.flush()


@contextmanager
def child(code, *args, env):
    with subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", code, *map(str, args)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8", env=env,
    ) as process:
        try:
            yield process
        finally:
            process.stdin.close()
            try:
                status = process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            if status:
                raise RuntimeError(f"Synthetic process exited with status {status}")


def environment(home, cert):
    home.mkdir(mode=0o700, exist_ok=True)
    return dict(
        os.environ, HOME=str(home), USERPROFILE=str(home),
        CODEX_HOME=str(home / ".codex"), SSL_CERT_FILE=str(cert),
    )


class Wire:
    def __init__(self, process, commit, harness):
        self.process = process
        self.commit = commit
        self.sequence = 0
        self.ready = receive(process)
        assert self.ready["toolContract"] == TOOL_CONTRACT_VERSION, self.ready
        hello = self.rpc("initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": f"synthetic-{harness}-lifecycle", "version": "1"},
        })
        assert hello["result"]["serverInfo"]["version"] == VERSION, hello
        send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    def rpc(self, method, params=None):
        self.sequence += 1
        request = {"jsonrpc": "2.0", "id": self.sequence, "method": method}
        if params is not None:
            request["params"] = params
        send(self.process, request)
        result = receive(self.process)
        assert result["id"] == self.sequence, result
        return result

    def call(self, name, **arguments):
        return self.rpc("tools/call", {"name": name, "arguments": arguments})

    def content(self, name, **arguments):
        result = self.call(name, **arguments)["result"]["structuredContent"]
        assert result["client_runtime"]["running_worker_commit"] == self.commit, result
        return result

    def lifecycle(self, action, **arguments):
        return self.content("content_lifecycle", action=action, **arguments)

    def tools(self):
        return {tool["name"]: tool for tool in self.rpc("tools/list")["result"]["tools"]}


def private_json(path, value):
    from agent_brain_client.adapters.storage.host_files import atomic

    raw = encode(value)
    atomic(path, raw, 0o600, cache=True)
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def worker(package, home, cert, hello, policy, harness):
    env = environment(home, cert)
    grant = hello["grant"]
    windows = grant["platform"].startswith("win32-")
    credential_source = package / "src/agent_brain_client/adapters/platforms" / (
        "windows/native.py" if windows else "macos/native/CredentialStore.swift"
    )
    source = hashlib.sha256(credential_source.read_bytes()).hexdigest()
    backend = {"kind": "windows-credential-manager", "source_sha256": source}
    if not windows:
        backend.update(kind="mac-login-keychain", helper=str(home / "synthetic-helper"), helper_sha256="a" * 64)
    profile = profile_metadata(
        hello["endpoint"], grant, backend, str(home / ".config/agent-brain-client/https"),
    )
    profile_path, policy_path = home / "synthetic-profile.json", home / "synthetic-policy.json"
    digest = private_json(profile_path, profile)
    policy_digest = private_json(policy_path, policy)
    with child(
        WORKER_ENTRY, package, "--managed-stdio", "initial", "--client-version", VERSION,
        "--harness", harness, "--device-home", home, "--codex-home", home / ".codex",
        "--cache", home / "synthetic-cache", "--content-policy", policy_path,
        "--content-policy-sha256", policy_digest, "--https-profile", profile_path,
        "--https-profile-sha256", digest, "--credential-source-sha256", source, env=env,
    ) as process:
        send(process, {
            "schema": 1, "profile_sha256": digest, "phase": "active",
            "nonce": uuid.uuid4().hex, "credential": hello["credential"],
            "device_id": hello["device_id"], "grant": grant,
        })
        commit = json.loads((package / "verified.json").read_bytes())["commit"]
        yield Wire(process, commit, harness)


def describe(packages, grant, policy):
    tools = []
    for target, package in packages.items():
        raw = subprocess.check_output(
            [sys.executable, "-I", "-B", str(package / WORKER), "--describe-tools"],
            input=json.dumps({"grant": dict(grant, platform=target), "policy": policy}),
            text=True,
        )
        result = json.loads(raw)
        assert result["version"] == TOOL_CONTRACT_VERSION, result
        tools.append(set(result["tools"]))
    assert tools[0] == tools[1] and "content_lifecycle" in tools[0] and not RETIRED_TOOLS & tools[0], tools
    return tools[0]


def queue_saved(reply):
    assert reply["local_queue"]["server_saved"] is True, reply
    assert reply["local_refresh"]["status"] in ("content-refreshed", "content-current"), reply
    return reply["result"]


def request_id():
    return "synthetic_" + uuid.uuid4().hex


def exercise(server, root, cert, key, packages, target, harness, *, legacy):
    env = environment(root / "server-home", cert)
    with child(SERVER, server, cert, key, env=env) as process:
        hello = receive(process)
        hello["grant"]["platform"] = target
        policy = {
            "schema": 1, "adapter": "codex", "kinds": hello["grant"]["artifact_types"],
            "ids": hello["grant"]["ids"], "dependencies": {"commands": [], "python_modules": []},
        }
        names = describe(packages, hello["grant"], policy)
        home = root / "worker-home"
        package = packages[target]

        def state(action="state"):
            send(process, action)
            return receive(process)

        def mapped(kind):
            if harness == "codebuddy":
                name = "skills/synthetic-skill/SKILL.md" if kind == "skill" else "rules/agent-brain-synthetic-rule.md"
                return (home / ".codebuddy" / name).is_file()
            if kind == "skill":
                return (home / ".agents/skills/synthetic-skill/SKILL.md").is_file()
            agents = home / ".codex/AGENTS.md"
            return agents.is_file() and "Synthetic test guidance." in agents.read_text(encoding="utf-8")

        def catalog(wire):
            reply = wire.content("content_catalog", kinds=["rule", "skill"])
            return {row["id"] for row in reply["result"]["artifacts"]}

        for kind in ("rule", "skill"):
            item = {"kind": kind, "id": "synthetic-" + kind}
            with worker(package, home, cert, hello, policy, harness) as wire:
                tools = wire.tools()
                assert set(tools) == names, tools
                assert tools["content_read"]["inputSchema"]["properties"]["include_archived"]["enum"] == [True]
                schema = tools["content_lifecycle"]["inputSchema"]
                assert set(schema["required"]) == {"action", "kind", "id", "expected_revision", "request_id"}
                assert schema["properties"]["action"]["enum"] == list(ACTIONS)
                unavailable = dict(item, expected_revision="a" * 64, request_id=request_id())
                before = state()
                for name in RETIRED_TOOLS:
                    assert wire.call(name, **unavailable)["error"]
                assert wire.call("content_lifecycle", action="remove", **unavailable)["error"]
                assert state() == before
                assert wire.call("content_read", **dict(item, kind="memory"), include_archived=True)["error"]
                ordinary = wire.content("content_read", **item)["result"]
                assert "archived" not in ordinary, ordinary
                original = queue_saved(wire.content(
                    "content_set_enabled", **item, enabled=True,
                    expected_revision=ordinary["revision"], request_id=request_id(),
                ))["record"]
                assert mapped(kind)
                lifecycle = wire.content("content_read", **item, include_archived=True)
                archive = dict(item, expected_revision=ordinary["revision"], request_id=request_id())
                if legacy:
                    before = state()["head"]
                    assert lifecycle["error"]["retryable"] is False, lifecycle
                    for action in ACTIONS:
                        refusal = wire.lifecycle(action, **dict(archive, request_id=request_id()))
                        assert refusal["error"]["code"] == "device_method_denied", refusal
                        assert refusal["local_queue"]["state"] == "blocked", refusal
                    assert state()["head"] == before and mapped(kind) and item["id"] in catalog(wire)
                    assert wire.content("content_read", **item)["result"]["record"] == original
                    live = wire.content("content_read", **item)["result"]
                    queue_saved(wire.content(
                        "content_set_enabled", **item, enabled=False,
                        expected_revision=live["revision"], request_id=request_id(),
                    ))
                    assert not mapped(kind)
                    continue
                assert lifecycle["result"]["record"] == original and lifecycle["result"]["archived"] is False
                archive["expected_revision"] = lifecycle["result"]["revision"]
                if kind == "rule":
                    state("drop")
                    pending = wire.lifecycle("archive", **archive)
                    assert pending["local_queue"]["state"] == "pending" and mapped(kind), pending
                    committed = state("restart")["head"]
                    sent = state()
                    for retry in (
                        wire.lifecycle("restore", **archive),
                        wire.lifecycle("archive", **dict(archive, expected_revision="c" * 64)),
                    ):
                        assert retry["error"]["code"] == "content_adapter_blocked", retry
                    assert state() == sent
                else:
                    queue_saved(wire.lifecycle("archive", **archive))
                    assert not mapped(kind)
                    committed = state()["head"]

            with worker(package, home, cert, hello, policy, harness) as wire:
                saved = queue_saved(wire.lifecycle("archive", **archive))
                assert saved["archived"] is True and saved["record"] is None and not mapped(kind)
                assert state()["head"] == committed and item["id"] not in catalog(wire)
                if kind == "rule":
                    assert [req for req, _ in state()["writes"] if req["request_id"] == archive["request_id"]] == [
                        {"method": "content_archive", "params": {k: v for k, v in archive.items() if k != "request_id"},
                         "request_id": archive["request_id"]},
                    ] * 2
                assert wire.content("content_read", **item)["result"]["revision"] == "missing"
                archived = wire.content("content_read", **item, include_archived=True)["result"]
                assert archived == saved, archived
                stale = dict(archive, request_id=request_id())
                conflict = wire.lifecycle("restore", **stale)
                assert conflict["error"]["code"] == "revision_conflict" and state()["head"] == committed
                inspected = wire.call("content_queue_inspect", request_id=stale["request_id"])
                assert inspected["result"]["structuredContent"]["remote_snapshot"] == archived, inspected
                restore = dict(item, expected_revision=archived["revision"], request_id=request_id())
                restored = queue_saved(wire.lifecycle("restore", **restore))
                assert restored["record"] == dict(original, enabled=False) and restored["archived"] is False
                assert restored["id"] == item["id"] and restored["revision"] != archived["revision"]
                restored_head = state()["head"]
                assert queue_saved(wire.lifecycle("restore", **restore)) == restored
                assert state()["head"] == restored_head and not mapped(kind) and item["id"] in catalog(wire)
                assert wire.content("content_read", **item, include_archived=True)["result"] == restored
                ordinary = wire.content("content_read", **item)["result"]
                assert ordinary["record"] == restored["record"] and ordinary["revision"] != restored["revision"]

        if not legacy:
            assert {header for _, header in state()["writes"]} == {"local-durable-v1"}
        before = state()["head"]
        invalid = dict(hello, credential="synthetic_invalid_credential_" + uuid.uuid4().hex)
        with worker(package, root / "denied-home", cert, invalid, policy, harness) as wire:
            target_args = {"kind": "rule", "id": "synthetic-rule"}
            read = wire.content("content_read", **target_args, include_archived=True)
            write = wire.lifecycle("archive", **target_args, expected_revision="b" * 64, request_id=request_id())
            for reply in (read, write):
                assert reply["error"]["code"] == "device_authentication_failed", reply
            assert state()["head"] == before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-root", required=True, type=Path)
    parser.add_argument("--legacy-server-root", required=True, type=Path)
    args = parser.parse_args()
    source_pin(args.server_root, CURRENT_SERVER)
    source_pin(args.legacy_server_root, LEGACY_SERVER)
    commit = subprocess.check_output(["git", "-C", str(CLIENT), "rev-parse", "HEAD"], text=True).strip()
    target = native_platform()
    mac = target if target.startswith("darwin-") else "darwin-arm64"
    win = target if target.startswith("win32-") else "win32-amd64"
    with tempfile.TemporaryDirectory(prefix="ab-lifecycle-") as temporary:
        root = Path(temporary).resolve()
        cert, key = root / "localhost.pem", root / "localhost-key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ], check=True, capture_output=True)
        ssl.create_default_context(cafile=str(cert))
        packages = {mac: root / "mac", win: root / "windows"}
        for platform, package in packages.items():
            manifest = release(CLIENT, package, VERSION, "personal-content-https", platform)
            private_json(package / "verified.json", {"commit": commit, "manifest": manifest})
        for checkout, legacy in ((args.server_root, False), (args.legacy_server_root, True)):
            for harness in HARNESSES:
                run = root / f"{'legacy' if legacy else 'current'}-{harness}"
                run.mkdir(mode=0o700)
                exercise(checkout.resolve(), run, cert, key, packages, target, harness, legacy=legacy)
    print(json.dumps({
        "status": "content-lifecycle-interop-passed", "version": VERSION, "client_head": commit,
        "worker_platform": target, "harnesses": HARNESSES, "package_tool_manifests": [mac, win],
        "server_head": CURRENT_SERVER, "legacy_server_head": LEGACY_SERVER,
        "real_loopback_https": True, "synthetic_enrollment_only": True,
    }))


if __name__ == "__main__":
    main()

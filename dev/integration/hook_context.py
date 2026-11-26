"""Measure decoded model-request bodies through a real CLI and a loopback API."""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import Queue
import re
import shlex
import subprocess
import sys
import tempfile
from threading import Thread
from time import monotonic


ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_KEY = "not-a-real-model-credential"
MARKERS = ("BEGIN_SYNTHETIC_CONTEXT", "MID_SYNTHETIC_EVIDENCE", "END_SYNTHETIC_CONTEXT")
MODEL = "claude-sonnet-4-5"


class Capture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append(json.loads(data))
        events = self.response_events()
        stream = "".join(
            "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
            for event in events
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(stream)))
        self.end_headers()
        self.wfile.write(stream)

    def response_events(self):
        if self.server.host == "codex":
            message = {
                "id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Synthetic response", "annotations": []}],
            }
            return [
                {"type": "response.created", "response": {"id": "resp_fixture", "model": MODEL}},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": dict(message, status="in_progress", content=[])},
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.completed", "response": {"id": "resp_fixture", "output": [message],
                 "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}},
            ]
        message = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": MODEL,
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 0, "output_tokens": 0}}
        return [
            {"type": "message_start", "message": message},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "Synthetic response"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]


def model_text(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from model_text(child)
    elif isinstance(value, list):
        for child in value:
            yield from model_text(child)
    elif isinstance(value, str):
        yield value


def fixture_body(size, cjk):
    fixture = json.loads((ROOT / "tests/fixtures/automatic_memory_context.json").read_text(encoding="utf-8"))
    a, b, *_ = fixture["evidence"]
    header = f"{MARKERS[0]}\n[Project A] {a['text']}\n"
    focus = f"\n{MARKERS[1]}\n[Project B] {b['text']}\n"
    footer = f"\n{MARKERS[2]}\n"
    padding = size - len((header + focus + footer).encode())
    if padding < 0:
        raise ValueError("Requested bytes must fit the synthetic evidence")

    def pad(count):
        return "证" * (count // 3) + "x" * (count % 3) if cjk else "x" * count

    half = padding // 2
    return header + pad(half) + focus + pad(padding - half) + footer


def hook_main(body_file):
    sys.path.insert(0, str(ROOT / "src"))
    from agent_brain_client.adapters.harnesses.hook_output import hook_context
    from agent_brain_client.domain.final_context import FinalContext

    body = body_file.read_bytes().decode("utf-8")
    print(json.dumps(hook_context("UserPromptSubmit", FinalContext(body, ())), ensure_ascii=False))


def approve_fixture_hook(executable, env, workspace, config_file, hook_command):
    # Use the host-reported identity for the single command this fixture
    # created. Only the temporary profile receives the trust entry.
    messages = [
        {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "synthetic-fixture", "version": "0.0.0"},
            "capabilities": {"experimentalApi": True},
        }},
        {"method": "initialized"},
        {"id": 2, "method": "hooks/list", "params": {"cwds": [str(workspace)]}},
    ]
    with subprocess.Popen(
        [executable, "app-server", "--listen", "stdio://"], cwd=workspace, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8",
    ) as process:
        result = Queue()

        def read():
            for line in process.stdout:
                row = json.loads(line)
                if row.get("id") == 2:
                    result.put(row)
                    return

        Thread(target=read, daemon=True).start()
        try:
            process.stdin.write("".join(json.dumps(msg) + "\n" for msg in messages))
            process.stdin.flush()
            response = result.get(timeout=10)
        finally:
            process.terminate()
            process.wait(timeout=5)
    entries = response["result"]["data"]
    hooks = entries[0]["hooks"] if len(entries) == 1 else []
    if len(hooks) != 1:
        raise ValueError("Expected one generated Codex fixture Hook")
    hook = hooks[0]
    if (
        hook["command"] != hook_command
        or Path(hook["sourcePath"]).resolve() != config_file
        or hook["eventName"] != "userPromptSubmit"
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", hook["currentHash"])
    ):
        raise ValueError("Unexpected Codex fixture Hook identity")
    with config_file.open("a", encoding="utf-8") as target:
        target.write(f'\n[hooks.state.{json.dumps(hook["key"])}]\n')
        target.write(f'trusted_hash = {json.dumps(hook["currentHash"])}\n')


def configure(host, executable, port, home, workspace, hook_command):
    codex = home / ".codex"
    codex.mkdir()
    env_keys = ("PATH", "LANG", "TMP", "TEMP", "TMPDIR", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR")
    env = {key: os.environ[key] for key in env_keys if key in os.environ}
    env.update(HOME=str(home), CODEX_HOME=str(codex), USERPROFILE=str(home),
               APPDATA=str(home / "AppData" / "Roaming"), LOCALAPPDATA=str(home / "AppData" / "Local"),
               PYTHONIOENCODING="utf-8")
    if host == "codex":
        config = f"""model_provider = "probe"
model = "{MODEL}"
[model_providers.probe]
name = "Local synthetic probe"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
[projects.{json.dumps(str(workspace))}]
trust_level = "trusted"
[[hooks.UserPromptSubmit]]
[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = {json.dumps(hook_command)}
"""
        config_file = codex / "config.toml"
        config_file.write_text(config, encoding="utf-8")
        approve_fixture_hook(executable, env, workspace, config_file, hook_command)
        args = ["exec", "--ephemeral", "--json", "--skip-git-repo-check", "-C", str(workspace)]
    else:
        home_dir = home / ".codebuddy"
        home_dir.mkdir()
        settings = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": hook_command}]}]}}
        (home_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        env.update(CODEBUDDY_API_KEY=SYNTHETIC_KEY, CODEBUDDY_BASE_URL=f"http://127.0.0.1:{port}",
                   CODEBUDDY_CONFIG_DIR=str(home_dir), CODEBUDDY_MODEL=MODEL)
        args = ["-p", "--model", MODEL]
    return env, args


def measure(args):
    with tempfile.TemporaryDirectory(prefix="brain-hook-context-") as temp, ThreadingHTTPServer(
        ("127.0.0.1", 0), Capture,
    ) as server:
        root = Path(temp).resolve()
        home, workspace = root / "home", root / "workspace"
        home.mkdir()
        workspace.mkdir()
        body_file = root / "context.txt"
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "--emit-hook", str(body_file)]
        shell_command = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
        env, invocation = configure(args.host, args.executable, server.server_port, home, workspace, shell_command)
        server.host = args.host
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            if args.server_example:
                result = json.loads((ROOT / "tests/fixtures/server_final_context.json").read_text(encoding="utf-8"))
                bodies = [result["final_context"]["body"]]
                markers = ("amber-47", "github.com/synthetic/project-b")
                evidence = "amber-47"
            else:
                bodies = [fixture_body(size, args.cjk) for size in args.bytes]
                markers, evidence = MARKERS, "COBALT-28"
            for body in bodies:
                size = len(body.encode())
                body_file.write_bytes(body.encode())
                server.requests = []
                start = monotonic()
                result = subprocess.run(
                    [args.executable, *invocation, "What is the release gate for Project B?"],
                    cwd=workspace, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=35,
                )
                duration = round(1000 * (monotonic() - start))
                spans = [span for data in server.requests for span in model_text(
                    data.get("input") if args.host == "codex" else data.get("messages"),
                ) if any(marker in span for marker in markers)]
                exact = any(body in span for span in spans)
                print(json.dumps({
                    "host": args.host, "utf8_bytes": size, "exact_body": exact,
                    "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "middle_evidence": any(evidence in span for span in spans),
                    "model_requests": len(server.requests), "wall_ms": duration,
                    "exit_code": result.returncode,
                }), flush=True)
                if result.returncode or not server.requests or not spans:
                    raise SystemExit("Native model request or Hook context missing")
        finally:
            server.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-hook", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--host", choices=("codex", "codebuddy"))
    parser.add_argument("--executable")
    fixture = parser.add_mutually_exclusive_group()
    fixture.add_argument("--bytes", type=int, nargs="+")
    fixture.add_argument("--server-example", action="store_true")
    parser.add_argument("--cjk", action="store_true")
    args = parser.parse_args()
    if args.emit_hook is not None:
        hook_main(args.emit_hook)
    elif args.host and args.executable and (args.bytes or args.server_example):
        measure(args)
    else:
        parser.error("--host, --executable, and a fixture selector are required")


if __name__ == "__main__":
    main()

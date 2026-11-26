"""Offline subprocess bridge for explicit Server integration; no host identity."""

from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_brain_client.runtime.collection as gm
import agent_brain_client.adapters.server.https as https_content
from agent_brain_client.adapters.server.collection import CollectionClient


def main():
    config = json.loads(sys.stdin.readline())
    wire_input = sys.stdin

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, count):
            return wire_input.readline().encode()

    class Opener:
        def open(self, request, **kwargs):
            print(json.dumps({
                "url": request.full_url, "headers": dict(request.header_items()),
                "request": json.loads(request.data),
            }), flush=True)
            return Response()

    # Only credential storage and the network are replaced at the wire boundary.
    base = SimpleNamespace(
        grant={"actor": config["actor"]}, data_mode="personal",
        device_id=config["device_id"], profile_sha="a" * 64,
        profile={"endpoint": "https://synthetic.invalid"}, queue_identity={},
        _credential=lambda: config["token"], transport=SimpleNamespace(opener=Opener()),
    )
    harness = config.get("harness", "codex")
    final = config.get("final_context") is True
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        directory = Path(temporary).resolve()
        if os.name == "nt":
            from agent_brain_client.adapters.platforms.windows.native import protect_new
            protect_new(directory, directory=True)
        value = dict(config["value"], device_home=str(directory))
        local = config["origin"]
        if harness == "codebuddy":
            workspace = directory / "workspace"
            workspace.mkdir()
            local = dict(local, cwd=str(workspace), project_root=str(workspace), repository_root=str(workspace))
        payload = {
            "cwd": local["cwd"], "hook_event_name": "UserPromptSubmit",
            "session_id": "synthetic-independent-reader", "turn_id": "synthetic-turn",
            "generation_id": "synthetic-generation", "prompt": config.get("query", "Synthetic"),
        }
        registered = {"runtime": {"https_profile": "synthetic-profile", "https_profile_sha256": "a" * 64}}
        observed = []
        for mock in (
            patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")),
            patch.object(https_content, "HTTPSContentClient", return_value=base),
            patch("agent_brain_client.adapters.storage.registration.registration", return_value=(None, registered)),
            patch.object(gm, "load", return_value=value),
            patch.object(gm, "eligible", return_value=local),
            patch.object(gm, "submit_hook", return_value={"status": "staged"}),
            patch.object(gm, "schedule_delivery"),
            patch.object(gm, "settings_path", return_value=directory / "settings.json"),
            patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))),
        ):
            stack.enter_context(mock)
        if final:
            stack.enter_context(patch.object(CollectionClient, "recall_workspace_project",
                                             return_value=config.get("workspace_project")))
        else:
            # The older explicit test sends exactly one schema-1 response.
            stack.enter_context(patch.object(CollectionClient, "capabilities", ()))
        if harness == "codebuddy":
            from agent_brain_client.adapters.harnesses.codebuddy import events, transcript

            stack.enter_context(patch.object(events, "capture_messages", return_value={"status": "staged"}))
            stack.enter_context(patch.object(transcript, "observe", return_value="synthetic"))
        result = gm.hook(value, harness=harness, observe=observed.append)
        context = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        print(json.dumps({
            "status": "synthetic-hook-context-verified", "observation": observed[0],
            "L2_marker_present": "Synthetic L2 continuity marker" in context,
            "L3_marker_present": "Synthetic L3 preference marker" in context,
            "model_invoked": False,
            **({"hook_output": result} if final else {}),
        }), flush=True)


if __name__ == "__main__":
    main()

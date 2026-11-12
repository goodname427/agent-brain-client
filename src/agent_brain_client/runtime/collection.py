"""Compose source collection services for CLI and authenticated Harness gateways."""

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from agent_brain_client.application.collection.control import CollectionControl
from agent_brain_client.application.collection import routing
from agent_brain_client.application.memory.recall import recall as recall_memory


from agent_brain_client.adapters.storage.collection_control import (
    CollectionControlIO,
)
from agent_brain_client.adapters.server.collection import CollectionClient
from agent_brain_client.adapters.storage.collection import (
    settings_path,
    load as load,
    approved,
    admission_state,
)
from agent_brain_client.adapters.storage.origins import discover
from agent_brain_client.adapters.storage.recall_receipts import (
    save_recall_observation as save_recall_observation,
)
from agent_brain_client.adapters.storage import metadata as metadata_io


ROOT = Path(__file__).resolve().parents[3]


def control():
    from agent_brain_client.runtime import project_memory
    return CollectionControl(
        CollectionControlIO(
            settings_path=lambda home: settings_path(home),
            admission=lambda value: admission_state(value),
            client=lambda value: CollectionClient(value),
            project_load=lambda *args: project_memory.load(*args),
            project_command=lambda *args: project_memory.daily_command(*args),
        )
    )


def resume(value):
    return control().resume(value)


def stop(value):
    return control().stop(value)


def configure_recall(value, options):
    return control().configure_recall(value, options)


def native_services():
    return SimpleNamespace(
        load=lambda home: load(home),
        approved=lambda value: approved(value),
        admission=lambda value: admission_state(value),
        discover=lambda cwd: discover(cwd),
        settings_path=lambda home: settings_path(home),
        CollectionClient=lambda *args: CollectionClient(*args),
        eligible=lambda *args: eligible(*args),
        legacy=lambda *args: legacy(*args),
        adapter=lambda *args: adapter(*args),
        submit_hook=lambda *args: submit_hook(*args),
        recall=lambda *args, **kwargs: recall(*args, **kwargs),
        recall_prompt=lambda *args, **kwargs: recall_prompt(*args, **kwargs),
        schedule_delivery=lambda *args: schedule_delivery(*args),
        build_delivery_queue=lambda *args: build_delivery_queue(*args),
        host_status=lambda value: host_status(value),
    )


def eligible(value, cwd):
    return routing.eligible(
        value,
        cwd,
        ports=SimpleNamespace(
            approved=lambda v: approved(v),
            canonical=lambda path: str(Path(path).resolve()),
            discover=lambda path: discover(path),
        ),
    )


def legacy(value, local):
    return routing.delegated_project(value, local)


def adapter(value, local):
    return build_delivery_queue(value, local, "codex")


def submit_hook(value, local, payload):
    from agent_brain_client.adapters.harnesses.codex import collection as native_collection
    return native_collection.submit_hook(
        value, local, payload, services=native_services()
    )


def hook(value, *, harness="codex", observe=None):
    # Omitted host is retained only for existing Codex global-hook launchers.
    if harness == "codex":
        from agent_brain_client.adapters.harnesses.codex import collection as native_collection
        return native_collection.hook(value, observe=observe, services=native_services())
    if harness == "codebuddy":
        import sys
        import json
        from agent_brain_client.adapters.harnesses.codebuddy import events
        from agent_brain_client.runtime.delivery import schedule_assistant_completion
        output, diagnostic = events.hook(
            Path(value["device_home"]), sys.stdin.buffer, core=native_services(),
            defer=lambda turn: schedule_assistant_completion(value["device_home"], turn),
            observe=observe,
        )
        safe = {key: diagnostic[key] for key in (
            "status", "error_type", "recall_status", "recall_error_type",
            "assistant_capture", "timing_ms", "delivery_schedule", "schedule_error_type",
        ) if key in diagnostic}
        if diagnostic.get("error"):
            safe["capture_error"] = diagnostic["error"].get("code", "unconfirmed")
        print(json.dumps(dict(operation="codebuddy-hook", **safe)), file=sys.stderr)
        return output
    raise ValueError("Explicit supported collection Harness required")


def flush(value):
    from agent_brain_client.runtime import collection_management
    return collection_management.flush(value, services=native_services())


def status(
    value, *, check_server=False, request_key=None, event_id=None, session_id=None
):
    from agent_brain_client.runtime import collection_management
    return collection_management.status(
        value,
        check_server=check_server,
        request_key=request_key,
        event_id=event_id,
        session_id=session_id,
        services=native_services(),
    )


recall = partial(
    recall_memory,
    services=SimpleNamespace(
        client=lambda value, local: CollectionClient(value, local),
        settings_path=lambda home: settings_path(home),
        cache_exists=lambda path: path.exists(),
        read_cache=lambda *args, **kwargs: metadata_io.private_json(*args, **kwargs),
        write_cache=lambda *args, **kwargs: metadata_io.write(*args, **kwargs),
        approved=lambda value: approved(value),
    ),
)


def build_delivery_queue(value, local, harness):
    from agent_brain_client.runtime.capture_queues import open_queue
    return open_queue(value, local, harness, services=native_services())


def schedule_delivery(home, harness, local):
    from agent_brain_client.runtime.delivery import schedule_delivery as schedule
    return schedule(home, harness, local)


def recall_prompt(value, local, payload, query, *, observe=None):
    from agent_brain_client.application.memory.prompt_recall import recall_prompt as invoke
    from agent_brain_client.adapters.platforms.filesystem import locked
    from agent_brain_client.adapters.storage.content import operation_stage
    return invoke(value, local, payload, query, observe=observe, services=SimpleNamespace(
        settings_path=lambda home: settings_path(home),
        recall=lambda *args, **kwargs: recall(*args, **kwargs),
        locked=locked, operation_stage=operation_stage, cache_exists=lambda path: path.exists(),
        write=metadata_io.write, save_observation=save_recall_observation,
    ))


def host_status(value):
    home = Path(value["device_home"])
    hosts = {}
    codex_receipt = settings_path(home).parent / "hook-install.json"
    if codex_receipt.exists() or (home / ".config/agent-brain-client/codex/registration.json").exists():
        from agent_brain_client.adapters.harnesses.codex.collection_installation import installation_status
        try:
            from agent_brain_client.adapters.harnesses.codex import binding
            from agent_brain_client.adapters.storage.registration import registration
            _, policy = registration(home)
            native = binding.status(home, policy["runtime"])
            hosts["codex"] = dict(native, mcp_installed=native["installed"], installed=installation_status(value))
        except Exception as error:
            hosts["codex"] = {"installed": False, "status": "review-required", "error_type": type(error).__name__}
    buddy = home / ".config/agent-brain-client/codebuddy/registration.json"
    if buddy.exists():
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.runtime.host_binding import inspect_backend
        try:
            hosts["codebuddy"] = configuration.status(home, None, backend_check=inspect_backend)
            hosts["codebuddy"]["mcp_installed"] = hosts["codebuddy"]["installed"]
            hosts["codebuddy"]["installed"] = hosts["codebuddy"].get("collection_hooks_installed", False) and hosts["codebuddy"]["installed"]
        except Exception as error:
            hosts["codebuddy"] = {"installed": False, "status": "review-required", "error_type": type(error).__name__}
    return hosts

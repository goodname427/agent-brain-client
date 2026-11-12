"""Compose source collection services for CLI and authenticated Harness gateways."""

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from agent_brain_client.application.collection.control import CollectionControl
from agent_brain_client.application.collection import routing
from agent_brain_client.application.memory.recall import recall as recall_memory
from agent_brain_client.adapters.harnesses.codex import collection as native_collection
from agent_brain_client.adapters.harnesses.codex import (
    project_configuration as project_sources,
)
from agent_brain_client.adapters.harnesses.codex.collection_control import (
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
from agent_brain_client.runtime import project_memory

ROOT = Path(__file__).resolve().parents[3]


def control():
    return CollectionControl(
        CollectionControlIO(
            settings_path=lambda home: settings_path(home),
            admission=lambda value: admission_state(value),
            client=lambda value: CollectionClient(value),
            project_load=lambda *args: project_sources.load(*args),
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
    return native_collection.adapter(value, local, services=native_services())


def submit_hook(value, local, payload):
    return native_collection.submit_hook(
        value, local, payload, services=native_services()
    )


def hook(value, *, observe=None):
    return native_collection.hook(value, observe=observe, services=native_services())


def flush(value):
    return native_collection.flush(value, services=native_services())


def status(
    value, *, check_server=False, request_key=None, event_id=None, session_id=None
):
    return native_collection.status(
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
    if harness == "codex":
        return adapter(value, local)
    if harness == "codebuddy":
        from agent_brain_client.adapters.harnesses.codebuddy.events import open_queue
        import sys
        return open_queue(sys.modules[__name__], value, local)
    raise ValueError("Supported Harness required")


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

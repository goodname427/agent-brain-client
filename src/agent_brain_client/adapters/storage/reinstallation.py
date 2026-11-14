"""Private fresh-install journal and retained state; no credentials are copied."""

import base64
from contextlib import ExitStack, contextmanager
import json
import re
from pathlib import PurePosixPath
from pathlib import Path
import secrets
from agent_brain_client.domain import contract
from agent_brain_client.adapters.storage import metadata as io
from agent_brain_client.adapters.storage.host_files import (
    host_plan,
    host_apply,
    host_restore,
    host_row,
)
from agent_brain_client.adapters.platforms.filesystem import (
    private_directory,
    private_mode,
    locked,
    LockTimeout,
    lock_probe,
    lock_resource_users,
)
from agent_brain_client.adapters.platforms.retention import (
    item_identity,
    retain_item,
    retention_root,
    RetentionFailure,
)


RUNTIME_NEXT_ACTION = (
    "Disconnect only the affected Agent Brain MCP service (agent_brain_server) in its independently identified host, "
    "and let its active Hook/transaction finish; keep the IDE and unrelated Clients open. "
    "Resource users are diagnostic candidates, not permission to stop a PID. "
    "If that host cannot disconnect this service or an old worker remains, stop and report it; "
    "do not kill processes, delete locks, change pins or replay queues. Rerun reinstall-check, then the SAME reinstall command."
)


class RuntimeInUse(LockTimeout):
    def __init__(self, path):
        super().__init__("An exact Agent Brain installation lease is in use")
        self.path = Path(path)

    def result(self):
        return {
            "status": "fresh-install-runtime-in-use", "installed": False,
            "native_changed": False, "blocked_lock": str(self.path),
            "resource_users": lock_resource_users(self.path),
            "automatic_shutdown_supported": False,
            "next_action": RUNTIME_NEXT_ACTION,
        }


def journal_path(home):
    return Path(home) / ".config/agent-brain-reinstallation/current.json"


def read_journal(home):
    path = io.canonical(journal_path(home))
    if not path.exists():
        return None
    private_mode(path, owner=True)
    if path.stat().st_size > 8 * 1048576 or path.stat().st_nlink != 1:
        raise ValueError("Bounded private fresh-install journal required")
    value = json.loads(path.read_bytes(), object_pairs_hook=io.unique)
    if (
        value.get("schema") != 1
        or value.get("home") != str(Path(home))
        or value.get("phase")
        not in ("prepared", "detaching", "archiving", "ready", "complete")
    ):
        raise ValueError("Fresh-install journal changed")
    if not re.fullmatch(r"[a-f0-9]{32}", value.get("id", "")):
        raise ValueError("Fresh installation instance changed")
    relative = PurePosixPath(value["cache_relative"])
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or str(relative) != value["cache_relative"]
    ):
        raise ValueError("Retained cache scope changed")
    if (
        Path(value["archive"])
        != Path(home) / ".config/agent-brain-retained" / value["id"]
    ):
        raise ValueError("Retention archive scope changed")
    return value


def require_memory_verified(home, profile_sha=None):
    """Read the owner's durable proof; never repair, activate or infer success."""
    from agent_brain_client.application.installation.fresh import (
        require_verified_memory,
    )

    require_verified_memory(read_journal(home), profile_sha)


def require_collection_ready(home, profile_sha=None):
    from agent_brain_client.application.installation.fresh import (
        require_verified_memory,
        FreshSourceRoutingRequired,
    )

    record = read_journal(home)
    require_verified_memory(record, profile_sha)
    if (
        record is not None
        and any(row["stopped"] or not row["enabled"] for row in record["project_controls"]["states"])
        and record.get("routing_restored") is not True
    ):
        raise FreshSourceRoutingRequired("fresh-install-source-routing-required")


class RetainedInstallation:
    def __init__(
        self,
        home,
        native,
        *,
        host_payload,
        read_mapping,
        restore_mapping,
        read_registration,
        read_project_controls,
    ):
        self.home, self.native = io.canonical(home), io.canonical(native)
        self.root = self.home / ".config/agent-brain-client"
        self.read_registration = read_registration
        self.read_project_controls = read_project_controls
        self.host_payload, self.read_mapping, self.write_mapping = (
            host_payload,
            read_mapping,
            restore_mapping,
        )

    @contextmanager
    def lease(self):
        directory = journal_path(self.home).parent
        private_directory(directory)
        with locked(directory / "reinstall.lock", timeout=0):
            yield

    def load(self):
        value = read_journal(self.home)
        if value and value["phase"] != "complete" and value["native"] != str(self.native):
            raise ValueError("Fresh-install host changed")
        return value

    def save(self, record):
        if len(contract.encode(record)) > 8 * 1048576:
            raise ValueError("Bounded retention journal required")
        previous = read_journal(self.home)
        if previous is not None and previous["id"] != record["id"]:
            from agent_brain_client.application.installation.fresh import require_verified_memory

            require_verified_memory(previous)
            directory = journal_path(self.home).parent / "completed"
            private_directory(directory)
            path = io.canonical(directory / (previous["id"] + ".json"))
            if path.exists():
                if io.private_json(path) != previous:
                    raise ValueError("Completed fresh-install history changed")
            else:
                io.write(path, previous)
        io.write(journal_path(self.home), record)

    def plan(self):
        from agent_brain_client.adapters.packages.client import (
            HTTPS_CONTENT_CAPABILITIES,
        )

        retention_root(self.root)
        registration = self.read_registration(self.home)
        metadata, installed = registration["metadata"], registration["installed"]
        policy_path = io.canonical(Path(metadata["release_policy"]))
        if self.root not in policy_path.parents:
            raise ValueError("Device-local release policy required")
        policy = io.private_json(policy_path, metadata["release_policy_sha256"])
        runtime = policy["runtime"]
        if (
            policy["capabilities"] != HTTPS_CONTENT_CAPABILITIES
            or runtime["device_home"] != str(self.home)
            or runtime["codex_home"] != str(self.native)
        ):
            raise ValueError("Original personal service scope required")
        profile = io.private_json(
            Path(runtime["https_profile"]), runtime["https_profile_sha256"]
        )
        content = io.private_json(
            Path(runtime["content_policy"]), runtime["content_policy_sha256"]
        )
        from agent_brain_client.domain.content_policy import validate_policy

        validate_policy(content, profile["grant"])
        cache = io.canonical(Path(runtime["content_cache"]), existing=True)
        if self.root not in cache.parents:
            raise ValueError(
                "Retain an external content cache separately before reconnecting"
            )
        snapshot = self.read_mapping(self.home, self.native, cache, content)
        payload = self.host_payload(
            self.home, self.native, metadata, installed=installed
        )
        keep = {"install-runtime", "installer-checkouts"}
        if self.root in self.native.parents:
            relative = self.native.relative_to(self.root)
            if len(relative.parts) != 1 or relative.parts[0] != "shared-native":
                raise ValueError("Known shared native home required")
            keep.add("shared-native")
        children = {
            p.name: item_identity(p) for p in self.root.iterdir() if p.name not in keep
        }
        from agent_brain_client.adapters.storage.collection import (
            retained_routing,
            combined_exclusions,
        )

        routing = retained_routing(self.home)
        project_controls = self.read_project_controls(self.home)
        combined_exclusions(
            routing["preferences"]["exclude_roots"] if routing is not None else [],
            project_controls["blocked_roots"],
        )
        identifier = secrets.token_hex(16)
        archive = self.home / ".config/agent-brain-retained" / identifier
        return {
            "schema": 1,
            "phase": "prepared",
            "id": identifier,
            "home": str(self.home),
            "native": str(self.native),
            "archive": str(archive),
            "children": children,
            "keep": sorted(keep),
            "host": host_plan(payload),
            "endpoint": profile["endpoint"],
            "grant": profile["grant"],
            "content": content,
            "mapping": snapshot,
            "memory_ids": registration["memory_ids"],
            "service_enabled": registration["service_enabled"],
            "routing": routing["preferences"] if routing is not None else None,
            "project_controls": project_controls,
            "preconditions": {
                **project_controls["source"],
                **(routing["source"] if routing is not None else {}),
                **registration["source"],
                str(policy_path): metadata["release_policy_sha256"],
                runtime["https_profile"]: runtime["https_profile_sha256"],
                runtime["content_policy"]: runtime["content_policy_sha256"],
            },
            "cache_relative": cache.relative_to(self.root).as_posix(),
            "old_profile_sha256": runtime["https_profile_sha256"],
            "archived": [],
            "old_queue_replayed": False,
        }

    def detach(self, record):
        with self.old_runtime_lease(record):
            from agent_brain_client.adapters.storage.collection import retained_routing

            routing = retained_routing(self.home)
            if self.read_project_controls(self.home) != record["project_controls"]:
                raise ValueError("Original project stop state changed before retention")
            if (routing["preferences"] if routing is not None else None) != record[
                "routing"
            ]:
                raise ValueError(
                    "Original source pause or scope changed before retention"
                )
            for name, digest in record["preconditions"].items():
                if (
                    io.digest(io.canonical(Path(name), existing=True).read_bytes())
                    != digest
                ):
                    raise ValueError(
                        "Original installation metadata changed before retention"
                    )
            current = self.read_mapping(
                self.home,
                self.native,
                self.root / record["cache_relative"],
                record["content"],
            )
            if current != record["mapping"]:
                raise ValueError("Memory mapping changed before retention")
            # Recover exact own writes after an interruption, then stage all native
            # changes together. Any outside edit is retained rather than repinned.
            host_restore(record["host"], lambda: self.save(record))
            payload = {
                Path(name): base64.b64decode(row["target"], validate=True)
                for name, row in record["host"].items()
            }
            record["host"] = host_plan(payload)
            self.save(record)
            host_apply(record["host"], lambda: self.save(record))

    def runtime_paths(self, record):
        # This exact set is shared by preflight and the authoritative mutation
        # gate. A process merely using another file in the directory is irrelevant.
        paths = []
        for name in ("access-bootstrap", "first-install", "codebuddy", "global-memory"):
            directory = io.canonical(self.root / name)
            if directory.exists():
                paths.append((directory / (
                    "hook-install.lock" if name == "global-memory" else
                    "install.lock" if name == "codebuddy" else "first-install.lock"
                ), False))
        cache = io.canonical(self.root / record["cache_relative"])
        if self.root not in cache.parents:
            raise ValueError("Device-local runtime cache required")
        if cache.exists():
            paths.extend((cache / name, True) for name in (
                ".content-runtime.lock", "content.lock", "outbox-enqueue.lock"))
        return [(io.canonical(path), cache) for path, cache in paths]

    def runtime_status(self):
        record = self.load()
        if record is not None and record["phase"] in ("archiving", "ready"):
            return {"status": "fresh-install-resume-required", "native_changed": False,
                    "phase": record["phase"], "transaction_id": record["id"],
                    "retained_archive": record["archive"], "archived_items": record["archived"],
                    "pending_items": [name for name in record["children"] if name not in record["archived"]],
                    "snapshot_only": True,
                    "next_action": "Preserve both remaining source files and the archive. Rerun the SAME reinstall command with the SAME Server/account from the current official checkout to resume this transaction. This is recovery state, not a new lock inventory; do not create another transaction, remove records or close all hosts."}
        if record is None or record["phase"] == "complete":
            registration = self.read_registration(self.home)
            meta = registration["metadata"]
            policy_path = io.canonical(Path(meta["release_policy"]))
            if self.root not in policy_path.parents:
                raise ValueError("Device-local release policy required")
            policy = io.private_json(policy_path, meta["release_policy_sha256"])
            runtime = policy["runtime"]
            if runtime["device_home"] != str(self.home):
                raise ValueError("Original device required")
            cache = io.canonical(Path(runtime["content_cache"]), existing=True)
            if self.root not in cache.parents:
                raise ValueError("Device-local runtime cache required")
            record = {"cache_relative": cache.relative_to(self.root).as_posix()}
        rows = []
        for path, _ in self.runtime_paths(record):
            state = lock_probe(path)
            row = {"path": str(path), "state": state}
            if state == "busy":
                row["resource_users"] = lock_resource_users(path)
            rows.append(row)
        busy = any(row["state"] == "busy" for row in rows)
        return {"status": "fresh-install-runtime-in-use" if busy else "fresh-install-runtime-idle",
                "native_changed": False, "device_credentials_accessed": False,
                "network_used": False, "locks": rows,
                "automatic_shutdown_supported": False,
                "snapshot_only": True,
                "next_action": RUNTIME_NEXT_ACTION if busy else
                "Run the explicit reinstall command; it rechecks exclusive leases before detaching. No host-wide exit is required."}

    @contextmanager
    def old_runtime_lease(self, record):
        with ExitStack() as stack:
            for path, cache in self.runtime_paths(record):
                try:
                    stack.enter_context(locked(path, timeout=0, cache=cache))
                except LockTimeout:
                    if lock_probe(path) != "busy":
                        raise  # Access failure or a changed snapshot is not an identified worker.
                    raise RuntimeInUse(path) from None
            yield

    def archive(self, record):
        for name, row in record["host"].items():
            try:
                _, target_image, current = host_row(Path(name), row)
            except OSError as error:
                raise RetentionFailure(error, "verify-detached-host", name, record["archive"]) from error
            if current != target_image:
                raise ValueError("Native configuration changed after detachment")
        target = io.canonical(Path(record["archive"]))
        if (
            target.parent != self.home / ".config/agent-brain-retained"
            or target.name != record["id"]
        ):
            raise ValueError("Retention scope changed")
        try:
            private_directory(target)
        except OSError as error:
            raise RetentionFailure(error, "prepare-archive-directory", self.root, target) from error
        allowed = set(record["children"]) | set(record["keep"])
        if any(p.name not in allowed for p in self.root.iterdir()):
            raise ValueError("Installation changed before retention")
        for name, identity in record["children"].items():
            if "/" in name or "\\" in name or name in (".", ".."):
                raise ValueError("Retention item name changed")
            retain_item(self.root / name, target / name, identity)
            if name not in record["archived"]:
                record["archived"].append(name)
                try:
                    self.save(record)
                except OSError as error:
                    raise RetentionFailure(error, "persist-archive-progress", self.root / name, target / name) from error

    def validate_connection(self, connection):
        record = self.load()
        if not record or record["phase"] not in ("ready", "complete"):
            return
        from agent_brain_client.application.installation.fresh import (
            validate_reconnection,
        )

        validate_reconnection(record, connection)

    def restore_mapping(self, cache, content):
        record = self.load()
        if not record or record["phase"] not in ("ready",):
            return
        if content != record["content"]:
            raise ValueError("Original memory content scope required")
        self.write_mapping(
            self.home, self.native, Path(cache), content, record["mapping"]
        )

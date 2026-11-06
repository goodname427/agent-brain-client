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
)
from agent_brain_client.adapters.platforms.retention import (
    item_identity,
    retain_item,
    retention_root,
)


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
    ):
        self.home, self.native = io.canonical(home), io.canonical(native)
        self.root = self.home / ".config/agent-brain-client"
        self.read_registration = read_registration
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
        if value and value["native"] != str(self.native):
            raise ValueError("Fresh-install host changed")
        return value

    def save(self, record):
        if len(contract.encode(record)) > 8 * 1048576:
            raise ValueError("Bounded retention journal required")
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
        from agent_brain_client.adapters.storage.collection import retained_routing

        routing = retained_routing(self.home)
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
            "routing": routing["preferences"] if routing is not None else None,
            "preconditions": {
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

    @contextmanager
    def old_runtime_lease(self, record):
        with ExitStack() as stack:
            for name in (
                "access-bootstrap",
                "first-install",
                "codebuddy",
                "global-memory",
            ):
                directory = io.canonical(self.root / name)
                if directory.exists():
                    stack.enter_context(
                        locked(
                            directory
                            / (
                                "hook-install.lock"
                                if name == "global-memory"
                                else "install.lock"
                                if name == "codebuddy"
                                else "first-install.lock"
                            ),
                            timeout=0,
                        )
                    )
            cache = self.root / record["cache_relative"]
            if cache.exists():
                for name in (
                    ".content-runtime.lock",
                    "content.lock",
                    "outbox-enqueue.lock",
                ):
                    stack.enter_context(
                        locked(io.canonical(cache / name), timeout=0, cache=True)
                    )
            yield

    def archive(self, record):
        for name, row in record["host"].items():
            _, target_image, current = host_row(Path(name), row)
            if current != target_image:
                raise ValueError("Native configuration changed after detachment")
        target = io.canonical(Path(record["archive"]))
        if (
            target.parent != self.home / ".config/agent-brain-retained"
            or target.name != record["id"]
        ):
            raise ValueError("Retention scope changed")
        private_directory(target)
        allowed = set(record["children"]) | set(record["keep"])
        if any(p.name not in allowed for p in self.root.iterdir()):
            raise ValueError("Installation changed before retention")
        for name, identity in record["children"].items():
            if "/" in name or "\\" in name or name in (".", ".."):
                raise ValueError("Retention item name changed")
            retain_item(self.root / name, target / name, identity)
            if name not in record["archived"]:
                record["archived"].append(name)
                self.save(record)

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

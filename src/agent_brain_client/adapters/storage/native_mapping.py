"""Native projection ownership, directory journals and atomic snapshot recovery."""

from agent_brain_client.domain.errors import CacheBlocked
import base64
import re
import shutil
import stat
from agent_brain_client.domain import contract
from agent_brain_client.application.control import check_request, RequestStopped
from agent_brain_client.adapters.platforms.filesystem import (
    cache_locked as locked,
    require_platform,
)
from agent_brain_client.adapters.storage.host_files import (
    host_atomic,
    host_plan,
    host_apply,
    host_restore,
)
from agent_brain_client.adapters.storage.content_limits import sha


class NativeMapping:
    def __init__(self, session):
        self.session = session

    def skill_files(self, record, identity):
        dependencies = record["dependencies"]
        if any(
            set(dependencies[key])
            - set(self.session.policy["dependencies"][key])
            for key in dependencies
        ):
            raise CacheBlocked()
        if any(
            shutil.which(command) is None
            for command in dependencies["commands"]
        ):
            raise CacheBlocked()
        # Modules need separately verified dependency evidence; no imports/install.
        if dependencies["python_modules"]:
            raise CacheBlocked()
        contract.skill_frontmatter(record, identity)
        return {self.skill_prefix + "/" + identity + "/" + name: body.encode()
                for name, body in record["files"].items()}

    def logical_name(self, path):
        return path.relative_to(self.session.home).as_posix()

    def detached_bytes(self, name, raw):
        return None

    def mapping_status(self, kind, identity, revision):
        """Inspect verified local files against a live revision without refreshing."""
        contract.validate("content_read", {"kind": kind, "id": identity})
        if kind not in self.session.policy["kinds"] or not self.session._allows(
            kind, identity
        ):
            raise CacheBlocked()
        try:
            with locked(
                self.session.cache / "content.lock", cancel_check=check_request
            ):
                journal = self.session._load("transaction.json")
                if journal is not None and journal.get("phase") not in (
                    "complete",
                    "rolled-back",
                ):
                    raise CacheBlocked()
                self.owned_snapshot()
                active = self.session._load("active.json")
                if active is None:
                    return {"status": "pull-required", "model_loaded": "unknown"}
                if (
                    not isinstance(active, dict)
                    or set(active) != {"commit", "snapshot_sha"}
                    or not isinstance(active["commit"], str)
                    or not re.fullmatch(r"[a-f0-9]{40}", active["commit"])
                ):
                    raise CacheBlocked()
                snapshot = self.session._load("snapshots/" + active["commit"] + ".json")
                if (
                    not isinstance(snapshot, dict)
                    or snapshot.get("commit") != active["commit"]
                    or sha(contract.encode(snapshot)) != active["snapshot_sha"]
                ):
                    raise CacheBlocked()
                value = next(
                    (
                        v
                        for v in snapshot["records"]
                        if (v["kind"], v["id"]) == (kind, identity)
                    ),
                    None,
                )
                if value is not None:
                    self.session._validate_target(value, kind, identity)
                current = value is not None and value["revision"] == revision
                return {
                    "status": "current" if current else "pull-required",
                    "model_loaded": "unknown",
                }
        except RequestStopped:
            raise
        except Exception:
            return {"status": "review-required", "model_loaded": "unknown"}

    def _raw(self, name):
        path = self._path(name)
        if not path.exists():
            return None
        if (
            not path.is_file()
            or path.stat().st_nlink != 1
            or path.stat().st_size > 262144
        ):
            raise CacheBlocked()
        return path.read_bytes()

    def owned_snapshot(self):
        owned = self.session._load(
            "owned.json",
            {
                "policy_sha": self.session.policy_sha,
                "binding": self.session.binding,
                "files": {},
            },
        )
        if (
            not isinstance(owned, dict)
            or set(owned) != {"policy_sha", "binding", "files"}
            or owned["policy_sha"] != self.session.policy_sha
            or owned["binding"] != self.session.binding
            or not isinstance(owned["files"], dict)
        ):
            raise CacheBlocked()
        for name, digest in owned["files"].items():
            raw = self._raw(name)
            if raw is None or sha(raw) != digest:
                raise CacheBlocked()
        self._skill_dirs(owned["files"])
        return owned

    def retention_snapshot(self):
        """Keep native memory projections without replaying any old queue."""
        owned = self.owned_snapshot()
        documents = {
            name: base64.b64encode(self._raw(name)).decode()
            for name in owned["files"]
            if name.startswith(".config/agent-brain-client/content/")
        }
        return {
            "owned": owned,
            "documents": documents,
            "directories": self._directory_ownership(),
        }

    def restore_retention(self, snapshot):
        """Adopt only byte-identical projections under the same content grant/home."""
        owned = snapshot["owned"]
        if (
            owned["policy_sha"] != self.session.policy_sha
            or owned["binding"] != self.session.binding
        ):
            raise CacheBlocked()
        from agent_brain_client.adapters.storage.host_files import atomic

        for name, encoded in snapshot["documents"].items():
            if (
                not name.startswith(".config/agent-brain-client/content/")
                or name not in owned["files"]
            ):
                raise CacheBlocked()
            raw = base64.b64decode(encoded, validate=True)
            if sha(raw) != owned["files"][name]:
                raise CacheBlocked()
            path = self._path(name)
            if path.exists():
                if self._raw(name) != raw:
                    raise CacheBlocked()
            else:
                atomic(path, raw, 0o600)
        for name, digest in owned["files"].items():
            if self._raw(name) is None or sha(self._raw(name)) != digest:
                raise CacheBlocked()
        for name, value in (
            ("owned.json", owned),
            ("skill-directories.json", snapshot["directories"]),
        ):
            old = self.session._load(name)
            if old is not None and old != value:
                raise CacheBlocked()
            self.session._save(name, value)
        self.owned_snapshot()
        self._directory_ownership()

    def _skill_dirs(self, names, owned=None):
        managed = {
            name[len(self.skill_prefix) + 1:].split("/")[0] for name in names if name.startswith(self.skill_prefix + "/")
        }
        for identity in managed:
            directory = self.session.home / self.skill_prefix / identity
            self.session._safe(directory, self.session.home)
            for entry in directory.rglob("*"):
                self.session._safe(entry, self.session.home)
                if entry.is_dir():
                    continue
                if not entry.is_file():
                    raise CacheBlocked()
                if (
                    owned is not None
                    and entry.relative_to(self.session.home).as_posix() not in owned
                ):
                    raise CacheBlocked()

    def _directory_ownership(self):
        state = self.session._load("skill-directories.json", {"directories": {}})
        if (
            not isinstance(state, dict)
            or set(state) != {"directories"}
            or not isinstance(state["directories"], dict)
        ):
            raise CacheBlocked()
        for name, identity in state["directories"].items():
            if not isinstance(name, str) or not re.fullmatch(
                re.escape(self.skill_prefix) + r"/[a-z0-9-]+(?:/.+)?", name
            ):
                raise CacheBlocked()
            self._path(name + "/x")
            if (
                not isinstance(identity, list)
                or len(identity) != 2
                or any(type(v) is not int or v < 0 for v in identity)
            ):
                raise CacheBlocked()
        return state

    def _prepare_skill_directories(self, desired):
        state = self._directory_ownership()
        for name in sorted(desired):
            if not name.startswith(self.skill_prefix + "/"):
                continue
            path = self._path(name).parent
            pending = []
            while path != self.session.home / self.skill_prefix:
                pending.append(path)
                path = path.parent
            for directory in reversed(pending):
                self.session._safe(directory, self.session.home)
                directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    directory.mkdir(mode=0o700)
                except FileExistsError:
                    continue  # Never adopt pre-existing directories.
                info = directory.stat()
                state["directories"][
                    directory.relative_to(self.session.home).as_posix()
                ] = [info.st_dev, info.st_ino]
                self.session._save("skill-directories.json", state)

    def _prune_skill_directories(self, files):
        state = self._directory_ownership()
        previous = dict(state["directories"])
        removed = False
        for name, identity in sorted(
            list(state["directories"].items()),
            key=lambda row: row[0].count("/"),
            reverse=True,
        ):
            if any(file.startswith(name + "/") for file in files):
                continue
            directory = self.session.home / name
            try:
                self.session._safe(directory, self.session.home)
                info = directory.lstat()
                if (
                    stat.S_ISDIR(info.st_mode)
                    and [info.st_dev, info.st_ino] == identity
                ):
                    directory.rmdir()  # Atomic empty-only removal, never recursive.
                    removed = True
                del state["directories"][name]
            except FileNotFoundError:
                state["directories"].pop(name, None)
            except OSError:
                pass  # Nonempty/busy directories and custom contents stay.
        if state["directories"] != previous:
            self.session._save("skill-directories.json", state)
        return removed

    def _restore(self, journal):
        require_platform("cache-recovery", self.session.platform)
        # Preflight all files before any restoration; preserve outside edits.
        if (
            set(journal)
            not in (
                {"phase", "before", "after", "owned", "active"},
                {"phase", "before", "after", "owned", "active", "host"},
            )
            or set(journal["before"]) != set(journal["after"])
            or journal["owned"].get("policy_sha") != self.session.policy_sha
            or journal["owned"].get("binding") != self.session.binding
        ):
            raise CacheBlocked()
        if "host" in journal:
            rows = journal["host"]
            if set(rows) != {str(self._path(name)) for name in journal["before"]}:
                raise CacheBlocked()
            for name, old in journal["before"].items():
                row = rows[str(self._path(name))]
                if (
                    row["before"] != old["data"]
                    or row["before_sha"] != old["sha"]
                    or row["target_sha"] != journal["after"][name]
                ):
                    raise CacheBlocked()
            host_restore(rows, lambda: self.session._save("transaction.json", journal))
            self.session._save("owned.json", journal["owned"])
            self.session._save("active.json", journal["active"])
            self.session._save("transaction.json", dict(journal, phase="rolled-back"))
            self._prune_skill_directories(journal["owned"]["files"])
            return
        for name, old in journal["before"].items():
            if (
                set(old) != {"data", "sha", "mode"}
                or type(old["mode"]) is not int
                or old["mode"] & ~0o777
            ):
                raise CacheBlocked()
            backup = (
                base64.b64decode(old["data"], validate=True)
                if old["data"] is not None
                else None
            )
            if (sha(backup) if backup is not None else None) != old["sha"]:
                raise CacheBlocked()
            raw = self._raw(name)
            digest = sha(raw) if raw is not None else None
            if digest not in (old["sha"], journal["after"][name]):
                raise CacheBlocked()
        for name, old in journal["before"].items():
            path = self._path(name)
            if old["data"] is None:
                path.unlink(missing_ok=True)
            else:
                host_atomic(
                    path,
                    base64.b64decode(old["data"], validate=True),
                    old["mode"],
                    self._raw(name),
                )
        self.session._save("owned.json", journal["owned"])
        self.session._save("active.json", journal["active"])
        self.session._save("transaction.json", dict(journal, phase="rolled-back"))
        self._prune_skill_directories(journal["owned"]["files"])

    def transaction_settled(self):
        """Read-only gate for offline reads/removal; never attempts recovery."""
        journal = self.session._load("transaction.json")
        return not journal or journal.get("phase") in ("complete", "rolled-back")

    def recovery_uncertain(self):
        try:
            journal = self.session._load("transaction.json")
            return bool(journal and journal["phase"] == "applying")
        except Exception:
            return True

    def recover(self):
        journal = self.session._load("transaction.json")
        if journal and journal["phase"] not in ("applying", "complete", "rolled-back"):
            raise CacheBlocked()
        if journal and journal["phase"] == "applying":
            self._restore(journal)

    def apply_snapshot(self, snapshot, owned, progress):
        progress["stage"] = "native-mapping"
        desired = self._mapping(snapshot, owned)
        self._skill_dirs(desired)
        if any(
            self.session.cache == self._path(name)
            or self.session.cache in self._path(name).parents
            for name in desired
        ):
            raise CacheBlocked()
        active = self.session._load("active.json")
        mapping_matches = set(owned["files"]) == set(desired) and all(
            owned["files"].get(name) == sha(value) for name, value in desired.items()
        )
        if (
            active
            == {
                "commit": snapshot["commit"],
                "snapshot_sha": sha(contract.encode(snapshot)),
            }
            and self.session._load("snapshots/" + snapshot["commit"] + ".json")
            == snapshot
            and mapping_matches
        ):
            self.session._pin_offline_snapshot(snapshot)
            return {
                "status": "content-current",
                "native_changed": self._prune_skill_directories(desired),
                "model_loaded": "unknown",
            }
        progress["stage"] = "native-preflight"
        before = {}
        after = {}
        for name in set(owned["files"]) | set(desired):
            raw = self._raw(name)
            if (
                name not in self.instruction_keys
                and name not in owned["files"]
                and raw is not None
            ):
                raise CacheBlocked()
            # A new snapshot commit need not change every host file.
            # Opening unchanged Skills/AGENTS for write can fail on
            # Windows while Codex or an editor holds them read-only.
            if raw == desired.get(name):
                continue
            path = self._path(name)
            before[name] = {
                "data": base64.b64encode(raw).decode() if raw is not None else None,
                "sha": sha(raw) if raw is not None else None,
                "mode": path.stat().st_mode & 0o777 if raw is not None else 0o600,
            }
            after[name] = sha(desired[name]) if name in desired else None
        journal = {
            "phase": "applying",
            "before": before,
            "after": after,
            "owned": owned,
            "active": active,
        }
        journal["host"] = host_plan(
            {self._path(name): desired.get(name) for name in before}
        )
        if any(
            journal["host"][str(self._path(name))]["before_sha"] != old["sha"]
            for name, old in before.items()
        ):
            raise CacheBlocked()
        self.session._save("transaction.json", journal)
        try:
            progress["stage"] = "native-apply"
            self._prepare_skill_directories(desired)
            host_apply(
                journal["host"], lambda: self.session._save("transaction.json", journal)
            )
            progress["stage"] = "snapshot-save"
            self.session._save("snapshots/" + snapshot["commit"] + ".json", snapshot)
            self.session._save(
                "owned.json",
                {
                    "policy_sha": self.session.policy_sha,
                    "binding": self.session.binding,
                    "files": {name: sha(raw) for name, raw in desired.items()},
                },
            )
            self.session._save(
                "active.json",
                {
                    "commit": snapshot["commit"],
                    "snapshot_sha": sha(contract.encode(snapshot)),
                },
            )
            progress["stage"] = "verification"
            self.session.health()
            self.owned_snapshot()
            self.session._pin_offline_snapshot(snapshot)
            self.session._save("transaction.json", dict(journal, phase="complete"))
        except Exception:
            self._restore(journal)
            raise
        self._prune_skill_directories(desired)
        return {
            "status": "content-refreshed",
            "native_changed": bool(before),
            "model_loaded": "unknown",
            "commit": snapshot["commit"],
        }

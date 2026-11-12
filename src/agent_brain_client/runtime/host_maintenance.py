"""Maintain only registered native hosts; shared code/policy is managed separately."""

from pathlib import Path
from types import SimpleNamespace
from agent_brain_client.adapters.storage import metadata
from agent_brain_client.adapters.storage.collection import settings_path
from agent_brain_client.adapters.storage.registration import registration
from agent_brain_client.adapters.packages.frozen import freeze_code

ROOT = Path(__file__).resolve().parents[3]


def host_errors(operation):
    from functools import wraps
    from agent_brain_client.adapters.storage.host_files import HostConfigChanged
    from agent_brain_client.application.installation.update_ports import HostEditInProgress, ManagedConfigurationConflict
    @wraps(operation)
    def run(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except HostConfigChanged:
            raise HostEditInProgress() from None
        except ValueError as error:
            if hasattr(error, "field"):
                raise ManagedConfigurationConflict(error.field) from None
            raise
    return run


def registered_hosts(home):
    base = Path(home) / ".config/agent-brain-client"
    hosts = []
    codex = base / "codex/registration.json"
    if ((codex.exists() and metadata.private_json(codex).get("removed") is not True)
            or not codex.exists() and (base / "global-memory/hook-install.json").exists()):
        hosts.append("codex")
    buddy = base / "codebuddy/registration.json"
    if buddy.exists() and metadata.private_json(buddy).get("removed") is not True:
        hosts.append("codebuddy")
    return hosts


def install_sources(args):
    from agent_brain_client.adapters.installation.installation import registered_context
    from agent_brain_client.adapters.storage.collection_setup import prepare
    home = Path(args.device_home)
    meta, _ = registration(home)
    _, command, _, _ = registered_context(home, meta)
    argv = command["args"]
    harness = getattr(args, "harness", None) or argv[argv.index("--harness") + 1]
    if harness == "codex":
        from agent_brain_client.adapters.storage.reinstallation import read_journal
        fresh = read_journal(home)
        retained = fresh["mapping"].get("registered_surfaces", {}).get("codex") if fresh and fresh["phase"] == "complete" else None
        if retained is not None and not retained["hooks_enabled"]:
            value = prepare(args)
            freeze_code(settings_path(home).parent, value["code_sha256"])
            return {"status": "retained-codex-hooks-paused", "native_changed": False, "harness": harness}
        from agent_brain_client.adapters.harnesses.codex.collection_installation import install
        return install(args)
    if harness != "codebuddy":
        raise ValueError("Explicit installed Harness required")
    value = prepare(args)
    freeze_code(settings_path(home).parent, value["code_sha256"])
    return {"status": "shared-source-runtime-prepared", "native_changed": False,
            "harness": harness, "settings": str(settings_path(home)),
            "host_binding": "codebuddy-entry-installs-selected-surface"}


def recover_pending(home, native):
    if "codex" in registered_hosts(home):
        from agent_brain_client.adapters.harnesses.codex import binding
        binding.recover(home)
        from agent_brain_client.adapters.harnesses.codex.collection_installation import recover_pending as recover
        _, policy = registration(home)
        recover(home, binding.native_home(home, policy["runtime"]))
    if "codebuddy" in registered_hosts(home):
        from agent_brain_client.adapters.harnesses.codebuddy.configuration import recover
        recover(home, None)


class HostMaintenance:
    def __init__(self, home, *, hosts=None):
        self.home = Path(home)
        self._hosts = hosts

    def hosts(self):
        return list(self._hosts) if self._hosts is not None else registered_hosts(self.home)

    @host_errors
    def recover(self, *, check_only):
        from agent_brain_client.adapters.storage.collection_setup import recover_code
        pending = False
        code_journal = settings_path(self.home).parent / "code-refresh.json"
        if code_journal.exists() and metadata.private_json(code_journal).get("phase") == "applying":
            pending = True
            if not check_only:
                recover_code(self.home)
        for host in self.hosts():
            if host == "codex":
                from agent_brain_client.adapters.harnesses.codex import binding
                transaction = binding.directory(self.home) / "transaction.json"
                active = transaction.exists() and metadata.private_json(transaction).get("phase") == "applying"
                pending = active or pending
                if active and not check_only:
                    binding.recover(self.home)
                from agent_brain_client.adapters.harnesses.codex.collection_installation import recover_update
                pending = recover_update(self.home, check_only=check_only) or pending
            else:
                from agent_brain_client.adapters.harnesses.codebuddy.configuration import state_directory, recover
                journal = state_directory(self.home) / "transaction.json"
                active = journal.exists() and metadata.private_json(journal).get("phase") == "applying"
                pending = active or pending
                if active and not check_only:
                    recover(self.home, None)
        return pending

    @host_errors
    def refresh(self, channel, *, check_only=False):
        active = channel.verified_active()
        if active is None:
            raise ValueError("Verified native-host package required")
        from agent_brain_client.adapters.storage.private_files import safe
        from agent_brain_client.runtime.updates import refresh_bindings
        package = safe(active["package"])
        hosts = {}
        if channel.verified_active() != active:
            raise ValueError("Active package changed before native host maintenance")
        for host in self.hosts():
            if host == "codex":
                from agent_brain_client.adapters.harnesses.codex import collection_installation as hooks
                if all((package / name).read_bytes() == (hooks.ROOT / name).read_bytes() for name in hooks.FILES):
                    try:
                        hosts[host] = hooks.refresh(self.home, check_only=check_only)
                    except hooks.ManagedHookConflict as error:
                        from agent_brain_client.application.installation.update_ports import ManagedConfigurationConflict
                        raise ManagedConfigurationConflict(error.field) from None
                else:
                    hosts[host] = refresh_bindings(self.home, package, harness=host, check_only=check_only)
            else:
                hosts[host] = refresh_bindings(self.home, package, harness=host, check_only=check_only)
            if channel.verified_active() != active:
                raise ValueError("Active package changed during host maintenance")
        changed = any(row["native_changed"] for row in hosts.values())
        available = any(row["update_available"] for row in hosts.values())
        return {"status": "global-hook-updated" if changed else "global-hook-update-available" if available else "global-hook-current",
                "native_changed": changed, "update_available": available, "hosts": hosts,
                "source_package_version": active["version"], "source_package_commit": active["commit"]}

    def registration_preimage(self):
        if "codex" not in self.hosts():
            return None
        from agent_brain_client.adapters.harnesses.codex.collection_installation import recovery_registration
        return recovery_registration(self.home)

    def install_sources(self):
        return install_sources(SimpleNamespace(device_home=str(self.home), profile_id=None,
                                               operator=None, operator_sha256=None))


def global_entry(args, *, harness, remove=False):
    if harness == "codex":
        from agent_brain_client.adapters.harnesses.codex import collection_installation
        return collection_installation.uninstall(args) if remove else collection_installation.install(args)
    if harness != "codebuddy":
        raise ValueError("Explicit global entry Harness required")
    from agent_brain_client.adapters.harnesses.codebuddy import configuration
    file = configuration.state_directory(args.device_home) / "registration.json"
    if not file.exists():
        raise ValueError("Install the explicitly selected CodeBuddy surface before changing its Hooks")
    receipt = metadata.private_json(file)
    if receipt.get("removed"):
        raise ValueError("CodeBuddy is uninstalled; preserve its retained configuration")
    if not remove:
        from agent_brain_client.adapters.storage.collection_setup import prepare
        value = prepare(args)
        freeze_code(settings_path(args.device_home).parent, value["code_sha256"])
    result = configuration.install(Path(args.device_home), ROOT, None, surface=receipt["surface"], enable_hooks=not remove)
    return dict(result, status="global-hook-removed" if remove else "global-hook-installed", harness=harness)

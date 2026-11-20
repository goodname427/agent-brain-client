"""Required Python runtime and scoped manifest extensions."""

from pathlib import Path, PurePosixPath
import re


MANIFEST_PYTHON_LAYOUT = 1

PYTHON_FILES = (
    "src/agent_brain_client/__init__.py",
    "src/agent_brain_client/adapters/__init__.py",
    "src/agent_brain_client/adapters/harnesses/__init__.py",
    "src/agent_brain_client/adapters/harnesses/codebuddy/__init__.py",
    "src/agent_brain_client/adapters/harnesses/codebuddy/configuration.py",
    "src/agent_brain_client/adapters/harnesses/codebuddy/content_ports.py",
    "src/agent_brain_client/adapters/harnesses/codebuddy/events.py",
    "src/agent_brain_client/adapters/harnesses/codebuddy/mapping.py",
    "src/agent_brain_client/adapters/harnesses/codebuddy/project_configuration.py",
    "src/agent_brain_client/adapters/harnesses/codebuddy/sources.py",
    "src/agent_brain_client/adapters/harnesses/codebuddy/transcript.py",
    "src/agent_brain_client/adapters/harnesses/codex/__init__.py",
    "src/agent_brain_client/adapters/harnesses/codex/binding.py",
    "src/agent_brain_client/adapters/harnesses/codex/collection.py",
    "src/agent_brain_client/adapters/harnesses/codex/collection_installation.py",
    "src/agent_brain_client/adapters/harnesses/codex/configuration.py",
    "src/agent_brain_client/adapters/harnesses/codex/content_limits.py",
    "src/agent_brain_client/adapters/harnesses/codex/content_ports.py",
    "src/agent_brain_client/adapters/harnesses/codex/hooks.py",
    "src/agent_brain_client/adapters/harnesses/codex/installation_detach.py",
    "src/agent_brain_client/adapters/harnesses/codex/mapping.py",
    "src/agent_brain_client/adapters/harnesses/codex/memory_inventory.py",
    "src/agent_brain_client/adapters/harnesses/codex/project_configuration.py",
    "src/agent_brain_client/adapters/harnesses/codex/transcript.py",
    "src/agent_brain_client/adapters/harnesses/hook_output.py",
    "src/agent_brain_client/adapters/installation/__init__.py",
    "src/agent_brain_client/adapters/installation/fresh_installation.py",
    "src/agent_brain_client/adapters/installation/installation.py",
    "src/agent_brain_client/adapters/installation/launcher_update.py",
    "src/agent_brain_client/adapters/installation/lifecycle.py",
    "src/agent_brain_client/adapters/installation/projection_removal.py",
    "src/agent_brain_client/adapters/installation/registry_format.py",
    "src/agent_brain_client/adapters/installation/update_interpreter.py",
    "src/agent_brain_client/adapters/installation/updates.py",
    "src/agent_brain_client/adapters/packages/__init__.py",
    "src/agent_brain_client/adapters/packages/build.py",
    "src/agent_brain_client/adapters/packages/client.py",
    "src/agent_brain_client/adapters/packages/frozen.py",
    "src/agent_brain_client/adapters/packages/layout.py",
    "src/agent_brain_client/adapters/packages/publication.py",
    "src/agent_brain_client/adapters/packages/releases.py",
    "src/agent_brain_client/adapters/platforms/__init__.py",
    "src/agent_brain_client/adapters/platforms/credentials.py",
    "src/agent_brain_client/adapters/platforms/filesystem.py",
    "src/agent_brain_client/adapters/platforms/host_files.py",
    "src/agent_brain_client/adapters/platforms/macos/__init__.py",
    "src/agent_brain_client/adapters/platforms/macos/component.py",
    "src/agent_brain_client/adapters/platforms/macos/helper_build.py",
    "src/agent_brain_client/adapters/platforms/macos/keychain.py",
    "src/agent_brain_client/adapters/platforms/posix/__init__.py",
    "src/agent_brain_client/adapters/platforms/posix/host_files.py",
    "src/agent_brain_client/adapters/platforms/posix/tree.py",
    "src/agent_brain_client/adapters/platforms/processes.py",
    "src/agent_brain_client/adapters/platforms/retention.py",
    "src/agent_brain_client/adapters/platforms/source_files.py",
    "src/agent_brain_client/adapters/platforms/tree.py",
    "src/agent_brain_client/adapters/platforms/windows/__init__.py",
    "src/agent_brain_client/adapters/platforms/windows/access.py",
    "src/agent_brain_client/adapters/platforms/windows/filesystem.py",
    "src/agent_brain_client/adapters/platforms/windows/host_files.py",
    "src/agent_brain_client/adapters/platforms/windows/native.py",
    "src/agent_brain_client/adapters/platforms/windows/tree.py",
    "src/agent_brain_client/adapters/server/__init__.py",
    "src/agent_brain_client/adapters/server/collection.py",
    "src/agent_brain_client/adapters/server/http.py",
    "src/agent_brain_client/adapters/server/https.py",
    "src/agent_brain_client/adapters/server/ssh.py",
    "src/agent_brain_client/adapters/storage/__init__.py",
    "src/agent_brain_client/adapters/storage/bounded_json.py",
    "src/agent_brain_client/adapters/storage/collection.py",
    "src/agent_brain_client/adapters/storage/collection_control.py",
    "src/agent_brain_client/adapters/storage/collection_setup.py",
    "src/agent_brain_client/adapters/storage/content.py",
    "src/agent_brain_client/adapters/storage/content_limits.py",
    "src/agent_brain_client/adapters/storage/content_session.py",
    "src/agent_brain_client/adapters/storage/event_policy.py",
    "src/agent_brain_client/adapters/storage/host_files.py",
    "src/agent_brain_client/adapters/storage/install_state.py",
    "src/agent_brain_client/adapters/storage/metadata.py",
    "src/agent_brain_client/adapters/storage/native_mapping.py",
    "src/agent_brain_client/adapters/storage/native_sources.py",
    "src/agent_brain_client/adapters/storage/observed_migration.py",
    "src/agent_brain_client/adapters/storage/origins.py",
    "src/agent_brain_client/adapters/storage/pinned.py",
    "src/agent_brain_client/adapters/storage/private_files.py",
    "src/agent_brain_client/adapters/storage/project_settings.py",
    "src/agent_brain_client/adapters/storage/recall_receipts.py",
    "src/agent_brain_client/adapters/storage/registration.py",
    "src/agent_brain_client/adapters/storage/reinstallation.py",
    "src/agent_brain_client/adapters/storage/source_imports.py",
    "src/agent_brain_client/adapters/storage/toml_document.py",
    "src/agent_brain_client/application/__init__.py",
    "src/agent_brain_client/application/collection/__init__.py",
    "src/agent_brain_client/application/collection/control.py",
    "src/agent_brain_client/application/collection/routing.py",
    "src/agent_brain_client/application/content/__init__.py",
    "src/agent_brain_client/application/content/management.py",
    "src/agent_brain_client/application/control.py",
    "src/agent_brain_client/application/installation/__init__.py",
    "src/agent_brain_client/application/installation/enrollment.py",
    "src/agent_brain_client/application/installation/follow_up.py",
    "src/agent_brain_client/application/installation/fresh.py",
    "src/agent_brain_client/application/installation/pairing.py",
    "src/agent_brain_client/application/installation/update.py",
    "src/agent_brain_client/application/installation/update_ports.py",
    "src/agent_brain_client/application/memory/__init__.py",
    "src/agent_brain_client/application/memory/capture.py",
    "src/agent_brain_client/application/memory/context.py",
    "src/agent_brain_client/application/memory/guidance.py",
    "src/agent_brain_client/application/memory/lifecycle.py",
    "src/agent_brain_client/application/memory/observations.py",
    "src/agent_brain_client/application/memory/prompt_recall.py",
    "src/agent_brain_client/application/memory/recall.py",
    "src/agent_brain_client/application/memory/rule_fallback.py",
    "src/agent_brain_client/application/queue/__init__.py",
    "src/agent_brain_client/application/queue/delivery.py",
    "src/agent_brain_client/application/queue/outbox.py",
    "src/agent_brain_client/application/queue/ports.py",
    "src/agent_brain_client/application/synchronization/__init__.py",
    "src/agent_brain_client/application/synchronization/ports.py",
    "src/agent_brain_client/application/synchronization/pull.py",
    "src/agent_brain_client/application/tools/__init__.py",
    "src/agent_brain_client/application/tools/catalog.py",
    "src/agent_brain_client/domain/__init__.py",
    "src/agent_brain_client/domain/authorization.py",
    "src/agent_brain_client/domain/content.py",
    "src/agent_brain_client/domain/content_policy.py",
    "src/agent_brain_client/domain/contract.py",
    "src/agent_brain_client/domain/enrollment.py",
    "src/agent_brain_client/domain/errors.py",
    "src/agent_brain_client/domain/events.py",
    "src/agent_brain_client/domain/hook_coverage.py",
    "src/agent_brain_client/domain/operations.py",
    "src/agent_brain_client/domain/privacy.py",
    "src/agent_brain_client/domain/recall.py",
    "src/agent_brain_client/domain/receipts.py",
    "src/agent_brain_client/domain/scopes.py",
    "src/agent_brain_client/domain/service.py",
    "src/agent_brain_client/domain/sources.py",
    "src/agent_brain_client/domain/tool_contract.py",
    "src/agent_brain_client/domain/turns.py",
    "src/agent_brain_client/domain/validation.py",
    "src/agent_brain_client/interfaces/__init__.py",
    "src/agent_brain_client/interfaces/cli/__init__.py",
    "src/agent_brain_client/interfaces/cli/access.py",
    "src/agent_brain_client/interfaces/cli/codebuddy.py",
    "src/agent_brain_client/interfaces/cli/collection.py",
    "src/agent_brain_client/interfaces/cli/delivery.py",
    "src/agent_brain_client/interfaces/cli/device.py",
    "src/agent_brain_client/interfaces/cli/install.py",
    "src/agent_brain_client/interfaces/cli/install_plan.py",
    "src/agent_brain_client/interfaces/cli/project.py",
    "src/agent_brain_client/interfaces/cli/request.py",
    "src/agent_brain_client/interfaces/cli/sources.py",
    "src/agent_brain_client/interfaces/management/__init__.py",
    "src/agent_brain_client/interfaces/management/server.py",
    "src/agent_brain_client/interfaces/mcp/__init__.py",
    "src/agent_brain_client/interfaces/mcp/content.py",
    "src/agent_brain_client/interfaces/mcp/framing.py",
    "src/agent_brain_client/interfaces/mcp/launcher.py",
    "src/agent_brain_client/interfaces/mcp/server.py",
    "src/agent_brain_client/interfaces/mcp/topics.py",
    "src/agent_brain_client/runtime/__init__.py",
    "src/agent_brain_client/runtime/capture_queues.py",
    "src/agent_brain_client/runtime/client_lifecycle.py",
    "src/agent_brain_client/runtime/collection.py",
    "src/agent_brain_client/runtime/collection_management.py",
    "src/agent_brain_client/runtime/delivery.py",
    "src/agent_brain_client/runtime/dependencies.py",
    "src/agent_brain_client/runtime/enrollment.py",
    "src/agent_brain_client/runtime/fresh_installation.py",
    "src/agent_brain_client/runtime/harness_content.py",
    "src/agent_brain_client/runtime/host_binding.py",
    "src/agent_brain_client/runtime/host_maintenance.py",
    "src/agent_brain_client/runtime/installation.py",
    "src/agent_brain_client/runtime/installation_completion.py",
    "src/agent_brain_client/runtime/launcher.py",
    "src/agent_brain_client/runtime/project_memory.py",
    "src/agent_brain_client/runtime/source_enrollment.py",
    "src/agent_brain_client/runtime/updates.py",
)


_EXTENSION_MODULE = re.compile(
    r"src/agent_brain_client/(?:application|domain|runtime|"
    r"adapters/(?:harnesses|server|storage))/"
    r"(?:[a-z_][a-z0-9_]*/)*[a-z_][a-z0-9_]*\.py"
)
_RESERVED_WINDOWS_NAME = re.compile(r"(?:aux|con|nul|prn|com[1-9]|lpt[1-9])")
_RUNTIME_ROOT = PurePosixPath("src/agent_brain_client")


def ordinary_python_module(name, members):
    if not isinstance(name, str) or not _EXTENSION_MODULE.fullmatch(name):
        return False
    path = PurePosixPath(name)
    if any(
        _RESERVED_WINDOWS_NAME.fullmatch(part.removesuffix(".py"))
        for part in path.parts
    ):
        return False
    if (path.with_suffix("") / "__init__.py").as_posix() in members:
        return False
    for parent in path.parents:
        if parent == _RUNTIME_ROOT:
            break
        if (
            (parent / "__init__.py").as_posix() not in members
            or parent.with_suffix(".py").as_posix() in members
        ):
            return False
    return True


def runtime_python_members(names, *, required=PYTHON_FILES):
    """Keep the existing installed layout and validate all optional members."""
    members = set(names)
    required = set(required)
    if not required <= members or any(
        not ordinary_python_module(name, members) for name in members - required
    ):
        raise ValueError("Python runtime members outside reviewed layout")
    return tuple(sorted(members))


def source_python_members(source):
    """Enumerate only in an installer source tree or its exact build checkout."""
    source = Path(source).resolve()
    paths = (source / "src/agent_brain_client").rglob("*.py")
    names = []
    for path in paths:
        if path.resolve() != path or path.is_symlink() or not path.is_file():
            raise ValueError("Linked or nonregular Python source")
        names.append(path.relative_to(source).as_posix())
    return runtime_python_members(names)

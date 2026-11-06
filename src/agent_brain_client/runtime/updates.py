"""Compose Client maintenance ports for the public installation interface."""

import subprocess
from agent_brain_client.application.installation.update import ClientUpdate
from agent_brain_client.adapters.harnesses.codex.updates import CodexUpdateInstallation
from agent_brain_client.adapters.harnesses.codex import update_interpreter


def manage_update(home, action, *, channel_factory=None):
    from agent_brain_client.runtime.installation_completion import source_setup

    return ClientUpdate(
        CodexUpdateInstallation(
            home,
            channel_factory=channel_factory,
            setup_sources=lambda: source_setup(home),
        )
    ).run(action)


def reexec_update_runtime(home, action, *, runner=subprocess.run):
    return update_interpreter.reexec_update_runtime(home, action, runner=runner)

"""Pinned macOS Keychain helper over private pipes; no plaintext fallback."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from agent_brain_client.adapters.platforms.credentials import canonical, unique, owned
from agent_brain_client.adapters.platforms.filesystem import require_platform


class NativeKeychain:
    def __init__(self, helper, digest, service, *, runner=subprocess.run):
        self.helper, self.digest, self.service, self.runner = (
            Path(helper),
            digest,
            service,
            runner,
        )
        if not re.fullmatch(r"agent-brain\.https\.[a-f0-9]{64}", service):
            raise ValueError("Dedicated credential item required")

    def call(self, op, **fields):
        require_platform("credential-store")
        helper = owned(self.helper)
        if (
            not re.fullmatch(r"[a-f0-9]{64}", self.digest)
            or helper.stat().st_size > 16777216
            or hashlib.sha256(helper.read_bytes()).hexdigest() != self.digest
        ):
            raise ValueError("Credential helper review required")
        if not os.access(helper, os.X_OK):
            raise ValueError("Credential helper must be executable")
        for parent in helper.parents:
            if parent.stat().st_mode & 0o022:
                raise ValueError("Credential helper ancestor writable")
        # No ambient injection, proxy, HOME override, debug flags or secret argv.
        env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "en_US.UTF-8"}
        from agent_brain_client.adapters.platforms.processes import controlled_run

        result = controlled_run(
            self.runner,
            [str(helper)],
            input=canonical(dict(op=op, service=self.service, **fields)),
            capture_output=True,
            timeout=180,
            env=env,
        )
        if result.returncode or len(result.stdout) > 32768:
            raise ValueError("Credential store unavailable")
        value = json.loads(result.stdout, object_pairs_hook=unique)
        if not isinstance(value, dict) or "error" in value:
            raise ValueError("Credential store unavailable")
        return value

    def load(self):
        value = self.call("load")
        if (
            set(value) != {"record"}
            or value["record"] is not None
            and not isinstance(value["record"], dict)
        ):
            raise ValueError("Unknown credential record")
        return value["record"]

    def save(self, record, expected):
        if self.call("save", record=record, expected=expected) != {"saved": True}:
            raise ValueError("Credential save unconfirmed")

    def clear(self, expected):
        if self.call("clear", expected=expected) != {"saved": True}:
            raise ValueError("Credential clear unconfirmed")

    def secure_input(self):
        value = self.call("input")
        if value == {"cancelled": True}:
            return None
        if (
            set(value) != {"code"}
            or not isinstance(value["code"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value["code"])
        ):
            raise ValueError("Invalid secure input")
        return value["code"]

    def confirm_reset(self, reason, device_id):
        if reason == "pairing_prepare_rejected" and device_id == "preparing":
            # Non-secret, constant OS dialog: keep the existing compiled helper
            # and its source pin compatible with installed update policies.
            require_platform("credential-store")
            script = 'display dialog "The server explicitly rejected this invitation. Clear only its saved preparing record? Pending or active device credentials and offline writes are preserved." with title "Agent Brain pairing" buttons {"Cancel", "Clear rejected invitation"} default button "Cancel" cancel button "Cancel"'
            result = self.runner(
                ["/usr/bin/osascript", "-e", script],
                capture_output=True,
                timeout=180,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(Path.home()),
                    "LANG": "en_US.UTF-8",
                },
            )
            return (
                result.returncode == 0
                and result.stdout.strip()
                == b"button returned:Clear rejected invitation"
            )
        value = self.call("confirm-reset", reason=reason, device_id=device_id)
        if set(value) != {"confirmed"} or type(value["confirmed"]) is not bool:
            raise ValueError("Reset UI unconfirmed")
        return value["confirmed"]

"""One shared address/key installer; OS backends only handle local credentials.

The user-selected TLS origin is the bootstrap authority. Hashes saved afterwards
pin local state; they are not independent proof of a downloaded package.
"""

import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.error import HTTPError
from urllib.request import Request
import warnings

import mac_first_install as wizard
from https_content import PairingHTTPS, endpoint_check, valid_secret
from system_credentials import CredentialStore, NativeKeychain, canonical, owned, unique
from brain_lock import private_directory, native_platform


class AccessDenied(ValueError):
    pass


class AccessHTTPS(PairingHTTPS):
    def __init__(self, endpoint):
        super().__init__(endpoint)
        self.key: str | None = None

    def access(self, phase, payload, key):
        if phase not in ("bootstrap", "enroll") or not valid_secret(key):
            raise ValueError("Access request required")
        request = Request(
            self.endpoint + "/v1/access/" + phase,
            data=canonical(payload),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(16385)
                if response.status != 200 or len(raw) > 16384:
                    raise ValueError("Access reply unconfirmed")
            value = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(value, dict) or "error" in value:
                raise ValueError("Access reply unconfirmed")
            return value
        except HTTPError as error:
            # Only this exact application rejection can retire an origin key.
            try:
                raw = error.read(16385)
                value = (
                    json.loads(raw, object_pairs_hook=unique)
                    if len(raw) <= 16384
                    else {}
                )
                rejected = (
                    error.code == 403
                    and value.get("error", {}).get("code") == "access_denied"
                )
            except Exception:
                rejected = False
            if rejected:
                raise AccessDenied("Access key rejected") from None
            raise ValueError("Access connection unconfirmed") from None
        except AccessDenied:
            raise
        except Exception:
            raise ValueError("Access connection unconfirmed") from None

    def bootstrap(self, key, target):
        value = self.access(
            "bootstrap", {"device": {"adapter": "codex", "platform": target}}, key
        )
        if (
            set(value) != {"schema", "connection"}
            or type(value["schema"]) is not int
            or value["schema"] != 1
        ):
            raise ValueError("Access protocol required")
        connection = wizard.connection_value(value["connection"])
        if (
            connection["endpoint"] != self.endpoint
            or connection["grant"]["platform"] != target
        ):
            raise ValueError("Selected Server identity changed")
        self.key = key
        return connection

    def prepare(self, code, nonce, profile):
        # The legacy coordinator persists an opaque recovery marker, not the key.
        return self.access(
            "enroll",
            {
                "nonce": nonce,
                "device": {k: profile[k] for k in ("adapter", "platform")},
            },
            self.key,
        )


def access_store(home, endpoint, materials, *, helper_builder=None):
    state = Path(home) / ".config/agent-brain-client/access-bootstrap"
    private_directory(state)
    owned(state, directory=True)
    service = (
        "agent-brain.https."
        + hashlib.sha256(
            canonical({"protocol": "access-v1", "endpoint": endpoint})
        ).hexdigest()
    )
    if os.name == "nt":
        import brain_windows

        backend = brain_windows.WindowsCredentials(service)
    else:
        from build_credential_helper import build

        helper = state / "credential-helper"
        pin = state / "helper.json"
        if helper.exists() or pin.exists():
            metadata = wizard.private_json(owned(pin))
            if metadata.get("source_sha256") != wizard.sha(
                (wizard.SOURCE / "native/AccessCredentialStore.swift").read_bytes()
            ) or metadata.get("helper_sha256") != wizard.sha(
                owned(helper).read_bytes()
            ):
                raise ValueError("Access helper changed; preserve original credentials")
        else:
            built = (helper_builder or build)(helper, access=True)
            metadata = {k: built[k] for k in ("source_sha256", "helper_sha256")}
            wizard.same_private(pin, metadata)
        backend = NativeKeychain(helper, metadata["helper_sha256"], service)
    return CredentialStore(backend, state / "credentials")


def hidden_input(store):
    if os.name != "nt":
        return store.backend.secure_input()
    import getpass

    if not sys.stdin.isatty():
        raise ValueError(
            "Interactive local Windows console required for hidden key input"
        )
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        key = getpass.getpass("Agent Brain Server access key (hidden; Enter cancels): ")
    return key or None


def run_address_install(
    home,
    native,
    endpoint=None,
    *,
    transport=None,
    store_factory=access_store,
    dependency_preparer=wizard.prepare_dependencies,
    install=wizard.run,
    address_input=None,
    key_input=hidden_input,
):
    state = Path(home) / ".config/agent-brain-client/access-bootstrap"
    try:
        # A new address cannot replace an existing install or unfinished legacy transaction.
        root = Path(home) / ".config/agent-brain-client"
        marker = state / "endpoint.json"
        if root.exists() and not marker.exists():
            allowed = {"install-runtime", "installer-checkouts"}
            if state.exists():
                owned(state, directory=True)
                if set(p.name for p in state.iterdir()) <= {"first-install.lock"}:
                    allowed.add("access-bootstrap")
            if any(p.name not in allowed for p in root.iterdir()):
                raise ValueError("Existing installation materials preserved")
        if marker.exists():
            saved_endpoint = wizard.private_json(owned(marker))
            if (
                set(saved_endpoint) != {"schema", "endpoint"}
                or type(saved_endpoint["schema"]) is not int
                or saved_endpoint["schema"] != 1
            ):
                raise ValueError("Saved Server identity changed")
            saved = endpoint_check(saved_endpoint["endpoint"])
            if endpoint is not None and endpoint_check(endpoint) != saved:
                raise ValueError("Existing Server identity preserved")
            endpoint = saved
        else:
            if endpoint is None:
                if address_input is None:
                    print("Agent Brain Server HTTPS address:", file=sys.stderr)
                    endpoint = input().strip()
                else:
                    endpoint = address_input(
                        "Agent Brain Server HTTPS address: "
                    ).strip()
            endpoint = endpoint_check(endpoint)
        # Serialize bootstrap and local install together. The existing install also
        # leases first-install separately; no key-store lock is held during apply.
        with wizard.private_lease(state):
            wizard.same_private(marker, {"schema": 1, "endpoint": endpoint})
            print("Agent Brain Server: " + endpoint, file=sys.stderr)
            materials = dependency_preparer(Path(home))
            store = store_factory(home, endpoint, materials)
            peer = transport or AccessHTTPS(endpoint)
            with store.lease():
                record = store.load()
                if record is None:
                    key = key_input(store)
                    if key is None:
                        return {
                            "status": "access-input-cancelled",
                            "installed": False,
                            "native_changed": False,
                        }
                    if not valid_secret(key):
                        return {
                            "status": "access-input-invalid",
                            "installed": False,
                            "native_changed": False,
                        }
                    record = {"schema": 1, "endpoint": endpoint, "key": key}
                    store.save(record, None)
                if (
                    not isinstance(record, dict)
                    or set(record) != {"schema", "endpoint", "key"}
                    or type(record["schema"]) is not int
                    or record["schema"] != 1
                    or record["endpoint"] != endpoint
                    or not valid_secret(record["key"])
                ):
                    raise ValueError("Access credential binding changed")
                try:
                    connection = peer.bootstrap(record["key"], native_platform())
                except AccessDenied:
                    store.clear(
                        record
                    )  # Only exact rejected origin credential, never device/cache/queue.
                    return {
                        "status": "access-key-rejected",
                        "installed": False,
                        "native_changed": False,
                        "next_action": "Rerun with a valid Server access key in the hidden local input. Existing device credentials and offline queues are preserved.",
                    }
            connection = wizard.connection_value(connection)
            if connection["endpoint"] != endpoint:
                raise ValueError("Selected Server identity changed")
            # Persist authenticated metadata before the existing transaction. Its
            # canonical digest is a local drift check, never an external trust claim.
            wizard.same_private(state / "connection.json", connection)
            return install(
                home,
                native,
                connection,
                wizard.sha(canonical(connection)),
                dependency_preparer=lambda *a, **k: materials,
                transport=peer,
                secure_input=lambda: secrets.token_urlsafe(32),
                access_mode=True,
            )
    except EOFError:
        return {
            "status": "access-input-cancelled",
            "installed": False,
            "native_changed": False,
            "credential_accessed": False,
        }
    except KeyboardInterrupt:
        return {
            "status": "access-install-interrupted",
            "installed": False,
            "native_changed": None,
        }
    except Exception:
        return {
            "status": "access-install-resume-required",
            "installed": False,
            "native_changed": None,
            "next_action": "Retry the same Server address. Preserve the system credentials, authenticated metadata and installation receipts; authentication alone does not complete installation.",
        }

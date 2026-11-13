"""Stop/resume collection while retaining local fences until confirmation."""

from typing import Protocol
from agent_brain_client.domain.contract import recall_options


class CollectionControlPorts(Protocol):
    def set_fence(self, value: dict) -> None: ...
    def clear_fence(self, value: dict) -> None: ...
    def admission(self, value: dict) -> dict: ...
    def remote(self, value: dict, method: str) -> dict: ...
    def stop_project(self, row: dict) -> None: ...
    def resume_project(self, row: dict) -> None: ...
    def save_settings(self, value: dict) -> None: ...


class CollectionControl:
    def __init__(self, ports: CollectionControlPorts):
        self.ports = ports

    def stop(self, value):
        self.ports.set_fence(value)
        if value["server_scope_sha256"]:
            response = self.ports.remote(value, "memory_global_stop")
            if "error" in response:
                raise ValueError("Remote global control unconfirmed")
        else:
            for row in value["legacy_projects"]:
                self.ports.stop_project(row)
        return {
            "status": "global-memory-stopped",
            "remote_confirmed": bool(
                value["server_scope_sha256"] or value["legacy_projects"]
            ),
        }

    def configure_recall(self, value, options):
        if not isinstance(options, dict):
            raise ValueError("Native recall options required")
        settings = dict(options)
        limit = settings.pop("maxResults", 5)
        if type(limit) is not int or not 1 <= limit <= 2**31 - 1:
            raise ValueError("Native L1 count required")
        recall_options(settings)
        value["recall"] = options
        self.ports.save_settings(value)
        return {
            "status": "native-recall-configured",
            "recall": options,
            "source_scope_changed": False,
            "model_calls": 0,
        }

    def resume(self, value):
        """Resume only an already approved source; preserve fences on uncertainty."""
        legacy_sources = [
            row
            for row in value.get("legacy_projects", [])
            if row.get("capture_delegated", True)
        ]
        if not value["server_scope_sha256"] and legacy_sources:
            # Preserve the original per-project control path; it cannot activate
            # the unapproved expanded scope on this device.
            self.ports.set_fence(value)
            projects = []
            for row in legacy_sources:
                try:
                    self.ports.resume_project(row)
                except (ValueError, OSError):
                    projects.append({"root": row["root"], "remote_confirmed": False})
                else:
                    projects.append({"root": row["root"], "remote_confirmed": True})
            if not all(project["remote_confirmed"] for project in projects):
                return {
                    "status": "global-memory-resume-unconfirmed",
                    "collection_enabled": False,
                    "legacy_capture_resumed": False,
                    "remote_confirmed": False,
                    "local_stop_fence": True,
                    "projects": projects,
                    "next_action": "Inspect unconfirmed project admission and retry explicit global-resume; preserve the local stop fence.",
                }
            self.ports.clear_fence(value)
            return {
                "status": "global-memory-legacy-resumed",
                "collection_enabled": False,
                "legacy_capture_resumed": True,
                "remote_confirmed": True,
                "projects": projects,
                "next_action": self.ports.admission(value)["next_action"],
            }
        state = self.ports.admission(value)
        if any(reason != "local-stop-fence" for reason in state["reasons"]):
            return {
                "status": "global-memory-activation-required",
                "collection_enabled": False,
                "remote_confirmed": False,
                "admission": state,
                "permissions_changed": False,
            }
        self.ports.set_fence(value)
        response = self.ports.remote(value, "memory_global_resume")
        result = response.get("result", {})
        if (
            "error" in response
            or result.get("schema") != 1
            or result.get("stopped") is not False
        ):
            return {
                "status": "global-memory-resume-unconfirmed",
                "collection_enabled": False,
                "remote_confirmed": False,
                "server_error": response.get("error"),
                "local_stop_fence": True,
                "next_action": "Inspect global-status --check-server and the existing Server source; preserve the local stop fence.",
            }
        self.ports.clear_fence(value)
        return {
            "status": "global-memory-resumed",
            "collection_enabled": True,
            "remote_confirmed": True,
        }

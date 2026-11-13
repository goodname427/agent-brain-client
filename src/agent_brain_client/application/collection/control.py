"""Stop/resume collection while retaining local fences until confirmation."""

from typing import Protocol
from agent_brain_client.domain.contract import recall_options


class CollectionControlPorts(Protocol):
    def set_fence(self, value: dict) -> None: ...
    def clear_fence(self, value: dict) -> None: ...
    def admission(self, value: dict) -> dict: ...
    def remote(self, value: dict, method: str) -> dict: ...
    def fence_project(self, row: dict, *, global_only: bool = False) -> None: ...
    def clear_project_fence(self, row: dict) -> None: ...
    def stop_project(self, row: dict) -> None: ...
    def resume_project(self, row: dict) -> None: ...
    def save_settings(self, value: dict) -> None: ...


class CollectionControl:
    def __init__(self, ports: CollectionControlPorts):
        self.ports = ports

    def stop(self, value):
        self.ports.set_fence(value)
        projects = []
        # Every original project Hook has its own admission fence. Complete
        # local stops before credentials or a slow/failing remote are accessed.
        for row in value["legacy_projects"]:
            project = {"root": row["root"], "local_stop_fence": False,
                       "remote_confirmed": False}
            try:
                self.ports.fence_project(row, global_only=bool(value["server_scope_sha256"]))
            except Exception as error:
                project.update(error_stage="local-stop", error_type=type(error).__name__)
            else:
                project["local_stop_fence"] = True
            projects.append(project)
        remote_confirmed = False
        remote_error = {}
        if value["server_scope_sha256"]:
            try:
                response = self.ports.remote(value, "memory_global_stop")
                if "error" in response:
                    raise ValueError("Remote global control unconfirmed")
            except Exception as error:
                remote_error = {"error_stage": "remote-stop", "error_type": type(error).__name__}
            else:
                remote_confirmed = True
        else:
            for row, project in zip(value["legacy_projects"], projects):
                try:
                    self.ports.stop_project(row)
                except Exception as error:
                    project.update(remote_error_type=type(error).__name__)
                else:
                    project["remote_confirmed"] = True
            remote_confirmed = bool(projects) and all(p["remote_confirmed"] for p in projects)
        complete = all(p["local_stop_fence"] for p in projects) and not remote_error
        if not value["server_scope_sha256"] and projects:
            complete = complete and remote_confirmed
        return {
            "status": "global-memory-stopped" if complete else "global-memory-stop-unconfirmed",
            "remote_confirmed": remote_confirmed,
            "local_stop_fence": True,
            "projects": projects,
            **remote_error,
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
        projects = []
        # Approved global control never stopped each project's remote source.
        # Lift only the separate local fences created for that global stop;
        # independently stopped or migrated project markers stay untouched.
        for row in value.get("legacy_projects", []):
            try:
                self.ports.clear_project_fence(row)
            except Exception as error:
                projects.append({"root": row["root"], "local_stop_fence": True,
                                 "error_type": type(error).__name__})
            else:
                projects.append({"root": row["root"], "local_stop_fence": False})
        if any(project["local_stop_fence"] for project in projects):
            return {"status": "global-memory-resume-unconfirmed",
                    "collection_enabled": False, "remote_confirmed": True,
                    "local_stop_fence": True, "projects": projects}
        self.ports.clear_fence(value)
        return {
            "status": "global-memory-resumed",
            "collection_enabled": True,
            "remote_confirmed": True,
        }

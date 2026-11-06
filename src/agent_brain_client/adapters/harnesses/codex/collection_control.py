"""Native fence and per-project controls, without command or admission decisions."""

from pathlib import Path
from agent_brain_client.adapters.storage import metadata


class CollectionControlIO:
    def __init__(
        self, *, settings_path, admission, client, project_load, project_command
    ):
        self.settings_path = settings_path
        self.admission = admission
        self.client = client
        self.project_load = project_load
        self.project_command = project_command

    def set_fence(self, value):
        (self.settings_path(value["device_home"]).parent / "global.off").touch(
            mode=0o600
        )

    def clear_fence(self, value):
        (self.settings_path(value["device_home"]).parent / "global.off").unlink()

    def remote(self, value, method):
        return self.client(value).wire(method, {})

    def stop_project(self, row):
        source = self.project_load(row["settings"], row["settings_sha256"])
        Path(source["off_file"]).touch(mode=0o600)
        self.project_command(source, "stop")

    def resume_project(self, row):
        source = self.project_load(row["settings"], row["settings_sha256"])
        self.project_command(source, "resume")

    def save_settings(self, value):
        metadata.write(self.settings_path(value["device_home"]), value)

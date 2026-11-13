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

    def fence_project(self, row, *, global_only=False):
        source = self.project_load(row["settings"], row["settings_sha256"])
        marker = Path(source["off_file"])
        if global_only:
            marker = marker.with_name("global-memory.off")
        if not marker.exists():
            try:
                # Use the private writer so a new Windows marker has its exact
                # owner as well as its DACL before later resume validates it.
                metadata.write(marker, {"schema": 1, "control": "global-stop"} if global_only else {}, exclusive=True)
            except FileExistsError:
                pass  # A concurrent stop remains fenced; preserve its marker.

    def clear_project_fence(self, row):
        source = self.project_load(row["settings"], row["settings_sha256"])
        marker = Path(source["off_file"]).with_name("global-memory.off")
        if marker.exists() or marker.is_symlink():
            if metadata.private_json(marker) != {"schema": 1, "control": "global-stop"}:
                raise ValueError("Unknown global project stop marker preserved")
            marker.unlink()

    def stop_project(self, row):
        source = self.project_load(row["settings"], row["settings_sha256"])
        self.project_command(source, "stop")

    def resume_project(self, row):
        source = self.project_load(row["settings"], row["settings_sha256"])
        self.project_command(source, "resume")
        self.clear_project_fence(row)

    def save_settings(self, value):
        metadata.write(self.settings_path(value["device_home"]), value)

"""Native host file semantics used by storage transactions, without journals."""

import os
from agent_brain_client.adapters.platforms.filesystem import file_mode


def inplace_host_updates():
    return os.name == "nt"


def open_host_file(path):
    from agent_brain_client.adapters.platforms.windows.host_files import open_host_file

    return open_host_file(path)


def validate_host_path(path):
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.native import canonical

        canonical(path)


def host_metadata(info):
    if os.name == "nt":
        return getattr(info, "st_file_attributes", 0)
    return (info.st_uid, info.st_gid, info.st_mode & 0o777)


def prepare_atomic_mode(file, mode, *, cache):
    if not cache or os.name != "nt":
        file_mode(file.fileno(), mode)


def finish_atomic_mode(path, mode):
    if os.name != "nt":
        os.chmod(path, mode)


def prepare_host_mode(file, mode, image, *, private):
    if os.name == "nt":
        if private:
            file_mode(file.fileno(), mode)
        return
    from agent_brain_client.adapters.platforms.posix.host_files import prepare_host_mode

    prepare_host_mode(file, mode, image)


def publish_new_host_file(pending, path):
    if os.name == "nt":
        os.rename(pending, path)
    else:
        os.link(pending, path)
        pending.unlink()

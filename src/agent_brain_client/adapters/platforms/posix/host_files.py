"""POSIX host file ownership and mode preservation."""

import os


def prepare_host_mode(file, mode, image):
    if image is not None:
        info = os.fstat(file.fileno())
        if (info.st_uid, info.st_gid) != image[4][:2]:
            os.fchown(file.fileno(), *image[4][:2])
    os.fchmod(file.fileno(), mode)

"""Extended local Win32 paths for bounded native transcript I/O."""

from pathlib import Path


def io_path(path):
    text = str(path)
    if text.startswith("\\\\") or ".." in path.parts or not path.drive:
        raise ValueError("Local unambiguous native source path required")
    # Extended paths must not turn an ordinary Win32 alias into a different
    # file, or admit an alternate data stream/device namespace.
    for part in path.parts[1:]:
        if (":" in part or part.endswith((" ", "."))
                or part.split(".")[0].upper() in {
                    "CON", "PRN", "AUX", "NUL",
                    *(f"COM{i}" for i in range(10)),
                    *(f"LPT{i}" for i in range(10)),
                }):
            raise ValueError("Unambiguous native source name required")
    return Path("\\\\?\\" + text)


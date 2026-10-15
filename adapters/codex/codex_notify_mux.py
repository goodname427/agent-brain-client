"""Dispatch one Codex notify event to independent, configured callbacks.

Codex appends its event as one JSON argv to the configured notify command.
Each configured command is itself encoded as a JSON array of argv strings;
the original event JSON is appended unchanged to both callback argv lists.
"""

import json
import subprocess
import sys

_TIMEOUT_SECONDS = 8
_MAX_EVENT_BYTES = 1_048_576
_MAX_COMMAND_BYTES = 16_384


def _decode_command(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_COMMAND_BYTES:
        return None
    try:
        command = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if (not isinstance(command, list) or not command or
            any(not isinstance(part, str) or not part for part in command)):
        return None
    return command


def _run_independently(command, event_json, runner):
    """Run one callback without allowing its failure to skip the next one."""
    try:
        result = runner(command + [event_json], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        close_fds=True, timeout=_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return getattr(result, "returncode", 1) == 0


def dispatch_notify(argv, runner=subprocess.run):
    """Dispatch to both commands; return nonzero if either callback fails.

    ``argv`` excludes the executable name and must end in Codex's event JSON.
    The preceding arguments must contain exactly one ``--callback-argv-json``
    and one ``--capture-argv-json`` value, in either order.
    """
    if not isinstance(argv, (list, tuple)) or len(argv) < 5:
        return 0
    event_json = argv[-1]
    if not isinstance(event_json, str) or len(event_json.encode("utf-8")) > _MAX_EVENT_BYTES:
        return 0
    try:
        json.loads(event_json)
    except (ValueError, TypeError):
        return 0

    options = argv[:-1]
    commands = {}
    index = 0
    while index < len(options):
        flag = options[index]
        if flag not in ("--callback-argv-json", "--capture-argv-json") or index + 1 >= len(options):
            return 0
        if flag in commands:
            return 0
        commands[flag] = _decode_command(options[index + 1])
        if commands[flag] is None:
            return 0
        index += 2
    if set(commands) != {"--callback-argv-json", "--capture-argv-json"}:
        return 0

    # Evaluate both independently, even if the first command raises, times out,
    # or returns nonzero. Never put event content in diagnostics.
    callback_ok = _run_independently(commands["--callback-argv-json"], event_json, runner)
    capture_ok = _run_independently(commands["--capture-argv-json"], event_json, runner)
    return 0 if callback_ok and capture_ok else 1


def main(argv=None):
    return dispatch_notify(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())

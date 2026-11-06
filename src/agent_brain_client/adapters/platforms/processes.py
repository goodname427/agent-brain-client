"""Run native helper processes under the current request cancellation/deadline."""

import subprocess
from agent_brain_client.application.control import current_request, request_timeout


def controlled_run(runner, command, **kwargs):
    """Read-only helper invocation; unchanged runner for pairing and writes."""
    control = current_request()
    if control is None or runner is not subprocess.run:
        return runner(command, **kwargs)
    control.check()
    options = dict(kwargs)
    data = options.pop("input", None)
    timeout = request_timeout(options.pop("timeout"))
    options.pop("capture_output")
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **options,
    ) as process:
        control.register(lambda: process.kill() if process.poll() is None else None)
        try:
            stdout, stderr = process.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            control.cancel("request_deadline_exceeded")
            process.kill()
            process.communicate()
            control.check()
            raise
        control.check()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

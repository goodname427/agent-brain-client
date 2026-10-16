"""Dispatch one Codex notify event to independent, configured callbacks.

Codex appends its event as one JSON argv to the configured notify command.
Each configured command is itself encoded as a JSON array of argv strings.
The original callback gets the event unchanged; capture receives a reconstructed
event containing only validated thread/turn identifiers and no message text.
"""

import json
import hashlib
import fcntl
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

_TIMEOUT_SECONDS = 8
_MAX_EVENT_BYTES = 1_048_576
_MAX_COMMAND_BYTES = 16_384
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TURN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_CAPTURE_MARKER = "AGENTBRAIN_NOTIFY_CAPTURE:"


def _valid_job(job):
    return (isinstance(job, dict) and
            set(job) == {"job_id", "thread_id", "turn_id", "turn_digest", "status",
                         "attempts", "created_at", "lease_until"} and
            isinstance(job["thread_id"], str) and _THREAD_ID_RE.fullmatch(job["thread_id"]) and
            isinstance(job["turn_id"], str) and _TURN_ID_RE.fullmatch(job["turn_id"]) and
            isinstance(job["turn_digest"], str) and _DIGEST_RE.fullmatch(job["turn_digest"]) and
            job["job_id"] == hashlib.sha256((job["thread_id"] + "\0" + job["turn_digest"]).encode()).hexdigest() and
            job["status"] in ("pending", "processing", "done", "failed") and
            isinstance(job["attempts"], int) and job["attempts"] >= 0 and
            isinstance(job["created_at"], (int, float)) and math.isfinite(job["created_at"]) and
            (job["lease_until"] is None or
             (isinstance(job["lease_until"], (int, float)) and math.isfinite(job["lease_until"]))) and
            ((job["status"] == "processing") == (job["lease_until"] is not None)))


class NotifyCaptureQueue:
    """Durable private queue for capture jobs; never stores event text."""

    def __init__(self, ledger_path, *, max_jobs=1000, lease_seconds=120, clock=time.time):
        if not isinstance(max_jobs, int) or max_jobs < 1 or lease_seconds <= 0:
            raise ValueError("invalid notify queue bounds")
        self.path = Path(ledger_path)
        self.max_jobs = max_jobs
        self.lease_seconds = float(lease_seconds)
        self.clock = clock

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        lock_path = self.path.with_name(self.path.name + ".lock")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read(self):
        if not self.path.exists():
            return {"schema": 1, "jobs": []}
        info = self.path.lstat()
        if not self.path.is_file() or info.st_mode & 0o077 or info.st_size > 1_048_576:
            raise OSError("notify queue permissions are unsafe")
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or set(state) != {"schema", "jobs"} or
                state.get("schema") != 1 or not isinstance(state.get("jobs"), list) or
                len(state["jobs"]) > self.max_jobs or any(not _valid_job(job) for job in state["jobs"])):
            raise OSError("notify queue is invalid")
        return state

    def _write(self, state):
        raw = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
        fd, temp_path = tempfile.mkstemp(prefix=".notify-jobs.", dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _enqueue(self, event, *, supersede):
        if (not isinstance(event, dict) or event.get("type") != "agent-turn-complete" or
                not isinstance(event.get("input-messages"), list) or not event["input-messages"] or
                any(not isinstance(item, str) or len(item) > 65536 for item in event["input-messages"]) or
                any(item.startswith(_CAPTURE_MARKER) for item in event["input-messages"])):
            return None
        thread_id, turn_id = event.get("thread-id"), event.get("turn-id")
        if not isinstance(thread_id, str) or not _THREAD_ID_RE.fullmatch(thread_id):
            return None
        if not isinstance(turn_id, str) or not _TURN_ID_RE.fullmatch(turn_id):
            return None
        digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        job_id = hashlib.sha256((thread_id + "\0" + digest).encode()).hexdigest()
        with self._locked():
            state = self._read()
            existing = next((job for job in state["jobs"] if job.get("job_id") == job_id), None)
            if existing is not None:
                return {"job_id":job_id,"created":False,"status":existing["status"]} if supersede else job_id
            now=float(self.clock())
            # A later stop event cannot stand in for the messages that belonged
            # to an older digest. Expired work and unstarted older jobs are terminal.
            if supersede:
                for prior in state["jobs"]:
                    if prior["status"] == "pending" or (prior["status"] == "processing" and prior["lease_until"] <= now):
                        prior.update(status="failed",lease_until=None)
            state["jobs"].append({"job_id": job_id, "thread_id": thread_id,
                                  "turn_id": turn_id, "turn_digest": digest,
                                  "status": "pending", "attempts": 0,
                                  "created_at": now, "lease_until": None})
            terminal = [job for job in state["jobs"] if job["status"] in ("done","failed")]
            while len(state["jobs"]) > self.max_jobs and terminal:
                state["jobs"].remove(terminal.pop(0))
            if len(state["jobs"]) > self.max_jobs:
                raise OSError("notify queue is full")
            self._write(state)
        return {"job_id":job_id,"created":True,"status":"pending"} if supersede else job_id

    def enqueue(self, event):
        """Persist a durable job and return its stable identifier."""
        return self._enqueue(event,supersede=False)

    def enqueue_for_dispatch(self, event):
        """Queue a fresh stop event and make older unstarted work terminal."""
        return self._enqueue(event,supersede=True)

    def claim(self, job_id):
        now = float(self.clock())
        with self._locked():
            state = self._read()
            for job in state["jobs"]:
                if job["status"] == "processing" and job["lease_until"] <= now:
                    job.update(status="pending", lease_until=None)
            job = next((item for item in state["jobs"] if item["job_id"] == job_id and item["status"] == "pending"), None)
            if job is None:
                return None
            job.update(status="processing", attempts=job["attempts"] + 1,
                       lease_until=now + self.lease_seconds)
            self._write(state)
            return dict(job)

    def claim_next(self):
        """Deprecated compatibility helper; callers should claim a specific event."""
        now=float(self.clock())
        with self._locked():
            state=self._read()
            changed=False
            for item in state["jobs"]:
                if item["status"]=="processing" and item["lease_until"]<=now:
                    item.update(status="pending",lease_until=None);changed=True
            if changed:self._write(state)
            job=next((item for item in state["jobs"] if item["status"]=="pending"),None)
            job_id=job["job_id"] if job is not None else None
        return self.claim(job_id) if job_id is not None else None

    def finish(self, job_id, *, success, terminal_failure=False):
        with self._locked():
            state = self._read()
            for job in state["jobs"]:
                if job["job_id"] == job_id and job["status"] == "processing":
                    job.update(status="done" if success else ("failed" if terminal_failure else "pending"), lease_until=None)
                    self._write(state)
                    return True
        return False

    def snapshot(self):
        with self._locked():
            return [dict(job) for job in self._read()["jobs"]]


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

    # Preserve the pre-existing callback's exact argv. Store only IDs in the
    # durable queue, and only dispatch the event delivered by this invocation.
    callback_ok = _run_independently(commands["--callback-argv-json"], event_json, runner)
    try:
        event = json.loads(event_json)
        queue_path = os.environ.get("AGENTBRAIN_NOTIFY_QUEUE_PATH")
        if not queue_path:
            queue_path = str(Path.home() / ".cache" / "agent-brain-client" / "notify-capture" / "jobs.json")
        capture_queue = NotifyCaptureQueue(queue_path)
        queued = capture_queue.enqueue_for_dispatch(event)
        job = capture_queue.claim(queued["job_id"]) if queued["created"] else None
    except (OSError, ValueError, TypeError):
        job = None
        queue_ok = False
    else:
        queue_ok = True
    capture_ok = True
    if job is not None:
        # Reconstruct only non-sensitive eligibility metadata. The original
        # messages are intentionally not persisted or replayed to the worker.
        job_event = json.dumps({"type": "agent-turn-complete",
                                "thread-id": job["thread_id"],
                                "turn-id": job["turn_id"],
                                "input-messages": ["validated notify input"]},
                               separators=(",", ":"))
        capture_ok = _run_independently(commands["--capture-argv-json"], job_event, runner)
        try:
            capture_queue.finish(job["job_id"], success=capture_ok, terminal_failure=not capture_ok)
        except (OSError, ValueError, TypeError):
            capture_ok = False
    return 0 if callback_ok and queue_ok and capture_ok else 1


def main(argv=None):
    return dispatch_notify(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())

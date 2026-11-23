"""Start the approved MCP worker, run checks or dispatch a finite event Hook."""

import sys
from pathlib import Path

import argparse
import json
from collections import deque
from dataclasses import dataclass, replace
import hashlib
from queue import Empty, Queue
import subprocess
import threading
import time

MCP_SINGLE_ENTRY_SUPERVISOR = 1
MAX_MESSAGE = 131072
QUEUE_DEADLINE_ERROR = {
    "code": -32001,
    "message": "request_deadline_exceeded",
    "data": {"stage": "queue"},
}


class ProtocolError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ProtocolError(-32602, "Invalid parameters; preserve existing data.")


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def write_response(output, response):
    output.write(json.dumps(response, ensure_ascii=True) + "\n")
    output.flush()


class BackgroundTasks:
    """Dispatch bounded work from the connection's existing release watcher."""

    def __init__(self, run, *, clock=time.monotonic, thread=threading.Thread):
        self.run, self.clock, self.thread = run, clock, thread
        self.due = dict.fromkeys(("background-content", "background-update"), 0)
        self.jobs = {}
        self.retries = Queue()

    def release_changed(self):
        self.due["background-content"] = 0

    def tick(self):
        now = self.clock()
        while not self.retries.empty():
            task, delay = self.retries.get_nowait()
            self.due[task] = min(self.due[task], now + delay)
        for task, due in self.due.items():
            worker = self.jobs.get(task)
            if now < due or worker is not None and worker.is_alive():
                continue
            worker = self.thread(target=self._run, args=(task,), daemon=True, name="agentbrain-" + task)
            worker.start()
            self.jobs[task] = worker
            self.due[task] = now + 60

    def _run(self, task):
        result = self.run(task)
        if task == "background-content" and result:
            retry = result.get("retry_after")
            if type(retry) in (float, int) and 0 <= retry < 60:
                self.retries.put((task, retry))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-file", type=Path, required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--harness", choices=("codex", "codebuddy"), required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument(
        "--installation-check",
        action="store_true",
        help="Read-only content protocol/authentication check; no native mapping.",
    )
    parser.add_argument(
        "--bootstrap-check",
        action="store_true",
        help="Offline check of this launcher and the active verified package; no activation or credential access.",
    )
    parser.add_argument("--event-hook", action="store_true")
    parser.add_argument("--event-hook-policy", type=Path)
    parser.add_argument("--event-hook-policy-sha256")
    parser.add_argument(
        "--private-stdio", choices=("plan", "check", "discover", "resolve", "verify-retained", "worker",
                                   "background-content", "background-update"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--private-ticket", help=argparse.SUPPRESS)
    parser.add_argument("--managed-stdio", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    finite = args.check_only or args.installation_check or args.bootstrap_check or args.event_hook
    if not finite and args.private_stdio is None:
        if args.private_ticket is not None or args.managed_stdio is not None:
            parser.error("Private MCP arguments require a private operation")
        return isolated_stdio(args)
    return finite_startup(args)


def finite_startup(args):
    # This code is reached only for explicit finite modes or in a transient child.
    if __package__ in (None, ""):
        root = Path(__file__).resolve().parents[4]
        sys.path.insert(0, str(root / "src"))
    from agent_brain_client.runtime import launcher as runtime
    from agent_brain_client.adapters.platforms.filesystem import PlatformUnsupported

    try:
        if args.private_stdio is not None:
            if args.check_only or args.installation_check or args.bootstrap_check or args.event_hook:
                raise ValueError("Distinct private MCP operation required")
            result = runtime.isolated_stdio_operation(
                args.policy_file, args.policy_sha256, args.harness,
                args.private_stdio, args.private_ticket, args.managed_stdio,
            )
            if args.private_stdio != "worker":
                print(json.dumps(result))
                return 0
            if not result.get("served"):
                print(json.dumps({"stage": "mcp-worker-start", "status": result.get("status")}),
                      file=sys.stderr)
            return result.get("exit_code", 2)

        if bool(args.event_hook_policy) != bool(
            args.event_hook_policy_sha256
        ) or args.event_hook != bool(args.event_hook_policy):
            raise ValueError("Exact finite hook policy required")
        result = runtime.startup(
            args.policy_file,
            args.policy_sha256,
            args.check_only,
            args.installation_check,
            args.bootstrap_check,
            (args.event_hook_policy, args.event_hook_policy_sha256)
            if args.event_hook
            else None,
            harness=args.harness,
        )
    except PlatformUnsupported as error:
        result = error.result(served=False, native_changed=False)
    except Exception:
        result = {
            "status": "startup-review-required",
            "served": False,
            "next_action": "Inspect the approved fixed bootstrap, policy digest and isolated client paths; preserve existing packages.",
        }
    if args.check_only or args.bootstrap_check:
        print(json.dumps(result))
    elif not result.get("served"):
        print(json.dumps(result), file=sys.stderr)
    if args.event_hook:
        # A capture failure must not block UserPromptSubmit or force another
        # model continuation. Keep failure/pending evidence, return no context.
        if not result.get("served"):
            print("{}")
        elif result.get("exit_code", 0) != 0:
            # The host sees success by design. Preserve a bounded indication
            # that dispatch reached this launcher but its worker failed, even
            # when that worker could not emit its own startup diagnostic.
            print(
                json.dumps(
                    {"stage": "event-hook-worker", "error_code": "hook_worker_failed"}
                ),
                file=sys.stderr,
            )
        return 0
    checked = (
        args.check_only
        and result["status"] in ("current", "updated", "offline")
        and bool(result.get("active"))
    )
    bootstrap_checked = (
        args.bootstrap_check and result["status"] == "client-bootstrap-verified"
    )
    return result.get("exit_code", 0 if checked or bootstrap_checked else 2)


def isolated_stdio(args):
    """The registered process only supervises streams and transient children."""
    entry = str(Path(__file__).resolve())
    base = (
        sys.executable, "-I", "-S", "-B", entry, "--policy-file", str(args.policy_file),
        "--policy-sha256", args.policy_sha256, "--harness", args.harness,
    )

    def probe(mode, ticket=None, *, timeout=25):
        command = [*base, "--private-stdio", mode]
        if ticket is not None:
            command += ["--private-ticket", ticket]
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                timeout=timeout, check=True)
        if len(result.stdout) > 16384:
            raise ValueError("Bounded private MCP response required")
        return json.loads(result.stdout)

    def worker(plan, snapshot=None):
        ticket = json.dumps(plan["ticket"], separators=(",", ":"), sort_keys=True)
        tools = plan["enabled_tools"]
        return WorkerSpec((ticket,), plan["fingerprint"], plan["cache_abi"],
                          None if tools is None else frozenset(tools), snapshot)

    retained = None

    def remember_worker(spec, command):
        nonlocal retained
        retained = (spec, command)

    def prepare_worker(spec):
        try:
            if retained is not None and spec == retained[0]:
                # The session already received and admitted this exact command.
                # Recheck its bytes even when the update journal has advanced.
                resolved = probe("verify-retained", spec.command[0])
                command = retained[1]
            else:
                resolved = probe("resolve", spec.command[0])
                command = resolved["command"]
            if (
                resolved["fingerprint"] != spec.fingerprint or resolved["cache_abi"] != spec.cache_abi
                or not isinstance(command, (tuple, list)) or not command
                or not all(isinstance(arg, str) for arg in command)
            ):
                raise ValueError("Worker plan no longer matches the selected snapshot")
            return command
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            raise RuntimeError("Verified MCP worker unavailable") from error

    try:
        first = probe("plan")
        if first.get("legacy"):
            # Historical non-managed transports keep their established startup.
            # The registered process still does not import their runtime.
            return subprocess.run([*base, "--private-stdio", "worker"]).returncode
        initial = worker(first)
        watch_paths = tuple(map(Path, first["watch"]))
        stamp = first["ticket"]["registration"]
        stamp_ticket = json.dumps({"registration": stamp})
        background_ticket = json.dumps({"registration": first["ticket"]["native_registration"]})
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(json.dumps({"stage": "mcp-supervisor", "status": "verified-startup-unavailable"}),
              file=sys.stderr)
        return 2

    def token():
        state = []
        for path in watch_paths:
            try:
                info = path.stat()
                state.append((info.st_ino, info.st_mtime_ns, info.st_size))
            except FileNotFoundError:
                state.append(None)
        return tuple(state)

    last = None
    initial = replace(initial, binding_token=token())
    active_commit = first["ticket"]["active"]["commit"]

    def observe():
        nonlocal last, active_commit, background_ticket
        # An unchanged local snapshot avoids process boots/hash checks at idle.
        before = token()
        if before == last:
            return None
        plan = probe("plan")
        if before != token():
            return None
        if plan["ticket"]["registration"] != stamp:
            last = before
            return False
        result = worker(plan, before)
        background_ticket = json.dumps({"registration": plan["ticket"]["native_registration"]})
        commit = plan["ticket"]["active"]["commit"]
        if commit != active_commit:
            active_commit = commit
            tasks.release_changed()
        last = before
        return result

    def registration_current():
        return probe("check", stamp_ticket).get("current") is True

    def binding_observed(spec):
        try:
            return token() == spec.binding_token
        except OSError:
            return False

    def background(task):
        try:
            return probe(task, background_ticket, timeout=55 if task == "background-content" else 120)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None  # Background work never owns the MCP streams.

    tasks = BackgroundTasks(background)
    return supervise_stdio(initial, observe, sys.stdin.buffer, sys.stdout, {},
                           idle_timeout=30, can_switch=registration_current,
                           prepare_worker=prepare_worker, remember_worker=remember_worker,
                           background_tick=tasks.tick,
                           binding_observed=binding_observed)


# The fixed launcher owns the host stream. The worker's startup record and drain
# request are private to the child pipes and never forwarded to the host.
HANDOFF_VERSION = 1
DRAIN_METHOD = "agentbrain/private/drain"
DRAIN_PREFIX = DRAIN_METHOD + "/"
UNKNOWN_OUTCOME = {
    "code": -32003,
    "message": "MCP worker exited before acknowledging the request.",
    "data": {"outcome": "unknown", "automatic_replay": False},
}


@dataclass(frozen=True)
class WorkerSpec:
    command: tuple
    fingerprint: str
    cache_abi: str
    enabled_tools: frozenset[str] | None = None
    binding_token: tuple | None = None


def runtime_files(manifest):
    return {path: sha for path, sha in manifest["files"].items() if path.startswith("src/")}


def runtime_fingerprint(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def tool_contract(tools):
    result = {}
    for tool in tools:
        name = tool["name"]
        if not isinstance(name, str) or name in result:
            raise ValueError("Unique named MCP tools required")
        result[name] = {
            key: value for key, value in tool.items()
            if key not in ("description", "title", "icons")
        }
    return result


def _pump(source, events, owner, *, limit=-1, slots=None, stop=None):
    try:
        while True:
            if slots is not None:
                while not slots.acquire(timeout=0.1):
                    if stop.is_set():
                        return
                if stop.is_set():
                    return
            line = source.readline(limit)
            events.put((owner, line or None, time.monotonic()))
            if not line:
                return
            if limit >= 0 and len(line) >= limit:
                return
    except (OSError, ValueError):
        events.put((owner, None, time.monotonic()))


class _Worker:
    def __init__(self, spec, protocol, options, events, timeout, command=None):
        self.spec = spec
        self.command = spec.command if command is None else tuple(command)
        self.deadline = time.monotonic() + timeout
        self.process = subprocess.Popen(
            [*self.command, "--managed-stdio", protocol or "initial"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            **options,
        )
        self.reader = threading.Thread(
            target=_pump,
            args=(self.process.stdout, events, self),
            daemon=True,
            name="agentbrain-worker-output",
        )
        try:
            self.reader.start()
        except RuntimeError:
            self.process.kill()
            self.close_input()
            self.process.wait()
            self.process.stdout.close()
            raise
        self.contract = None

    def send(self, line):
        self.process.stdin.write(line)
        self.process.stdin.flush()

    def close_input(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass

    def finish(self, *, abort=False):
        if abort and self.process.poll() is None:
            self.process.kill()
        self.close_input()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join()
        self.process.stdout.close()


def _response(rid, error):
    return {"jsonrpc": "2.0", "id": rid, "error": error}


def _line(value):
    return json.dumps(value, ensure_ascii=True).encode("ascii") + b"\n"


def _read_contract(record):
    fields = (
        "abWorkerReady", "versions", "capabilities", "toolContract", "cacheIdentity", "cacheAbi", "tools",
    )
    keys(record, fields, fields)
    if type(record["abWorkerReady"]) is not int or record.pop("abWorkerReady") != HANDOFF_VERSION:
        raise ValueError("Worker readiness record required")
    if not isinstance(record["versions"], list) or not isinstance(record["tools"], list):
        raise ValueError("Invalid MCP protocol or tool catalog")
    tool_contract(record["tools"])
    for tool in record["tools"]:
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("Worker tool object input schema required")
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
            raise ValueError("Invalid required tool arguments")
    return record


def _compatible_session(first, second, protocol):
    return (
        protocol in second["versions"]
        and first["capabilities"] == second["capabilities"]
        and first["toolContract"] == second["toolContract"]
        and first["cacheIdentity"] == second["cacheIdentity"]
        and first["cacheAbi"] == second["cacheAbi"]
    )


def supervise_stdio(
    initial, observe, source, output, options, *,
    poll_interval=2, probe_timeout=20, request_timeout=90, drain_timeout=90, max_pending=255,
    can_switch=None, idle_timeout=None, prepare_worker=None, remember_worker=None, background_tick=None,
    binding_observed=None,
):
    """Keep one host session while locally verified workers change.

    One loop owns request routing. The old worker acknowledges a FIFO barrier
    through its existing executor. New requests wait behind that barrier and
    cancellation still reaches the worker that owns the request.
    """
    host, release = object(), object()
    events = Queue()
    host_slots = threading.BoundedSemaphore(max_pending)
    halt = threading.Event()
    active = candidate = None
    catalog = protocol = None
    published_tools = {}
    initialized = restart_attempted = input_closed = oversized = False
    drain_id = None
    drain_deadline = None
    drain_acked = False
    drain_sequence = 0
    parked = idle_timeout is not None
    serial_target = None
    last_activity = time.monotonic()
    last_good = initial
    rejected = None
    latest = initial
    pending = {}
    waiting = deque()

    def start(spec, seed):
        command = prepare_worker(spec) if prepare_worker is not None else None
        return _Worker(spec, seed, options, events, probe_timeout, command)

    def watch():
        if background_tick is not None and not halt.is_set():
            background_tick()
        while not halt.wait(poll_interval):
            try:
                spec = observe()
            except Exception:
                spec = None  # A transaction in progress is retried on the next tick.
            if spec is not None:
                events.put((release, spec, None))
            if background_tick is not None and not halt.is_set():
                background_tick()

    def registration_current():
        try:
            return can_switch is None or can_switch()
        except Exception:
            return False

    def report(code):
        print(json.dumps({"stage": "mcp-handoff", "error_code": code}), file=sys.stderr)

    def publish(worker):
        nonlocal published_tools
        allowed = latest.enabled_tools if latest else frozenset()
        tools = {tool["name"]: tool for tool in worker.contract["tools"]
                 if allowed is None or tool["name"] in allowed}
        if tools != published_tools:
            published_tools = tools
            if initialized and protocol is not None:
                write_response(output, {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    def binding_ready():
        return not latest or binding_observed is None or binding_observed(latest)

    def tool_error(line):
        request = json.loads(line)
        params = request.get("params")
        if request.get("jsonrpc") != "2.0" or not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return None  # The worker validates malformed requests.
        name = params["name"]
        # A retained worker cannot restore a name removed from the new registration.
        tool = published_tools.get(name)
        if tool is None:
            return {"code": -32602, "message": "Tool is unavailable in the current authorization."}
        arguments = params.get("arguments", {})
        required = tool.get("inputSchema", {}).get("required", [])
        if isinstance(arguments, dict) and any(key not in arguments for key in required):
            return {"code": -32602, "message": "Required tool arguments changed; refresh the tool list."}
        return None

    def begin_drain(now):
        nonlocal drain_sequence, drain_id, drain_deadline, drain_acked
        drain_sequence += 1
        drain_id = DRAIN_PREFIX + str(drain_sequence)
        drain_deadline = now + drain_timeout
        drain_acked = False
        try:
            active.send(_line({"jsonrpc": "2.0", "id": drain_id, "method": DRAIN_METHOD}))
        except (OSError, ValueError):
            worker_lost()

    def close_candidate(code):
        nonlocal candidate, drain_id, rejected
        if code != "host-input-closed":
            report(code)
        rejected = candidate.spec.fingerprint
        candidate.finish(abort=True)
        candidate = None
        drain_id = None

    def worker_lost():
        nonlocal active, drain_id, restart_attempted, candidate, rejected, serial_target
        lost = active
        serial_target = None
        active = None
        drain_id = None
        if lost.spec.fingerprint != last_good.fingerprint:
            rejected = lost.spec.fingerprint
        for rid, (_, owner) in list(pending.items()):
            if owner is lost:
                pending.pop(rid)
                write_response(output, _response(rid, UNKNOWN_OUTCOME))
        lost.finish(abort=True)
        report("worker-exited")
        if candidate is not None:
            candidate.finish(abort=True)
            candidate = None
        if restart_attempted:
            return
        restart_attempted = True
        try:
            candidate = start(last_good, protocol)
        except (OSError, RuntimeError):
            candidate = None
            report("worker-restart-failed")

    def dispatch(line, rid, method, deadline=None):
        if rid is not None:
            if time.monotonic() >= deadline:
                pending.pop(rid, None)
                write_response(output, _response(rid, QUEUE_DEADLINE_ERROR))
                return
            if method == "tools/call":
                error = tool_error(line)
                if error is not None:
                    pending.pop(rid, None)
                    write_response(output, _response(rid, error))
                    return
            pending[rid] = (method, active)
            # The managed worker uses the same monotonic clock. Keep the host's
            # original JSON bytes and its original deadline on either worker.
            header = f"@{int(deadline * 1_000_000_000):016x} ".encode("ascii")
            line = header + line
        try:
            active.send(line)
        except (OSError, ValueError):
            worker_lost()

    def expire_waiting(now):
        nonlocal waiting
        if not waiting or waiting[0][3] > now:
            return
        remaining = deque()
        for item in waiting:
            _, rid, _, deadline = item
            if rid is not None and deadline <= now:
                pending.pop(rid)
                write_response(output, _response(rid, QUEUE_DEADLINE_ERROR))
            else:
                remaining.append(item)
        waiting = remaining

    def flush_waiting():
        while waiting and active is not None and active.contract is not None and drain_id is None and binding_ready():
            dispatch(*waiting.popleft())

    def receive(line, received):
        nonlocal initialized
        rid = None
        try:
            if len(line) > MAX_MESSAGE:
                raise ProtocolError(-32600, "Message exceeds 128 KB; connection closed.")
            request = json.loads(line.decode("utf-8"), object_pairs_hook=pairs)
            if not isinstance(request, dict):
                request = {}
            method = request.get("method")
            if type(request.get("id")) in (str, int):
                rid = request["id"]
            if (
                method == DRAIN_METHOD or rid in pending
                or isinstance(rid, str) and rid.startswith(DRAIN_PREFIX)
            ):
                rid = None
                raise ProtocolError(-32600, "Request ID unavailable.")
            if request.get("jsonrpc") == "2.0" and method == "notifications/cancelled" and "id" not in request:
                params = request.get("params")
                target = params.get("requestId") if isinstance(params, dict) else None
                if type(target) not in (str, int) or target not in pending:
                    return
                _, owner = pending[target]
                if owner is None:
                    for item in waiting:
                        if item[1] == target:
                            waiting.remove(item)
                            break
                    pending.pop(target)
                    write_response(output, _response(target, {
                        "code": -32800, "message": "request_cancelled",
                        "data": {"stage": "queue"},
                    }))
                elif owner is active:
                    dispatch(line, None, method)
                return
            if len(pending) >= max_pending and rid is not None:
                raise ProtocolError(-32000, "MCP request queue full; retry after active work completes.")
            if (
                request.get("jsonrpc") == "2.0"
                and (protocol is not None or any(value[0] == "initialize" for value in pending.values()))
                and method == "notifications/initialized" and "id" not in request
                and not set(request) - {"jsonrpc", "method", "params"}
                and (not isinstance(request.get("params"), dict)
                     or isinstance(request["params"].get("_meta", {}), dict))
            ):
                initialized = True
            deadline = received + request_timeout
            if rid is not None and deadline <= time.monotonic():
                write_response(output, _response(rid, QUEUE_DEADLINE_ERROR))
                return
            if active is None or active.contract is None or drain_id is not None or not binding_ready():
                if len(waiting) >= max_pending:
                    if rid is None:
                        return
                    raise ProtocolError(-32000, "MCP request queue full; retry after active work completes.")
                waiting.append((line, rid, method, deadline))
                if rid is not None:
                    pending[rid] = (method, None)
            else:
                dispatch(line, rid, method, deadline)
        except ProtocolError as error:
            write_response(output, _response(rid, {"code": error.code, "message": error.message}))
        except (ValueError, UnicodeError):
            write_response(output, _response(rid, {
                "code": -32700, "message": "Invalid JSON message.",
            }))

    try:
        if not parked:
            active = start(initial, None)
        threading.Thread(
            target=_pump,
            args=(source, events, host),
            kwargs={"limit": MAX_MESSAGE + 1, "slots": host_slots, "stop": halt},
            daemon=True,
            name="agentbrain-host-input",
        ).start()
        threading.Thread(target=watch, daemon=True, name="agentbrain-release-observer").start()
        while True:
            if input_closed and not pending and not waiting:
                return 2 if oversized else 0
            if active is None and candidate is None and not parked:
                for rid in pending:
                    write_response(output, _response(rid, {
                        "code": -32003, "message": "MCP worker unavailable before dispatch.",
                        "data": {"outcome": "not-dispatched"},
                    }))
                return 2
            now = time.monotonic()
            expire_waiting(now)
            if active is not None and active.contract is None and now >= active.deadline:
                worker_lost()
            if candidate is not None and candidate.contract is None and now >= candidate.deadline:
                close_candidate("worker-readiness-timeout")
                flush_waiting()
            if parked and waiting and binding_ready():
                # The earlier worker has already exited. On wake, a newly
                # verified compatible worker may use the existing host session.
                target = latest if latest == last_good or registration_current() else last_good
                if (
                    not target
                    or target.fingerprint == rejected
                    or target.cache_abi != last_good.cache_abi
                ):
                    target = last_good
                try:
                    active = start(target, protocol)
                except (OSError, RuntimeError):
                    report("worker-start-failed")
                    if target != last_good:
                        rejected = target.fingerprint
                        try:
                            active = start(last_good, protocol)
                        except (OSError, RuntimeError):
                            report("worker-restart-failed")
                else:
                    restart_attempted = False
                parked = False
            if drain_id is not None and drain_acked and idle_timeout is not None:
                if not any(owner is active for _, owner in pending.values()):
                    if serial_target is not None and not registration_current():
                        rejected = serial_target.fingerprint
                        serial_target = None
                        drain_id = None
                        report("native-registration-changed")
                        flush_waiting()
                    else:
                        previous = active
                        last_good = previous.spec
                        previous.finish()
                        active = None
                        drain_id = None
                        parked = True
                        serial_target = None
            elif drain_id is not None and drain_acked:
                if candidate.process.poll() is not None:
                    close_candidate("candidate-exited")
                    flush_waiting()
                elif not any(owner is active for _, owner in pending.values()):
                    if registration_current():
                        previous, active, candidate = active, candidate, None
                        publish(active)
                        last_good = previous.spec
                        restart_attempted = False
                        drain_id = None
                        previous.finish()
                    else:
                        close_candidate("native-registration-changed")
                    flush_waiting()
            if drain_id is not None and now >= drain_deadline:
                if idle_timeout is None:
                    close_candidate("worker-drain-timeout")
                else:
                    if serial_target is not None:
                        rejected = serial_target.fingerprint
                    serial_target = None
                    drain_id = None
                    last_activity = now
                    report("worker-drain-timeout")
                flush_waiting()
            if (
                idle_timeout is not None and not input_closed and active is not None
                and active.contract is not None and initialized and protocol is not None
                and candidate is None and drain_id is None and not pending and not waiting
                and now - last_activity >= idle_timeout
            ):
                serial_target = None
                begin_drain(now)
            if (
                not input_closed and active is not None and active.contract is not None
                and initialized and protocol is not None and candidate is None and drain_id is None
                and latest and latest.fingerprint not in (active.spec.fingerprint, rejected)
            ):
                if active.spec.cache_abi != latest.cache_abi:
                    rejected = latest.fingerprint
                    report("cache-contract-changed")
                elif idle_timeout is not None:
                    serial_target = latest
                    begin_drain(now)
                else:
                    try:
                        candidate = start(latest, protocol)
                    except (OSError, RuntimeError):
                        rejected = latest.fingerprint
                        report("worker-start-failed")
            if binding_observed is not None:
                flush_waiting()
            try:
                owner, line, received = events.get(timeout=0.1)
            except Empty:
                continue
            if owner is host:
                host_slots.release()
                if line is not None:
                    last_activity = time.monotonic()
                    oversized = len(line) > MAX_MESSAGE
                    if not oversized and not line.endswith(b"\n") and len(line) < MAX_MESSAGE:
                        line += b"\n"
                    receive(line, received)
                if line is None or oversized:
                    input_closed = True
                    if active is not None and candidate is not None:
                        close_candidate("host-input-closed")
                        flush_waiting()
                    if active is not None and not waiting:
                        active.close_input()
            elif owner is release:
                latest = line
                if active is not None and active.contract is not None:
                    publish(active)
                if serial_target is not None and serial_target != latest:
                    drain_id = None
                    serial_target = None
                    flush_waiting()
                if active is not None and candidate is not None and candidate.spec != latest:
                    close_candidate("candidate-superseded")
                    flush_waiting()
            elif owner is active or owner is candidate:
                if line is None:
                    if owner is candidate:
                        close_candidate("candidate-exited")
                        flush_waiting()
                    else:
                        worker_lost()
                    continue
                if owner.contract is None:
                    try:
                        owner.contract = _read_contract(json.loads(line, object_pairs_hook=pairs))
                        if owner.contract["cacheAbi"] != owner.spec.cache_abi:
                            raise ValueError("MCP worker cache ABI changed")
                        if catalog is not None and not _compatible_session(
                            catalog, owner.contract, protocol,
                        ):
                            raise ValueError("MCP catalog changed")
                        if (
                            idle_timeout is not None and can_switch is not None
                            and not registration_current()
                        ):
                            report("native-registration-changed")
                            raise ValueError("MCP native binding changed during readiness")
                    except (AttributeError, KeyError, ProtocolError, TypeError, ValueError):
                        if owner is candidate:
                            close_candidate("worker-readiness-rejected")
                            flush_waiting()
                        else:
                            report("worker-readiness-rejected")
                            worker_lost()
                        continue
                    if owner is active or active is None:
                        active, candidate = owner, None
                        catalog = owner.contract if catalog is None else catalog
                        publish(owner)
                        if remember_worker is not None:
                            last_good = owner.spec
                            remember_worker(owner.spec, owner.command)
                        flush_waiting()
                        if input_closed and not waiting:
                            active.close_input()
                    else:
                        begin_drain(time.monotonic())
                    continue
                # A ready candidate is idle until the barrier on the old worker.
                if owner is candidate:
                    close_candidate("unexpected-candidate-output")
                    flush_waiting()
                    continue
                try:
                    response = json.loads(line)
                    rid = response["id"]
                    if rid is not None and type(rid) not in (str, int):
                        raise ValueError("Unexpected worker response ID")
                except (KeyError, TypeError, ValueError):
                    worker_lost()
                    continue
                if isinstance(rid, str) and rid.startswith(DRAIN_PREFIX):
                    if rid != drain_id:
                        continue
                    if response.get("result") == {}:
                        drain_acked = True
                    else:
                        if idle_timeout is None:
                            close_candidate("worker-drain-failed")
                        else:
                            if serial_target is not None:
                                rejected = serial_target.fingerprint
                            serial_target = None
                            drain_id = None
                            last_activity = time.monotonic()
                            report("worker-drain-failed")
                        flush_waiting()
                    continue
                last_activity = time.monotonic()
                method, _ = pending.pop(rid, (None, None))
                result = response.get("result")
                if method == "initialize" and isinstance(result, dict):
                    capabilities = result["capabilities"]
                    caps = dict(capabilities.get("tools", {}), listChanged=True)
                    response["result"] = dict(result, capabilities=dict(capabilities, tools=caps))
                    negotiated = result.get("protocolVersion")
                    if negotiated in catalog["versions"]:
                        protocol = negotiated
                if method == "tools/list" and isinstance(result, dict) and "tools" in result:
                    if not binding_ready():
                        write_response(output, _response(rid, {
                            "code": -32003, "message": "MCP registration or release changed; retry after verification."
                        }))
                        continue
                    response["result"] = dict(result, tools=list(published_tools.values()))
                write_response(output, response)
    finally:
        halt.set()
        if candidate is not None:
            candidate.finish(abort=True)
        if active is not None:
            active.finish()


if __name__ == "__main__":
    sys.exit(main())

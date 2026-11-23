"""Detached shared delivery, supervised while the ordinary MCP host is alive."""

from contextlib import ExitStack
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from agent_brain_client.adapters.platforms.filesystem import cache_directory, cache_locked
from agent_brain_client.adapters.storage import metadata
from agent_brain_client.domain.scopes import digest
from agent_brain_client.adapters.platforms.filesystem import LockTimeout


def directory(home):
    return Path(home) / '.config/agent-brain-client/global-memory/delivery'


def register_task(home, key, task, *, wake_worker=True):
    root = directory(home)
    cache_directory(root)
    # The worker holds this same short lock through its idle decision and lease
    # release. A new durable task can therefore never disappear into that gap.
    with cache_locked(root / 'spawn.lock', timeout=20):
        metadata.write(root / (key + '.json'), task, cache=True)
    return wake(home) if wake_worker else False


def schedule_delivery(home, host_adapter, origin):
    if host_adapter not in ('codex', 'codebuddy'):
        raise ValueError('Known Harness required')
    root = directory(home)
    cache_directory(root)
    key = digest([host_adapter, origin])
    return register_task(home, key, {
        'schema': 1, 'harness': host_adapter, 'origin': origin,
    })


def schedule_project(home, settings_path, settings_sha256, *, wake_worker=True, verified_release=False):
    from agent_brain_client.runtime.project_memory import load

    value = load(settings_path, settings_sha256, verified_release=verified_release)
    if Path(value['device_home']) != Path(home):
        raise ValueError('Project delivery device changed')
    root = directory(home)
    cache_directory(root)
    task = {'schema': 1, 'harness': 'codex-project', 'settings': str(settings_path),
            'settings_sha256': settings_sha256}
    return register_task(home, digest(task), task, wake_worker=wake_worker)


def schedule_assistant_completion(home, turn_id):
    if not isinstance(turn_id, str) or not re.fullmatch('[a-f0-9]{64}', turn_id):
        raise ValueError('Observed turn identity required')
    root = directory(home)
    cache_directory(root)
    task = {'schema': 1, 'harness': 'codebuddy-final', 'turn_id': turn_id}
    return register_task(home, digest(task), task)


def schedule_project_final(home, turn_id, settings, pin):
    if not re.fullmatch('[a-f0-9]{64}', turn_id):
        raise ValueError('Observed project turn required')
    task = {'schema': 1, 'harness': 'codebuddy-project-final', 'turn_id': turn_id,
            'settings': str(settings), 'settings_sha256': pin}
    return register_task(home, digest(task), task)


def wake(home):
    """Short startup lease suppresses spawn storms; worker owns its own lease."""
    root = directory(home)
    if not root.exists():
        return False
    try:
        with cache_locked(root / 'spawn.lock', timeout=0):
            status = root / 'status.json'
            completed_pass = False
            if status.exists():
                state = metadata.private_json(status, cache=True)
                completed_pass = state.get('status') == 'idle'
                tasks = [p for p in root.glob('*.json')
                         if p.name not in ('status.json', 'spawn.json')]
                if state.get('status') == 'idle' and all(
                        p.stat().st_mtime < state.get('scan_started', 0) for p in tasks):
                    return False
            try:
                with cache_locked(root / 'worker.lock', timeout=0):
                    pass
            except LockTimeout:
                return False
            stamp = root / 'spawn.json'
            if stamp.exists() and not completed_pass:
                saved = metadata.private_json(stamp, cache=True)
                if 0 <= time.time() - saved.get('at', 0) < 10:
                    return False
            entry = Path(__file__).resolve().parents[1] / 'interfaces/cli/delivery.py'
            options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, close_fds=True)
            if sys.platform == 'win32':
                options['creationflags'] = (subprocess.DETACHED_PROCESS
                                            | subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                options['start_new_session'] = True
            subprocess.Popen([sys.executable, '-I', '-B', str(entry),
                              '--device-home', str(home)], **options)
            metadata.write(stamp, {'at': time.time()}, cache=True)
            metadata.write(status, {'status': 'starting', 'at': time.time()}, cache=True)
            return True
    except LockTimeout:
        return False


def deliver_origin(home, task, core):
    from agent_brain_client.domain.scopes import select, namespace, source_id

    if (set(task) != {'schema', 'harness', 'origin'} or task['schema'] != 1
            or task['harness'] not in ('codex', 'codebuddy')):
        raise ValueError('Bound delivery task required')
    value = core.load(home)
    local = task['origin']
    from agent_brain_client.adapters.storage.project_settings import source_active
    if not source_active(home, local['cwd'], task['harness']):
        return True
    if not core.approved(value) or select(value['scope'], local) is None:
        return True  # Suspended, not empty: the supervisor rechecks without network.
    client, queue = core.build_delivery_queue(value, local, task['harness'])
    pending_capture = False
    if task['harness'] == 'codex':
        from agent_brain_client.adapters.harnesses.codex.hooks import resume_completions
        from agent_brain_client.adapters.storage.observed_migration import migrate_staged_turns

        selected = select(value['scope'], local)
        project_id = namespace(client.actor, source_id(value['scope'], local, selected['sharing']))
        migrate_staged_turns(queue, origin=local, project_id=project_id)
        pending_capture = resume_completions(
            queue, lambda payload: core.submit_hook(value, local, payload),
            allowed=lambda scope: scope['project_id'] == project_id and scope['cwd'] == local['cwd']
            and source_active(home, local['cwd'], 'codex') and core.legacy(value, local) is None,
        )
    scope = digest(value['scope'])

    def current_client():
        current = core.load(home)
        if (not source_active(home, local['cwd'], task['harness'])
                or not core.approved(current) or digest(current['scope']) != scope
                or select(current['scope'], local) is None):
            return None
        fresh, _ = core.build_delivery_queue(current, local, task['harness'])
        return fresh if fresh.queue_identity == client.queue_identity else None

    def allowed(session):
        fresh = current_client()
        return fresh is not None and fresh.status(session).get('source_admitted') is True

    return deliver_with_cursor(
        home, [task['harness'], local], queue, allowed,
        local_precheck=lambda: current_client() is not None,
    ) or pending_capture


def deliver_with_cursor(home, identity, queue, allowed, allowed_event=lambda event: True,
                        *, local_precheck=None):
    from agent_brain_client.application.queue.delivery import deliver_pending

    progress = directory(home) / 'progress'
    cache_directory(progress)
    cursor = progress / (digest(identity) + '.json')
    after = metadata.private_json(cursor, cache=True).get('after', '') if cursor.exists() else ''
    return deliver_pending(queue, allowed=allowed, allowed_event=allowed_event, after=after,
                           local_precheck=local_precheck,
                           attempted=lambda key: metadata.write(
                               cursor, {'after': key}, cache=True))


def run(home, *, max_seconds=600, pause=time.sleep):
    from agent_brain_client.application.control import RequestControl
    from agent_brain_client.runtime.host_binding import load_delivery_core

    root = directory(home)
    cache_directory(root)
    core = None
    binding_error = None

    def project_release():
        root = Path(__file__).resolve().parents[3]
        if not (root / "verified.json").is_file():
            return False
        verified = shared_core()  # Also checks this process is the enrolled serving package.
        return verified is not None

    def shared_core():
        nonlocal core, binding_error
        if binding_error is not None:
            raise binding_error.with_traceback(None)
        if core is None:
            try:
                core = load_delivery_core(home)
            except Exception as error:
                binding_error = error
                raise
        return core

    try:
        with ExitStack() as lease:
            lease.enter_context(cache_locked(root / 'worker.lock', timeout=0))
            end = time.monotonic() + max_seconds
            delay = 1
            while time.monotonic() < end:
                scan_started = time.time()
                pending = False
                # Retry a failed binding after backoff, without repeating it for
                # each task or blocking independently pinned project work.
                binding_error = None
                for file in sorted(root.glob('*.json')):
                    if file.name in ('spawn.json', 'status.json'):
                        continue
                    control = RequestControl(min(30, max(0.1, end - time.monotonic())))
                    try:
                        with control.activate():
                            task = metadata.private_json(file, cache=True)
                            if task.get('harness') == 'codebuddy-final':
                                from agent_brain_client.adapters.harnesses.codebuddy.events import resume_observed_assistant

                                if (set(task) != {'schema', 'harness', 'turn_id'}
                                        or task['schema'] != 1
                                        or not re.fullmatch('[a-f0-9]{64}', task['turn_id'])):
                                    raise ValueError('Observed completion task required')
                                terminal = root / 'review' / (digest(task) + '.json')
                                if terminal.exists():
                                    continue
                                current = shared_core()
                                try:
                                    pending = resume_observed_assistant(home, current, task['turn_id']) or pending
                                except ValueError as error:
                                    cache_directory(terminal.parent)
                                    metadata.write(terminal, {
                                        'status': 'review-required', 'task_sha256': digest(task),
                                        'error_type': type(error).__name__,
                                    }, cache=True)
                            elif task.get('harness') == 'codebuddy-project-final':
                                from agent_brain_client.runtime.project_memory import finish_codebuddy_project
                                if (set(task) != {'schema', 'harness', 'turn_id', 'settings', 'settings_sha256'}
                                        or task['schema'] != 1 or not re.fullmatch('[a-f0-9]{64}', task['turn_id'])):
                                    raise ValueError('Observed project completion task required')
                                pending = finish_codebuddy_project(home, task, verified_release=project_release()) or pending
                            elif task.get('harness') == 'onboarding':
                                from agent_brain_client.runtime.onboarding import deliver_approved_batch

                                batch_pending, advanced = deliver_approved_batch(home, task, shared_core())
                                pending = pending or batch_pending
                                if not batch_pending:
                                    file.unlink(missing_ok=True)
                                elif advanced:
                                    # The deadline bounds idle/retrying workers, not real
                                    # progress in a user's already-approved long batch.
                                    end = max(end, time.monotonic() + max_seconds)
                            elif task.get('harness') == 'codex-project':
                                from agent_brain_client.runtime.project_memory import capture, delivery_port, load

                                if set(task) != {'schema', 'harness', 'settings', 'settings_sha256'} or task['schema'] != 1:
                                    raise ValueError('Pinned project delivery required')
                                released = project_release()
                                project = load(task['settings'], task['settings_sha256'], verified_release=released)
                                if Path(project['device_home']) != Path(home):
                                    raise ValueError('Project device changed')
                                queue, allowed, allowed_event = delivery_port(task['settings'], task['settings_sha256'], verified_release=released)
                                if project['harness'] == 'codex':
                                    from agent_brain_client.adapters.harnesses.codex.hooks import resume_completions

                                    pending = resume_completions(
                                        queue, lambda payload: capture(project, payload), allowed=allowed_event,
                                    ) or pending
                                pending = deliver_with_cursor(home, task, queue, allowed, allowed_event) or pending
                            else:
                                pending = deliver_origin(home, task, shared_core()) or pending
                    except Exception as error:
                        pending = True
                        metadata.write(root / 'status.json', {
                            'at': time.time(), 'status': 'pending',
                            'error_type': type(error).__name__,
                        }, cache=True)
                    finally:
                        control.close()
                    if time.monotonic() >= end:
                        break
                if not pending:
                    with cache_locked(root / 'spawn.lock', timeout=1):
                        changed = any(p.stat().st_mtime >= scan_started
                                      for p in root.glob('*.json')
                                      if p.name not in ('spawn.json', 'status.json'))
                        if changed:
                            continue
                        metadata.write(root / 'status.json', {
                            'status': 'idle', 'scan_started': scan_started,
                        }, cache=True)
                        # Release ownership before a simultaneous wake may probe it.
                        lease.close()
                        return
                pause(min(delay, max(0, end - time.monotonic())))
                delay = min(delay * 2, 30)
    except LockTimeout:
        return


def _has_work(cache):
    """Inspect only owned durable observations; never host transcripts."""
    cache = Path(cache)
    staging = (cache / 'observed-staging', cache / 'codex-completions')
    for parent in (cache, *staging, cache / 'outbox'):
        if parent.is_symlink():
            raise ValueError('Owned observation directory required')
    if any(file for directory in staging for file in directory.glob('*.json')):
        return True
    for path in (cache / 'outbox').glob('*.json'):
        if metadata.private_json(path, cache=True).get('state') == 'pending':
            return True
    return False


def discover_existing(home):
    """Recover observed origin registrations, not host history or arbitrary files."""
    base = Path(home) / '.config/agent-brain-client'
    root = directory(home)
    for harness, outboxes in (('codex', base / 'global-memory/outboxes'),
                              ('codebuddy', base / 'codebuddy/outboxes')):
        if not outboxes.exists():
            continue
        for origin_file in sorted(outboxes.glob('*/origin.json')):
            try:
                local = metadata.private_json(origin_file, cache=True)
                if origin_file.parent.name != digest(local):
                    raise ValueError('Registered outbox origin changed')
                cache_directory(root)
                task_file = root / (digest([harness, local]) + '.json')
                if not task_file.exists() or _has_work(origin_file.parent):
                    metadata.write(task_file, {'schema': 1, 'harness': harness,
                                               'origin': local}, cache=True)
            except Exception:
                continue  # One invalid registration must not starve other observed sources.

    # A project task is pinned before staging. Refresh it from actual queue
    # state even if the hook died before its normal post-stage wake.
    for task_file in root.glob('*.json'):
        if task_file.name in ('status.json', 'spawn.json'):
            continue
        try:
            task = metadata.private_json(task_file, cache=True)
            if task.get('harness') != 'codex-project':
                continue
            from agent_brain_client.runtime.project_memory import load

            project = load(task['settings'], task['settings_sha256'])
            if Path(project['device_home']) == Path(home) and _has_work(project['cache']):
                metadata.write(task_file, task, cache=True)
        except Exception:
            continue

    turns = base / 'codebuddy/turns'
    if turns.exists():
        for marker in sorted(turns.glob('*.json')):
            try:
                if not re.fullmatch('[a-f0-9]{64}', marker.stem):
                    continue
                row = metadata.private_json(marker, cache=True)
                if not row.get('stopped') or row.get('complete') or row.get('excluded'):
                    continue
                if row.get('project_delivery'):
                    task = {'schema': 1, 'harness': 'codebuddy-project-final', 'turn_id': marker.stem, **row['project_delivery']}
                else:
                    task = {'schema': 1, 'harness': 'codebuddy-final', 'turn_id': marker.stem}
                cache_directory(root)
                task_file = root / (digest(task) + '.json')
                if not task_file.exists():
                    metadata.write(task_file, task, cache=True)
            except Exception:
                continue  # Preserve invalid evidence without blocking valid completion tasks.


def supervise(home, *, start=None):
    """Recover after worker failure without another user message or OS service."""
    stop = threading.Event()

    def watch():
        if start is not None:
            while not stop.is_set() and not start.wait(0.1):
                pass
        while not stop.is_set():
            try:
                discover_existing(home)
                wake(home)
            except Exception:
                pass  # Durable tasks survive; retry on the next supervised tick.
            stop.wait(15)

    thread = threading.Thread(target=watch, daemon=True, name='agentbrain-delivery')
    thread.start()
    return stop

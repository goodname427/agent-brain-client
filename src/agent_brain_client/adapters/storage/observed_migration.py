"""Migrate already observed staged turns, without importing host history."""

from agent_brain_client.application.memory.observations import stage_messages
from agent_brain_client.domain.turns import normalized_turn
from agent_brain_client.domain import privacy
from agent_brain_client.domain.scopes import restricted


def migrate_staged_turns(queue, *, origin, project_id=None, project_ids=None, excluded_directory=None):
    allowed_projects = set(project_ids or ())
    if project_id is not None:
        allowed_projects.add(project_id)
    if not allowed_projects:
        raise ValueError("Explicit migration projects required")
    directory = queue.cache / 'hook-turns'
    if not directory.exists():
        return
    with queue._outbox_lock(queue.cache / 'event-hook.lock', 'capture-migration'):
        for file in sorted(directory.glob('*.json')):
            try:
                row = queue._load('hook-turns/' + file.name)
                if (not isinstance(row, dict) or set(row) != {
                        'schema', 'scope', 'turn_id', 'prompt', 'occurred_at', 'final'
                        } or row['schema'] != 1):
                    raise ValueError('Original observed staging required')
                scope = row['scope']
                if scope.get('cwd') != origin['cwd'] or scope.get('project_id') not in allowed_projects:
                    raise ValueError('Original observed scope changed')
                key = normalized_turn(scope, row['turn_id'], 'prompt', row['prompt'])
                if file.name != key + '.json':
                    raise ValueError('Original observed identity changed')
                if queue._load('outbox/agentbrain_event_' + key + '.json') is not None:
                    continue  # Original paired requests remain authoritative, byte-for-byte.
                if ((queue.cache / ('excluded-' + key + '.json')).exists()
                        or excluded_directory is not None
                        and (excluded_directory / ('excluded-' + key + '.json')).exists()):
                    continue
                texts = [row['prompt']] + ([row['final']] if row['final'] is not None else [])
                if any(privacy.private_content(text) or restricted(text) for text in texts):
                    continue
                stage_messages(queue, project_id=scope['project_id'],
                               session_id=scope['session_id'], operation_id=row['turn_id'],
                               phase='prompt', messages=[{'role': 'user', 'content': row['prompt']}],
                               occurred_at=row['occurred_at'])
                if row['final'] is not None:
                    normalized_turn(scope, row['turn_id'], 'final', row['final'])
                    stage_messages(queue, project_id=scope['project_id'],
                                   session_id=scope['session_id'], operation_id=row['turn_id'],
                                   phase='assistant', messages=[{'role': 'assistant', 'content': row['final']}],
                                   occurred_at=row['occurred_at'])
            except (ValueError, OSError, KeyError, TypeError):
                continue  # Preserve a rejected old row without starving current delivery.

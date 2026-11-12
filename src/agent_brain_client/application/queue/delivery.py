"""Deliver native observations using their existing immutable outbox receipts."""


from agent_brain_client.domain import privacy
from agent_brain_client.domain.scopes import restricted


def deliverable(row):
    return (row['state'] == 'pending'
            and row['request']['method'] == 'memory_event_submit'
            and not any(privacy.private_content(message['content']) or restricted(message['content'])
                        for message in row['request']['params']['event'].get('messages', [])))


def deliver_pending(queue, *, allowed, limit=16, after="", attempted=lambda key: None, allowed_event=lambda event: True):
    """A bounded fair pass; an unknown response keeps its original request ID."""
    from agent_brain_client.application.memory.observations import resume_staging

    staging = resume_staging(queue)
    rows = [row for row in queue._queued() if deliverable(row)]
    rows.sort(key=lambda row: (row["request"]["request_id"] <= after,
                               row["request"]["request_id"]))
    for row in rows[:limit]:
        request = row['request']
        session = request['params']['event']['session_id']
        attempted(request["request_id"])
        event = request['params']['event']
        if not allowed(session) or not allowed_event(event):
            break
        queue.invoke(request, event_gate=lambda: allowed(session) and allowed_event(event))
    return staging or any(deliverable(row) for row in queue._queued())

"""Deliver native observations using their existing immutable outbox receipts."""


from agent_brain_client.domain import privacy
from agent_brain_client.domain.events import NATIVE_MEMORY_CONTRACT
from agent_brain_client.domain.scopes import restricted


def deliverable(row):
    # Originals use the same outbox, with explicit replay through the local
    # approval ledger. Hook delivery has no authority to replay an original.
    return (row['state'] == 'pending'
            and row['request']['method'] == 'memory_event_submit'
            and row['request']['params']['event'].get('contract') != NATIVE_MEMORY_CONTRACT
            and not any(privacy.private_content(message['content']) or restricted(message['content'])
                        for message in row['request']['params']['event'].get('messages', [])))


def deliver_pending(queue, *, allowed, limit=16, after="", attempted=lambda key: None,
                    allowed_event=lambda event: True, local_precheck=None):
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
        ready = allowed(session) if local_precheck is None else local_precheck()
        if not ready or not allowed_event(event):
            break
        admitted = True

        def event_gate():
            nonlocal admitted
            admitted = False
            admitted = allowed(session) and allowed_event(event)
            return admitted

        queue.invoke(request, event_gate=event_gate)
        if local_precheck is not None and not admitted:
            break
    return staging or any(deliverable(row) for row in queue._queued())

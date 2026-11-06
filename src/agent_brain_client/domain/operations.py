"""domain / operations: one owner for each contract and rule."""

FIELDS = {
    "memory_status": {"project_id", "session_id"},
    "memory_resume": {"project_id", "session_id"},
    "memory_stop": {"project_id", "session_id"},
    "memory_revoke": {"project_id", "session_id"},
    "memory_sync": {"project_id", "session_id"},
    "memory_query": {"kind", "query", "since", "until", "limit"},
    "content_read": {"kind", "id"},
    "content_catalog": {"kinds"},
    "memory_event_submit": {"kind", "id", "event"},
    "content_put": {"kind", "id", "record", "expected_revision"},
    "content_remove": {"kind", "id", "expected_revision"},
    "content_set_enabled": {"kind", "id", "enabled", "expected_revision"},
}


WRITES = {"content_set_enabled", "content_put", "content_remove", "memory_event_submit"}


HOOK_METHODS = {"memory_event_submit"} | {
    "memory_status",
    "memory_resume",
    "memory_stop",
    "memory_revoke",
    "memory_sync",
}


SCOPES = {"read", "put", "write", "remove"}

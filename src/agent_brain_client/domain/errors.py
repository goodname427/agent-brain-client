"""Safe content-state errors; callers retain the original state on failure."""


class CacheBlocked(Exception):
    pass


class CollectionCapacityExceeded(CacheBlocked):
    pass


class InstallBlocked(ValueError):
    pass


class EventDiagnosticsUnavailable(ValueError):
    """The authenticated response does not implement event diagnostics."""

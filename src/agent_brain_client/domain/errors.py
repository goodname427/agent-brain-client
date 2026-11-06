"""Safe content-state errors; callers retain the original state on failure."""


class CacheBlocked(Exception):
    pass


class CollectionCapacityExceeded(CacheBlocked):
    pass


class InstallBlocked(ValueError):
    pass

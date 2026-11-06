"""One active owner per required Harness lifecycle capability."""

CAPABILITIES = ("session_start", "user_prompt", "assistant_final")


def fallback_plan(hooks, *, rule_enabled=False):
    """Use working Hooks first, with explicit opt-in for missing capabilities.

    Coverage is supplied by the host. This pure decision neither discovers nor
    enables a Hook, Rule, source or service.
    """
    if (
        not isinstance(hooks, dict)
        or set(hooks) != set(CAPABILITIES)
        or any(type(value) is not bool for value in hooks.values())
        or type(rule_enabled) is not bool
    ):
        raise ValueError("Explicit Hook coverage and Rule opt-in required")
    return {
        name: "hook" if hooks[name] else "rule" if rule_enabled else "unavailable"
        for name in CAPABILITIES
    }

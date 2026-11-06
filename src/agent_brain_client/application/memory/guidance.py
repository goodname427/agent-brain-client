"""application / memory / guidance: one owner for each contract and rule."""

HOST_GUIDANCE = (
    "AgentBrain is the shared memory service for authorized durable information. Keep Harness-native memory intact and synchronize approved useful items with AgentBrain tools. "
    "Use agent-brain-memory for daily memory work and agent-brain-onboard for authorized source onboarding; report when a relevant Skill is unavailable. "
)


MEMORY_GUIDANCE = (
    "For daily memory decisions, follow the currently loaded agent-brain-memory Skill; for source import and coverage, follow agent-brain-onboard. This Client text defines interface and authorization constraints, not a second behavior policy. "
    "Use only content methods and IDs within the granted scope. Do not infer source access or permission from catalogs, prior imports or local mappings; never access prohibited sessions/databases, secrets or LocalOnly material. "
    "Writes require the current expected_revision and a stable request_id. A local-confirmed receipt with local_durable=true, github_confirmed=false and durable=false confirms the Server saved the write, but not its GitHub backup. Report this as saved; pending backup with automatic_retry.state=waiting is a separate background task, not a failed save, and does not require polling or another write. Preserve and report attention_required=true, paused/review-required retries or actual errors when present. Read-only durable=false describes a read, not an unsaved memory. Only remote-confirmed with durable=true confirms the GitHub backup; supported conflicts remain pending until reconciled. "
    "A mapping refresh does not prove an already running model reloaded its context. Importing a Rule or Skill does not enable it. "
)

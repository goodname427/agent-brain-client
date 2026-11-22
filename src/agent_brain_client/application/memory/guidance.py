"""application / memory / guidance: one owner for each contract and rule."""

HOST_GUIDANCE = (
    "AgentBrain is the shared memory service for authorized durable information. Keep Harness-native memory intact and synchronize approved useful items with AgentBrain tools. "
    "See the public Client guide for onboarding, memory and maintenance: https://github.com/goodname427/agent-brain-client/blob/main/BOOTSTRAP.md#onboarding. "
)


MEMORY_GUIDANCE = (
    "For first use and authorized source import, follow the public guide at https://github.com/goodname427/agent-brain-client/blob/main/BOOTSTRAP.md#onboarding. Use the memory guide at https://github.com/goodname427/agent-brain-client/blob/main/docs/reference/memory-behavior.md and the maintenance guide at https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/maintenance.md. These workflows do not require preloaded Skills or existing AgentBrain memory. "
    "Use only content methods and IDs within the granted scope. Do not infer source access or permission from catalogs, prior imports or local mappings; never access prohibited sessions/databases, secrets or LocalOnly material. "
    "Writes require the current expected_revision and a stable request_id. A local-confirmed receipt with local_durable=true, github_confirmed=false and durable=false confirms the Server saved the write, but not its GitHub backup. Report this as saved; pending backup with automatic_retry.state=waiting is a separate background task, not a failed save, and does not require polling or another write. Preserve and report attention_required=true, paused/review-required retries or actual errors when present. Read-only durable=false describes a read, not an unsaved memory. Only remote-confirmed with durable=true confirms the GitHub backup; supported conflicts remain pending until reconciled. "
    "A mapping refresh does not prove an already running model reloaded its context. Importing a Rule or Skill does not enable it. "
)

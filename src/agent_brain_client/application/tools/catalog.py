"""Single content-tool catalog, filtered by the enrolled grant and policy."""

from typing import Any
from agent_brain_client.domain import contract
from agent_brain_client.domain.content_policy import validate_policy


RECALL_EVIDENCE_GUIDANCE = (
    "This is the approved content service; synthetic sandbox tools are a separate test service and cannot prove production capture or recall. "
    "Use this currently connected approved-content service for Agent Brain personal memory. "
    "Do not route to agent_brain_synthetic_sandbox based on old memories, similar tool names, test wording or a disabled plugin still appearing in a session. "
    "A missing or failed approved service is a blocker, not permission to enable or fall back to another plugin. "
    "For an isolated offline/replay test, use a temporary Client fixture with a fictional transport; a sandbox health call cannot test the Client outbox. "
    "For a record from the current test or turn, match its source event/session, occurrence time or unique marker; an older matching topic or memory_updated_at alone is not evidence for this run. "
    "If current evidence is missing, report it as unconfirmed; do not substitute an older result. "
    "Hook core-accepted/committed confirms event receipt only. pipeline_state=unverified or model_extraction_verified=false does not confirm model extraction or searchable memory. "
)


def local_tools(kinds, memory_sources, layered_recall=False):
    tools = []
    tools.append(
        {
            "name": "content_queue_inspect",
            "description": "Inspect this Client's retained write queue without sending, retrying or changing requests. With no arguments, list a page of path-free metadata and exact blocking request IDs; use next_cursor for more. With request_id, read the exact original proposal, CAS base, reconciliation references, saved response and cached remote conflict snapshot. The snapshot may be stale: call content_read before an explicit reviewed merge through content_put with a new stable request_id and reconciles.request_ids. Unknown pending outcomes require the identical original request and ID; never replace, rebase or delete them. Terminal conflicts remain reviewable and do not block fresh CAS writes. Uncertain pending outcomes still require exact replay. No device or source discovery.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "pattern": "^[A-Za-z0-9_-]{16,128}$",
                    },
                    "cursor": {"type": "string", "maxLength": 96},
                },
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
            },
        }
    )
    if layered_recall:
        origin = {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["project", "non-project"]},
                "cwd": {"type": "string"},
                "project_root": {"type": ["string", "null"]},
                "repository_root": {"type": ["string", "null"]},
            },
            "required": ["kind", "cwd", "project_root", "repository_root"],
            "additionalProperties": False,
        }
        tools.append(
            {
                "name": "memory_query",
                "description": "The single memory query and automatic recall entry. Read shared L1/L2/L3 memory through the approved HTTPS Server using the existing device identity. Optional origin defaults to current directory metadata; optional session_id defaults to a distinct reader ID. Server source approval is required; this tool cannot enable capture, enroll a device, import old conversations or call a model. Returns layer-specific committed event provenance and source applicability. Direct and automatically extracted facts share L1 and the same L2/L3 derivation. Empty layers remain unconfirmed. "
                + RECALL_EVIDENCE_GUIDANCE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "origin": origin,
                        "session_id": {
                            "type": "string",
                            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
                        },
                        "query": {"type": "string", "minLength": 1, "maxLength": 1024},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "default": 5,
                            "description": "Native L1 maxResults per namespace; L2/L3 are assembled separately.",
                        },
                        "recall": {
                            "type": "object",
                            "properties": {
                                "strategy": {
                                    "type": "string",
                                    "enum": ["keyword", "embedding", "hybrid"],
                                },
                                "scoreThreshold": {"type": "number"},
                                "maxCharsPerMemory": {"type": "integer", "minimum": 0},
                                "maxTotalRecallChars": {
                                    "type": "integer",
                                    "minimum": 0,
                                },
                                "timeoutMs": {"type": "integer", "minimum": 1},
                            },
                            "additionalProperties": False,
                            "description": "Native recall settings. Zero character budgets are unlimited; unavailable embeddings use native FTS fallback.",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                },
            }
        )
    tools.append(
        {
            "name": "content_request_id",
            "description": "Generate a deterministic idempotent write request_id from the completed Harness turn digest, operation and AgentBrain target identity. Different targets/operations in one turn receive different IDs; retries of the same operation get the same ID. Local-only; no transfer.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "turn_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                    "method": {
                        "type": "string",
                        "enum": [
                            "content_put",
                            "content_remove",
                            "content_set_enabled",
                        ],
                    },
                    "kind": {"type": "string", "enum": kinds},
                    "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"},
                },
                "required": ["turn_sha256", "method", "kind", "id"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
            },
        }
    )
    if memory_sources:
        tools.append(
            {
                "name": "content_sources_inventory",
                "description": "Enumerate only the selected Harness persistent memories. Returns path-free IDs, file fingerprints and per-item processing status in pages of 64; never returns memory text or reads session/transcript files. Inventory is not semantic review or import coverage. Use the returned cursor for the next page; restart if it changes. Local-only read; no Server request or data transfer.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"cursor": {"type": "string", "maxLength": 96}},
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                },
            }
        )
        tools.append(
            {
                "name": "content_sources_read",
                "description": "Read the bounded body of exactly one previously inventoried selected Harness persistent Memory file, matching its ID and fingerprint. Local-only; never reads transcript/session paths and does not transfer data. Review the returned text for secrets, LocalOnly, customer/employer confidential data and cross-project scope before staging.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source_id": {
                            "type": "string",
                            "pattern": "^(codex|codebuddy)-memory:[a-f0-9]{64}$",
                        },
                        "source_fingerprint": {
                            "type": "string",
                            "pattern": "^[a-f0-9]{64}$",
                        },
                    },
                    "required": ["source_id", "source_fingerprint"],
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                },
            }
        )
        tools.append(
            {
                "name": "content_sources_review",
                "description": "Record an explicit review decision for a previously read exact source. This creates only a path-free status marker in the private cache, never stores text or transfers content. A shareable decision is required before any candidate from this source can be staged. Mark LocalOnly, secrets, or confidential sources excluded; mark unsafe project scope or uncertainty deferred. The legacy no-durable-user-fact reason is not a durability or importance threshold; do not use it to skip ordinary experiences, events, corrections or short-term progress.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source_id": {
                            "type": "string",
                            "pattern": "^(codex|codebuddy)-memory:[a-f0-9]{64}$",
                        },
                        "source_fingerprint": {
                            "type": "string",
                            "pattern": "^[a-f0-9]{64}$",
                        },
                        "decision": {
                            "type": "string",
                            "enum": ["shareable", "excluded", "deferred", "skipped"],
                        },
                        "reason_code": {
                            "type": "string",
                            "enum": [
                                "safe-cross-project-personal-summary",
                                "secret-localonly-or-confidential",
                                "project-scope-unavailable",
                                "uncertain-content",
                                "no-durable-user-fact",
                            ],
                        },
                    },
                    "required": [
                        "source_id",
                        "source_fingerprint",
                        "decision",
                        "reason_code",
                    ],
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                },
            }
        )
        tools.append(
            {
                "name": "content_sources_stage",
                "description": "Stage the final normalized AgentBrain Memory candidate for one exact source that was read locally and explicitly reviewed as shareable. Cover all authorized shareable sources; do not filter by importance or long-term durability. Include ordinary user experiences, events, decisions, corrections and short-term progress when they can be safely generalized to the shared personal scope. Before staging, call content_read for the target and finish deduplication, merging and corrections while preserving unique source and target information. The same source fingerprint is immutable once staged: repeat only the identical candidate; do not try to revise it in place. This call records a local import-plan entry only; it sends no Server write and is not a save. It returns source_key for local reconciliation. To persist this staged candidate, call content_put with the revision from content_read, a stable request_id and reconciles.source_keys containing that source_key. The reconciliation metadata stays local; content_put sends the record with its CAS revision through the existing outbox. Count it as saved only after the required Server persistence receipt; refresh is not required to perform the write. Never include secrets, LocalOnly, customer/employer confidential data or details unsafe to share across projects.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source_id": {
                            "type": "string",
                            "pattern": "^(codex|codebuddy)-memory:[a-f0-9]{64}$",
                        },
                        "source_fingerprint": {
                            "type": "string",
                            "pattern": "^[a-f0-9]{64}$",
                        },
                        "id": {
                            "type": "string",
                            "pattern": "^[a-z0-9][a-z0-9-]{0,63}$",
                        },
                        "record": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "maxLength": 1024},
                                "description": {"type": "string", "maxLength": 1024},
                                "body": {"type": "string"},
                                "type": {
                                    "type": "string",
                                    "enum": ["user", "feedback", "reference"],
                                },
                            },
                            "required": ["title", "description", "body", "type"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["source_id", "source_fingerprint", "id", "record"],
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                },
            }
        )
    return tools


LOCAL_TOOLS = frozenset(
    tool["name"]
    for tool in local_tools([], True, True)
    if tool["name"] != "memory_query"
)


def memory_sources_authorized(policy, grant):
    if policy is None and grant is None:
        return True  # isolated protocol self-check
    return "memory" in (policy or {}).get("kinds", []) and (
        grant is None or "memory" in grant.get("artifact_types", [])
    )


def tool_names(methods, memory_sources):
    names = set(methods) - contract.HOOK_METHODS
    names |= {
        tool["name"]
        for tool in local_tools(
            [], memory_sources, memory_sources and "memory_query" in methods
        )
    }
    if not set(methods) & {"content_put", "content_remove", "content_set_enabled"}:
        names.discard("content_queue_inspect")
    return sorted(names)


def enabled_tools(policy, grant):
    """One tool contract for plans, installation checks and the running MCP.

    All names remain bounded by existing read/put/remove and artifact grants.
    Local helpers grant no Server methods, future sources or directory access.
    """
    validate_policy(policy, grant)
    methods = contract.mcp_methods(grant["scopes"])
    if "memory" not in policy["kinds"]:
        methods -= {"memory_query"}
    if not set(policy["kinds"]) & {"rule", "skill"}:
        methods.discard("content_set_enabled")
    return tool_names(methods, memory_sources_authorized(policy, grant))


def tool_definitions(methods, kinds, memory_sources, client_version, update_guidance):
    """Build every content MCP declaration from the same authorized catalog."""
    tools = []
    declared = tool_names(methods, memory_sources)
    for name, fields in contract.FIELDS.items():
        if name not in declared or name == "memory_query":
            continue
        properties: dict[str, Any] = {field: {"type": "string"} for field in fields}
        if name == "content_catalog":
            properties["cursor"] = {
                "type": "string",
                "pattern": "^[a-f0-9]{64}:[0-9]{1,20}$",
                "description": "Use next_cursor from the preceding page; omit to restart.",
            }
        if "kinds" in fields:
            properties["kinds"] = {
                "type": "array",
                "items": {"type": "string", "enum": kinds},
                "minItems": 1,
                "uniqueItems": True,
            }
        if "kind" in fields:
            properties["kind"]["enum"] = kinds
        if name == "content_set_enabled":
            properties["kind"]["enum"] = [
                kind for kind in kinds if kind in ("rule", "skill")
            ]
            properties["enabled"] = {"type": "boolean"}
        if "record" in fields:
            properties["record"] = {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "maxLength": 1024},
                            "description": {
                                "type": "string",
                                "maxLength": 1024,
                            },
                            "body": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": ["user", "feedback", "reference"],
                            },
                        },
                        "required": ["title", "description", "body", "type"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "maxLength": 1024},
                            "body": {"type": "string"},
                            "enabled": {"type": "boolean"},
                            "trigger": {
                                "type": "object",
                                "properties": {
                                    "mode": {
                                        "type": "string",
                                        "enum": [
                                            "always",
                                            "on-demand",
                                            "conditional",
                                        ],
                                    },
                                    "patterns": {
                                        "type": "array",
                                        "items": {
                                            "type": "string",
                                            "maxLength": 256,
                                        },
                                        "maxItems": 32,
                                    },
                                },
                                "required": ["mode", "patterns"],
                                "additionalProperties": False,
                                "allOf": [
                                    {
                                        "if": {
                                            "properties": {
                                                "mode": {"const": "conditional"}
                                            },
                                            "required": ["mode"],
                                        },
                                        "then": {
                                            "properties": {"patterns": {"minItems": 1}}
                                        },
                                        "else": {
                                            "properties": {"patterns": {"maxItems": 0}}
                                        },
                                    }
                                ],
                            },
                        },
                        "required": ["title", "body", "enabled", "trigger"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "description": {
                                "type": "string",
                                "maxLength": 1024,
                            },
                            "enabled": {"type": "boolean"},
                            "files": {
                                "type": "object",
                                "properties": {"SKILL.md": {"type": "string"}},
                                "required": ["SKILL.md"],
                                "additionalProperties": {"type": "string"},
                                "minProperties": 1,
                                "maxProperties": 32,
                            },
                            "dependencies": {
                                "type": "object",
                                "properties": {
                                    "commands": {
                                        "type": "array",
                                        "items": {
                                            "type": "string",
                                            "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_.+-]{0,63}(?![\\s\\S])",
                                        },
                                        "uniqueItems": True,
                                        "maxItems": 32,
                                    },
                                    "python_modules": {
                                        "type": "array",
                                        "items": {
                                            "type": "string",
                                            "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_.+-]{0,63}(?![\\s\\S])",
                                        },
                                        "uniqueItems": True,
                                        "maxItems": 32,
                                    },
                                },
                                "required": ["commands", "python_modules"],
                                "additionalProperties": False,
                            },
                        },
                        "required": [
                            "description",
                            "enabled",
                            "files",
                            "dependencies",
                        ],
                        "additionalProperties": False,
                    },
                ],
                "description": "Typed v1 Memory, Rule, or Skill record. Server validates the selected record against kind.",
            }
        required = [] if name == "memory_query" else list(sorted(fields))
        if name in contract.WRITES:
            properties["request_id"] = {
                "type": "string",
                "pattern": "^[A-Za-z0-9_-]{16,128}$",
            }
            required.append("request_id")
        if name in ("content_put", "content_set_enabled"):
            properties["reconciles"] = {
                "type": "object",
                "description": "Local-only references to staged source plans or write conflicts; never sent to Server. Inspect retained proposals with content_queue_inspect and read the current target before merging. request_ids select only retained conflicts; recover pending outcomes by identical retry first. A still-inventoried source plan can be reconciled only by the exact staged record; a conflicted source plan requires an explicit merge of source and remote versions. Server persistence confirmation resolves selected conflicts.",
                "properties": {
                    "request_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "uniqueItems": True,
                        "maxItems": 256,
                    },
                    "source_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "uniqueItems": True,
                        "maxItems": 256,
                    },
                },
                "required": ["request_ids", "source_keys"],
                "additionalProperties": False,
            }
        if name == "content_set_enabled":
            properties["reconciles"]["properties"]["source_keys"]["maxItems"] = 0
            properties["reconciles"]["description"] = (
                "Optional explicit reconciliation of retained activation conflicts on this same target. Read the current target first; use a new request_id and current CAS revision. Source imports and ordinary edit conflicts cannot be resolved by activation."
            )
        if name == "content_put":
            properties["source_context"] = {
                "type": "object",
                "description": "Optional current Memory writing origin and actual host session_id. Directory metadata defaults locally; unavailable session is null, never guessed from the Server. Preserved unchanged on retries.",
                "properties": {
                    "origin": {"type": ["object", "null"]},
                    "session_id": {"type": ["string", "null"]},
                },
                "required": ["origin", "session_id"],
                "additionalProperties": False,
            }
        description = "Approved cloud content only. Server is the sole write boundary; retain CAS revision and request_id on write retries. Transport failures may return a verified stale snapshot with local_cache.stale=true; cache misses never prove remote absence. Local cache refresh does not reload existing model context. "
        if name == "content_set_enabled":
            description += "Only on an explicit user enable/disable request for this exact Rule/Skill. Changes enabled alone on an existing record; ordinary content_put edits must preserve enabled. Check local_refresh separately from persistence; a saved change may still require a local mapping retry. "
        if name == "content_put":
            description += "Preserve existing Rule/Skill enabled state; create disabled. Use content_set_enabled only for explicit user activation requests. "
        if name == "content_catalog":
            description += "AgentBrain destination records only; this does not discover Harness-native sources. Omit cursor for the first authorized inventory page, then follow next_cursor until absent. Use memory_query for retrieval. "
        if name in ("memory_query", "content_read", "content_catalog"):
            description += RECALL_EVIDENCE_GUIDANCE
        description += (
            "Running worker version: " + client_version + ". " + update_guidance
        )
        tools.append(
            {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": name not in contract.WRITES,
                    "destructiveHint": name == "content_remove",
                    "idempotentHint": True,
                },
            }
        )
    tools.extend(
        tool
        for tool in local_tools(
            kinds,
            memory_sources,
            "memory_query" in declared,
        )
        if tool["name"] in declared
    )
    return tools

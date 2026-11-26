"""Single content-tool catalog, filtered by the enrolled grant and policy."""

from typing import Any
from agent_brain_client.domain import contract
from agent_brain_client.application.memory.onboarding import INPUT_SCHEMA, TOOL_NAME
from agent_brain_client.domain.content import RULE_SKILL
from agent_brain_client.domain.content_policy import validate_policy
from agent_brain_client.domain.operations import CONTENT_WRITES, LIFECYCLE_WRITES, RULE_SKILL_WRITES


LIFECYCLE_TOOL = "content_lifecycle"
LIFECYCLE_ACTION_METHODS = {
    "archive": "content_archive",
    "restore": "content_restore",
    "remove": "content_remove",
}
LIFECYCLE_METHODS = frozenset(LIFECYCLE_ACTION_METHODS.values())


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


def local_tools(kinds, memory_sources, layered_recall=False, lifecycle_writes=()):
    tools = []
    tools.append(
        {
            "name": "content_queue_inspect",
            "description": "Inspect this Client's retained write queue without sending, retrying or changing requests. With no arguments, list a page of path-free metadata and exact blocking request IDs; use next_cursor for more. With request_id, read the exact original proposal, CAS base, reconciliation references, saved response and cached remote conflict snapshot. The snapshot may be stale: call content_read before an explicit reviewed merge through content_put with a new stable request_id and reconciles.request_ids. Unknown pending outcomes require the identical original request and ID; never replace, rebase or delete them. Terminal conflicts remain reviewable and do not block fresh CAS writes. To retry a retained content_set_enabled request, call content_put with operation=set_enabled and the original request_id, parameters and any reconciliation references. No device or source discovery.",
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
            "description": (
                "Generate a deterministic idempotent write request_id from the completed Harness turn digest, operation and AgentBrain target identity. "
                "Different targets/operations in one turn receive different IDs; retries of the same operation get the same ID. Local-only; no transfer. "
                "For content_put with operation=set_enabled, select method=content_set_enabled to retain its original request ID namespace."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "turn_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                    "method": {
                        "type": "string",
                        "description": "Use the underlying write method. For content_lifecycle, select the method corresponding to its action.",
                        "enum": [
                            "content_put",
                            "content_remove",
                            "content_set_enabled",
                            *sorted(lifecycle_writes),
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
        tools.append({
            "name": TOOL_NAME,
            "description": "Run Agent Brain Onboarding once for the current host, after the user's explicit approval of its shareable personal native memory and the exact enrolled destination. The Client returns an approval_id and submits authorized originals through the existing L0 queue in the background. Use resume=true with that ID and the same scope and destination for progress without original text.",
            "inputSchema": INPUT_SCHEMA,
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
            },
        })
    return tools


def memory_sources_authorized(policy, grant):
    if policy is None and grant is None:
        return True  # isolated protocol self-check
    return "memory" in (policy or {}).get("kinds", []) and (
        grant is None or "memory" in grant.get("artifact_types", [])
    )


def tool_names(methods, memory_sources):
    names = set(methods) - contract.HOOK_METHODS - LIFECYCLE_METHODS - {"content_set_enabled"}
    if LIFECYCLE_METHODS.intersection(methods):
        names.add(LIFECYCLE_TOOL)
    names |= {
        tool["name"]
        for tool in local_tools(
            [], memory_sources, memory_sources and "memory_query" in methods
        )
    }
    if not set(methods) & CONTENT_WRITES:
        names.discard("content_queue_inspect")
    if "content_put" not in methods:
        names.discard(TOOL_NAME)
    return sorted(names)


def authorized_content_methods(policy, grant, *, lifecycle=True):
    validate_policy(policy, grant)
    methods = contract.mcp_methods(grant["scopes"])
    if "memory" not in policy["kinds"]:
        methods -= {"memory_query"}
    if not set(policy["kinds"]) & RULE_SKILL:
        methods -= RULE_SKILL_WRITES
    if not lifecycle:
        methods -= LIFECYCLE_WRITES
    return methods


def enabled_tools(policy, grant, *, lifecycle=True):
    """Public tool allowlist for the enrolled content policy and grant."""
    methods = authorized_content_methods(policy, grant, lifecycle=lifecycle)
    return tool_names(methods, memory_sources_authorized(policy, grant))


def lifecycle_tool(methods, kinds, client_version, update_guidance):
    actions = [action for action, method in LIFECYCLE_ACTION_METHODS.items() if method in methods]
    archive_actions = [action for action in ("archive", "restore") if action in actions]
    rule_skill_kinds = [kind for kind in kinds if kind in RULE_SKILL]
    action_guidance = {
        "archive": "Archive a Rule or Skill, removing it from the active catalog and local mappings.",
        "restore": "Restore the original Rule or Skill with enabled:false.",
        "remove": "Remove an explicitly identified item using the existing remove grant and ordinary read revision.",
    }
    description = (
        "Only on the user's explicit request for this exact target. "
        + " ".join(action_guidance[action] for action in actions)
        + " On exact retry, retain the same action, target, expected_revision and request_id. "
        "Check Server persistence and local_refresh separately. "
        f"Running worker version: {client_version}. {update_guidance}"
    )
    existing_revision = "^[a-f0-9]{64}$"
    revision_sources = []
    if archive_actions:
        revision_sources.append("Use content_read with include_archived:true for archive or restore.")
    if "remove" in actions:
        revision_sources.append("Use the ordinary content_read for remove.")
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": actions},
            "kind": {"type": "string", "enum": kinds if "remove" in actions else rule_skill_kinds},
            "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"},
            "expected_revision": {
                "type": "string",
                "pattern": "^(missing|[a-f0-9]{64})$" if "remove" in actions else existing_revision,
                "description": " ".join(revision_sources),
            },
            "request_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{16,128}$"},
        },
        "required": ["action", "kind", "id", "expected_revision", "request_id"],
        "additionalProperties": False,
    }
    if archive_actions and "remove" in actions:
        schema["allOf"] = [{
            "if": {"properties": {"action": {"enum": archive_actions}}},
            "then": {"properties": {
                "kind": {"enum": rule_skill_kinds},
                "expected_revision": {"pattern": existing_revision},
            }},
        }]
    return {
        "name": LIFECYCLE_TOOL,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": "archive" in actions or "remove" in actions,
            "idempotentHint": True,
        },
    }


def tool_definitions(methods, kinds, memory_sources, client_version, update_guidance, *, lifecycle=True):
    """Build every content MCP declaration from the same authorized catalog."""
    tools = []
    declared = tool_names(methods, memory_sources)
    activation_kinds = [kind for kind in kinds if kind in RULE_SKILL]
    activation = "content_set_enabled" in methods and bool(activation_kinds)
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
        archive_read = name == "content_read" and lifecycle and bool(RULE_SKILL.intersection(kinds))
        if archive_read:
            properties["include_archived"] = {
                "type": "boolean",
                "enum": [True],
                "description": "For one exact Rule or Skill, set true to read lifecycle state and the CAS revision for archive or restore. Omit for the ordinary active view and edit/enable CAS revision.",
            }
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
        if name == "content_put":
            properties["reconciles"] = {
                "type": "object",
                "description": "Local-only references for recovering retained writes; never sent to Server. Inspect retained proposals with content_queue_inspect and read the current target before merging. Use request_ids for retained conflicts and recover pending outcomes by identical retry first. Native source_keys are only for recovering retained requests that already reference them. Server persistence confirmation resolves selected conflicts.",
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
        if archive_read:
            description += "With include_archived:true the Server must be live and support lifecycle reads. An archived target returns archived:true, record:null and a restore revision. Default reads and catalog remain active-only. "
        if name == "content_put":
            description += "For ordinary edits, omit operation and include record. Preserve existing Rule/Skill enabled state and create disabled. "
            if activation:
                description += "On explicit user request to enable or disable this exact existing Rule/Skill, use operation=set_enabled and a boolean enabled with its current revision. Omit record and source_context. Only retained activation conflicts on the same target may be reconciled by set_enabled. Check local_refresh separately from persistence. "
        if name == "content_catalog":
            description += "AgentBrain destination records only; this does not discover Harness-native sources. Omit cursor for the first authorized inventory page, then follow next_cursor until absent. Use memory_query for retrieval. "
        if name in ("memory_query", "content_read", "content_catalog"):
            description += RECALL_EVIDENCE_GUIDANCE
        description += (
            "Running worker version: " + client_version + ". " + update_guidance
        )
        schema = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        if name == "content_put" and activation:
            required.remove("record")
            properties["operation"] = {"type": "string", "enum": ["set_enabled"]}
            properties["enabled"] = {"type": "boolean"}
            schema.update({
                "if": {"required": ["operation"]},
                "then": {
                    "required": ["enabled"],
                    "not": {"anyOf": [{"required": ["record"]}, {"required": ["source_context"]}]},
                    "properties": {
                        "kind": {"enum": activation_kinds},
                        "expected_revision": {"pattern": "^[a-f0-9]{64}$"},
                        "reconciles": {
                            "description": "Select only retained activation conflicts for this target after reading the current revision. Source imports require an ordinary edit.",
                            "properties": {"source_keys": {"maxItems": 0}},
                        },
                    },
                },
                "else": {"required": ["record"], "not": {"required": ["enabled"]}},
            })
        tools.append(
            {
                "name": name,
                "description": description,
                "inputSchema": schema,
                "annotations": {
                    "readOnlyHint": name not in contract.WRITES,
                    "destructiveHint": False,
                    "idempotentHint": True,
                },
            }
        )
    if LIFECYCLE_TOOL in declared:
        tools.append(lifecycle_tool(methods, kinds, client_version, update_guidance))
    tools.extend(
        tool
        for tool in local_tools(
            kinds,
            memory_sources,
            "memory_query" in declared,
            LIFECYCLE_WRITES.intersection(methods),
        )
        if tool["name"] in declared
    )
    return tools

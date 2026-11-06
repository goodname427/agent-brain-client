"""Rule-only recall/capture use cases, with no Harness or filesystem dependency."""

import hashlib
import json
from typing import Callable, Protocol

from agent_brain_client.domain import contract
from agent_brain_client.domain.hook_coverage import CAPABILITIES, fallback_plan


class RuleCaptureQueue(Protocol):
    queue_sha: str

    def submit_rule_capture(
        self, request: dict, *, gate: Callable[[], bool]
    ) -> dict: ...


REPRESENTATIONS = {
    "user_prompt": ("observed-user-message", "agent-summary"),
    "assistant_final": ("observed-assistant-message", "agent-summary"),
}


def rule_instructions(hooks, *, rule_enabled=False):
    """Return guidance only for missing Hooks; a future host binds tool names."""
    plan = fallback_plan(hooks, rule_enabled=rule_enabled)
    actions = {
        "session_start": "Before handling the new session, call the approved Rule recall entry.",
        "user_prompt": "Call the Rule capture entry for the current user message or an explicitly labelled Agent summary.",
        "assistant_final": "Before delivery, call the Rule capture entry for an Agent summary. "
        "Only label text as an observed assistant message after it was actually sent.",
    }
    selected = [(name, actions[name]) for name in CAPABILITIES if plan[name] == "rule"]
    if not selected:
        return []
    boundary = (
        " Use only approved sources. Reuse the same operation ID and exact payload on retry. "
        "Do not invent native session or turn IDs, claim complete recording, or treat queued data "
        "as confirmed persistence. Missing calls remain possible."
    )
    return [
        {"capability": name, "instruction": action + boundary}
        for name, action in selected
    ]


class RuleFallback:
    """Host-independent manual entry points, using the existing bound outbox.

    ``hooks`` and ``enabled`` are live callbacks, not a stale generated Rule.
    ``source_gate(capability, source_context)`` checks the current approved
    source/scope. The typed outbox port owns exact staging, cache locking and
    retry; it retains the host's existing identity, grant and client.
    """

    def __init__(self, outbox: RuleCaptureQueue, *, hooks, enabled, source_gate):
        self.outbox = outbox
        self.hooks = hooks
        self.enabled = enabled
        self.source_gate = source_gate

    def _allowed(self, capability, context):
        try:
            return (
                fallback_plan(self.hooks(), rule_enabled=self.enabled())[capability]
                == "rule"
                and self.source_gate(capability, json.loads(contract.encode(context)))
                is True
            )
        except Exception:
            return False

    @staticmethod
    def _context(context):
        # The existing source contract permits unknown origin/session as null.
        # A local operation ID below is never promoted to a native identity.
        if not isinstance(context, dict) or set(context) != {"origin", "session_id"}:
            raise ValueError("Explicit source context required")
        if context["origin"] is not None:
            contract.recall_origin(context["origin"])
        if context["session_id"] is not None:
            contract.event_identifier(context["session_id"])
        return json.loads(contract.encode(context))

    def recall(self, request, *, source_context, invoke):
        """Call the host's approved memory_query; never invent a reader session."""
        context = self._context(source_context)
        if (
            not isinstance(request, dict)
            or set(request) != {"request_id", "method", "params"}
            or request["method"] != "memory_query"
        ):
            raise ValueError("Approved memory query required")
        contract.event_identifier(request["request_id"])
        contract.validate(request["method"], request["params"])
        if not self._allowed("session_start", context):
            return {"status": "inactive", "data_transferred": False}
        return invoke(json.loads(contract.encode(request)))

    def capture(
        self, capability, operation_id, text, *, representation, source_context
    ):
        """Save one labelled manual observation, with exact replay after restart.

        operation_id identifies a local submission, supplied/persisted by the
        caller. It is not a native turn ID. Changed text under the same identity
        is refused by the shared queue; summary updates need a new operation.
        """
        if (
            capability not in REPRESENTATIONS
            or representation not in REPRESENTATIONS[capability]
        ):
            raise ValueError("Explicit observation or Agent summary required")
        contract.event_identifier(operation_id)
        contract.text(text, 32768)
        context = self._context(source_context)
        if not self._allowed(capability, context):
            return {"status": "inactive", "data_transferred": False}
        key = hashlib.sha256(
            contract.encode(
                [
                    "agentbrain.rule.v1",
                    self.outbox.queue_sha,
                    context,
                    capability,
                    operation_id,
                ]
            )
        ).hexdigest()
        identity = "rule-" + key[:59]
        body = {
            "contract": "agentbrain.rule.v1",
            "source": "rule",
            "capability": capability,
            "representation": representation,
            "operation_id": operation_id,
            "text": text,
        }
        request = {
            "request_id": "agentbrain_rule_" + key,
            "method": "content_put",
            "params": {
                "kind": "memory",
                "id": identity,
                "expected_revision": "missing",
                "source_context": context,
                "record": {
                    "title": "Rule fallback observation",
                    "type": "reference",
                    "description": "Manual Rule submission; not verified native capture or complete recording.",
                    "body": contract.encode(body).decode("utf-8"),
                },
            },
        }
        return self.outbox.submit_rule_capture(
            request, gate=lambda: self._allowed(capability, context)
        )

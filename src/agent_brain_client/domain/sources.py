"""domain / sources: one owner for each contract and rule."""

import re
from agent_brain_client.domain.content import TYPES


def review_memory_sources(sources, candidates, mode="daily"):
    """Offline policy review of explicit metadata, not collection or authorization.

    Caller classifications are evidence to review, never permission grants or
    automatic language understanding. Bodies/files/credentials are not inputs.
    """
    if mode not in ("daily", "onboard"):
        raise ValueError("Explicit review mode required")
    if (
        not isinstance(sources, list)
        or not isinstance(candidates, list)
        or len(sources) > 256
        or len(candidates) > 1024
    ):
        raise ValueError("Bounded explicit review required")
    index = {}
    counts = {}
    seen = set()
    rows = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "source_id",
            "status",
            "authorized",
            "expected_candidates",
            "reason",
        }:
            raise ValueError("Explicit source coverage required")
        identity = source["source_id"]
        expected = source["expected_candidates"]
        if (
            not isinstance(source["reason"], str)
            or not 1 <= len(source["reason"]) <= 512
            or not isinstance(identity, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identity)
            or identity in index
            or source["status"]
            not in ("read", "excluded", "unavailable", "not-checked")
            or type(source["authorized"]) is not bool
            or expected is not None
            and (type(expected) is not int or not 0 <= expected <= 1024)
        ):
            raise ValueError("Invalid source review")
        index[identity] = source
        counts[identity] = 0
    for candidate in candidates:
        keys = {
            "id",
            "source_id",
            "kind",
            "scope",
            "evidence",
            "utility",
            "sensitive",
            "raw_history",
            "duplicate",
            "requested",
        }
        if not isinstance(candidate, dict) or set(candidate) != keys:
            raise ValueError("Explicit candidate reasons required")
        identity = candidate["id"]
        source = index.get(candidate["source_id"])
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identity)
            or identity in seen
            or source is None
            or candidate["kind"] not in TYPES
            or candidate["scope"]
            not in ("personal-shared", "project-only", "local-only")
            or candidate["evidence"]
            not in ("user-stated", "observed-result", "inferred")
            or candidate["utility"]
            not in (
                "preference",
                "continuity",
                "useful-fact",
                "reusable-guidance",
                "ephemeral",
                "none",
            )
            or any(
                type(candidate[k]) is not bool
                for k in ("sensitive", "raw_history", "duplicate", "requested")
            )
        ):
            raise ValueError("Invalid candidate review")
        seen.add(identity)
        counts[candidate["source_id"]] += 1
        if (
            candidate["sensitive"]
            or candidate["raw_history"]
            or candidate["scope"] == "local-only"
        ):
            decision, reason = "excluded", "privacy-or-prohibited-history"
        elif not source["authorized"] or source["status"] != "read":
            decision, reason = "deferred", "source-access-not-authorized-or-unavailable"
        elif candidate["scope"] == "project-only":
            decision, reason = "deferred", "outside-personal-shared-scope"
        elif candidate["duplicate"]:
            decision, reason = "duplicate", "equivalent-record-already-present"
        elif mode == "onboard" and candidate["kind"] == "memory":
            decision, reason = (
                "candidate",
                "all-authorized-existing-memory-preserve-unique-information-and-provenance",
            )
        elif candidate["requested"]:
            decision, reason = "candidate", "explicit-user-request-preserve-evidence"
        elif candidate["evidence"] == "inferred":
            decision, reason = "review", "unverified-inference"
        elif candidate["utility"] in ("ephemeral", "none"):
            decision, reason = "skipped", "no-useful-future-context"
        else:
            decision, reason = "candidate", "useful-authorized-fact-or-continuity"
        rows.append(
            {
                "id": identity,
                "source_id": candidate["source_id"],
                "kind": candidate["kind"],
                "decision": decision,
                "reason": reason,
            }
        )
    coverage = []
    for identity, source in index.items():
        expected = source["expected_candidates"]
        actual = counts[identity]
        if expected is not None and actual > expected:
            raise ValueError("Source candidate count mismatch")
        coverage.append(
            dict(
                source,
                reviewed_candidates=actual,
                fully_reviewed=source["authorized"]
                and source["status"] == "read"
                and expected is not None
                and actual == expected,
            )
        )
    totals = {
        name: sum(r["decision"] == name for r in rows)
        for name in (
            "candidate",
            "duplicate",
            "skipped",
            "excluded",
            "deferred",
            "review",
        )
    }
    return {
        "mode": mode,
        "sources": coverage,
        "candidates": rows,
        "counts": totals,
        "accessible_review_complete": all(
            r["fully_reviewed"]
            for r in coverage
            if r["authorized"] and r["status"] == "read"
        ),
        "coverage_gaps": sum(
            r["status"] in ("unavailable", "not-checked")
            or r["authorized"]
            and r["status"] == "read"
            and not r["fully_reviewed"]
            for r in coverage
        ),
        "account_complete": False,
        "data_transferred": False,
        "permissions_granted": False,
    }

"""Live content management, independent of CLI, browser and MCP.

A supplied content session owns remote I/O, durable writes and native mapping.
"""

import re
from agent_brain_client.domain import contract

GOOD_REFRESH = {"content-current", "content-refreshed"}


class LiveReadRequired(Exception):
    pass


class Management:
    def __init__(self, adapter):
        self.adapter = adapter

    def read(self, method, params):
        import uuid

        reply = self.adapter.invoke(
            {"request_id": uuid.uuid4().hex, "method": method, "params": params}
        )
        if "error" in reply or reply.get("local_cache", {}).get("stale") is True:
            raise LiveReadRequired()
        return reply["result"]

    def state(self):
        kinds = [
            kind for kind in ("rule", "skill") if kind in self.adapter.policy["kinds"]
        ]
        rows = []
        if kinds:
            cursor = None
            commit = None
            seen = set()
            while True:
                page = self.read(
                    "content_catalog",
                    dict(kinds=kinds, **({"cursor": cursor} if cursor else {})),
                )
                if commit is not None and page["commit"] != commit:
                    raise LiveReadRequired()
                commit = page["commit"]
                for item in page["artifacts"]:
                    key = (item["kind"], item["id"])
                    if key in seen:
                        raise LiveReadRequired()
                    seen.add(key)
                    value = self.read("content_read", {"kind": key[0], "id": key[1]})
                    if value["record"] is None or value["revision"] != item["revision"]:
                        raise LiveReadRequired()
                    record = value["record"]
                    rows.append(
                        {
                            "kind": key[0],
                            "id": key[1],
                            "revision": value["revision"],
                            "title": record.get(
                                "title", record.get("description", key[1])
                            ),
                            "enabled": record["enabled"],
                            "mapping": self.adapter.mapping.mapping_status(
                                *key, value["revision"]
                            ),
                        }
                    )
                next_cursor = page.get("next_cursor")
                if next_cursor is None:
                    break
                if next_cursor == cursor:
                    raise LiveReadRequired()
                cursor = next_cursor
        operations = []
        cursor = None
        while True:
            queue = self.adapter.inspect_queue(cursor=cursor)
            for item in queue["entries"]:
                if item["method"] != "content_set_enabled" or item["local_queue"][
                    "state"
                ] not in ("pending", "blocked", "conflict"):
                    continue
                retained = self.adapter.inspect_queue(request_id=item["request_id"])
                request = retained["request"]
                operations.append(
                    {
                        "request_id": request["request_id"],
                        **request["params"],
                        "state": item["local_queue"]["state"],
                        "error": (retained.get("response") or {}).get("error"),
                    }
                )
            cursor = queue["next_cursor"]
            if cursor is None:
                break
        return {
            "status": "live",
            "records": rows,
            "operations": operations,
            "queue": queue["counts"],
            "can_change": "content_set_enabled" in self.adapter.methods(),
            "model_loaded": "unknown",
        }

    def change(self, value):
        if not isinstance(value, dict) or set(value) != {
            "request_id",
            "kind",
            "id",
            "enabled",
            "expected_revision",
            "reconcile_request_ids",
        }:
            raise ValueError("Exact activation action required")
        params = {
            key: value[key] for key in ("kind", "id", "enabled", "expected_revision")
        }
        contract.validate("content_set_enabled", params)
        return self.adapter.invoke(
            {
                "request_id": value["request_id"],
                "method": "content_set_enabled",
                "params": params,
            },
            reconciles={
                "request_ids": value["reconcile_request_ids"],
                "source_keys": [],
            },
        )

    def retry(self, value):
        if not isinstance(value, dict) or set(value) != {"request_id"}:
            raise ValueError("Exact retained request ID required")
        rid = value["request_id"]
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", rid):
            raise ValueError("Exact retained request ID required")
        retained = self.adapter.inspect_queue(request_id=rid)
        request = retained.get("request", {})
        if request.get("method") != "content_set_enabled" or retained["local_queue"][
            "state"
        ] not in ("pending", "blocked"):
            raise ValueError("Only an unresolved activation request can be retried")
        return self.adapter.invoke(request, reconciles=retained["reconciles"])

    def pull(self, value):
        if value != {}:
            raise ValueError("Pull takes no arguments")
        return {
            "local_refresh": self.adapter.refresh(replay=False),
            "server_write_requested": False,
        }

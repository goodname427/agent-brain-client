"""Stop acknowledgments through real HTTPS facades and transport decoders."""

import io
import json
import unittest
from unittest.mock import patch

from agent_brain_client.adapters.server.collection import CollectionClient
from agent_brain_client.adapters.server.http import RemoteClient
from agent_brain_client.adapters.server.https import HTTPSContentClient
from agent_brain_client.application.collection.control import CollectionControl
from agent_brain_client.runtime import project_memory
from test_collection_control import ControlPorts


class StopConfirmation(unittest.TestCase):
    def client(self, response):
        calls = []
        class Opener:
            def open(inner, request, timeout):
                decoded = json.loads(request.data)
                calls.append(decoded["method"])
                body = dict(response, request_id=decoded["request_id"])
                return io.BytesIO(json.dumps(body).encode())

        transport = RemoteClient(
            "https://synthetic.invalid", data_mode="personal",
            credential_provider=lambda: "s" * 32,
        )
        transport.opener = Opener()
        client = object.__new__(HTTPSContentClient)
        client.transport = transport
        client.grant = {"artifact_types": ["memory"]}
        client.profile = {"endpoint": "https://synthetic.invalid"}
        client._credential = lambda: "synthetic-credential"
        client.synthetic_calls = calls
        return client

    def test_global_wire_requires_explicit_stop_confirmation(self):
        replies = [
            {"result": {"schema": 1, "stopped": True}},
            {"result": {"schema": 1, "stopped": False}},
            {"result": {"schema": 999}},
            {"result": {"schema": True, "stopped": True}},
            {"result": {"schema": 1, "stopped": 1}},
            {}, {"result": None}, {"error": {"code": "synthetic_denial"}},
        ]
        for index, response in enumerate(replies):
            with self.subTest(response=response):
                client = object.__new__(CollectionClient)
                client.base = self.client(response)
                ports = ControlPorts()
                ports.remote = lambda value, method: client.wire(method, {})
                result = CollectionControl(ports).stop(
                    {"server_scope_sha256": "a" * 64, "legacy_projects": []}
                )
                self.assertEqual(result["remote_confirmed"], index == 0)
                self.assertEqual(result["status"], "global-memory-stopped" if index == 0 else "global-memory-stop-unconfirmed")
                self.assertTrue(ports.fenced)
                self.assertEqual(client.base.synthetic_calls, ["memory_global_stop"])

    def test_project_stop_requires_both_admissions_false(self):
        status = {"schema": 2, "lifecycle": "until-disabled",
                  "model_admitted": False, "source_admitted": False}
        replies = [
            {"result": status},
            *({"result": dict(status, model_admitted=model, source_admitted=source)}
              for model, source in ((True, True), (True, False), (False, True),
                                    (0, False), (False, 0), (False, None))),
            *({"result": dict(status, schema=schema)} for schema in (999, 2.0, True)),
            {"result": {"schema": 2}}, {},
            {"result": status, "error": {"code": "synthetic_denial"}},
        ]
        policy = {"schema": 2, "project_id": "synthetic-project",
                  "recall_session_id": "synthetic-reader"}
        source = {"recall_policy": "synthetic", "recall_policy_sha256": "a" * 64}
        for action in ("stop", "revoke"):
            for index, response in enumerate(replies):
                with self.subTest(action=action, response=response):
                    client = self.client(response)
                    with patch.object(project_memory, "private_json", return_value=policy), patch.object(
                        project_memory, "adapter_for", return_value=(client, None)
                    ):
                        if index == 0:
                            self.assertEqual(project_memory.daily_command(source, action), status)
                        else:
                            with self.assertRaises(ValueError):
                                project_memory.daily_command(source, action)
                    self.assertEqual(client.synthetic_calls, ["memory_" + action])

    def test_status_keeps_reporting_admitted_sources(self):
        status = {"schema": 2, "lifecycle": "until-disabled",
                  "model_admitted": True, "source_admitted": True}
        source = {"recall_policy": "synthetic", "recall_policy_sha256": "a" * 64}
        with patch.object(project_memory, "private_json", return_value={
            "schema": 2, "project_id": "synthetic-project",
            "recall_session_id": "synthetic-reader",
        }), patch.object(project_memory, "adapter_for", return_value=(self.client({"result": status}), None)):
            self.assertEqual(project_memory.daily_command(source, "server-status"), status)

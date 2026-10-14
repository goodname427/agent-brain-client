"""Synthetic MCP/HTTP stages; no network, credentials or real memory bodies."""

import http.client
import io
import json
import ssl
import unittest
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

import content_contract as contract
from content_mcp import ContentServer, ContentParameterError, serve
from remote_http import RemoteClient


class Adapter:
    policy = {"kinds": ["memory"]}

    def __init__(self):
        self.calls = []

    def methods(self):
        return set(contract.FIELDS)

    def invoke(self, request):
        self.calls.append(request)
        return {
            "request_id": request["request_id"],
            "result": {"artifacts": [{}] * 23},
            "persistence": {"durable": True},
        }


class ContentErrors(unittest.TestCase):
    def setUp(self):
        self.adapter = Adapter()
        self.server = ContentServer(self.adapter, "0.2.11")
        self.server.state = "ready"

    def test_catalog_limit_rejects_locally_and_full_inventory_is_unbounded(self):
        for limit in (100, True, 0, 4):
            with self.assertRaises(ContentParameterError) as error:
                self.server.invoke(
                    "content_catalog", {"kinds": ["memory"], "limit": limit}
                )
            self.assertEqual(error.exception.code, -32602)
            self.assertIn("no pagination", error.exception.message)
            self.assertNotIn("Writes", error.exception.message)
        self.assertEqual(self.adapter.calls, [])
        result = self.server.invoke("content_catalog", {"kinds": ["memory"]})
        self.assertEqual(len(result["structuredContent"]["result"]["artifacts"]), 23)
        self.assertEqual(self.adapter.calls[0]["method"], "content_catalog")
        self.assertEqual(
            result["structuredContent"]["client_runtime"]["running_worker_version"],
            "0.2.11",
        )
        self.server.invoke("content_catalog", {"kinds": ["memory"], "limit": 3})
        self.assertEqual(self.adapter.calls[-1]["method"], "memory_query")

    def test_skill_dependency_schema_matches_shared_validator_limit(self):
        tools=self.server.handle({'jsonrpc':'2.0','id':1,'method':'tools/list'})['tools']
        put=next(tool for tool in tools if tool['name']=='content_put')
        skill=put['inputSchema']['properties']['record']['oneOf'][2]
        dependencies=skill['properties']['dependencies']['properties']
        self.assertEqual(dependencies['commands']['maxItems'],32)
        self.assertEqual(dependencies['python_modules']['maxItems'],32)
        record={'description':'Synthetic skill','enabled':False,
                'files':{'SKILL.md':'---\\nname: synthetic-skill\\ndescription: Synthetic skill\\n---\\nSynthetic'},
                'dependencies':{'commands':['synthetic-command']*33,'python_modules':[]}}
        with self.assertRaises(ContentParameterError) as error:
            self.server.invoke('content_put',{'kind':'skill','id':'synthetic-skill',
                'record':record,'expected_revision':'missing','request_id':'synthetic_request_123456'})
        self.assertEqual(error.exception.code,-32602)
        self.assertEqual(self.adapter.calls,[])

    def test_read_error_does_not_give_write_parameters_and_mcp_reports_stage(self):
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "content_read", "arguments": {"kind": "memory"}},
        }
        output = io.StringIO()
        serve(self.server, io.BytesIO((json.dumps(request) + "\n").encode()), output)
        error = json.loads(output.getvalue())["error"]
        self.assertEqual(
            (error["code"], error["data"]["stage"]), (-32602, "parameters")
        )
        self.assertNotIn("Writes", error["message"])
        self.assertNotIn("expected_revision", error["message"])
        self.assertEqual(self.adapter.calls, [])
        with self.assertRaises(ContentParameterError) as failure:
            self.server.invoke("content_put", {})
        self.assertIn("stable request_id", failure.exception.message)

    def test_public_tool_guidance_distinguishes_update_worker_and_complete_catalog(
        self,
    ):
        tools = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[
            "tools"
        ]
        catalog = next(tool for tool in tools if tool["name"] == "content_catalog")
        for text in (
            "--action check-update",
            "--action update",
            "running_worker_version",
            "kinds only",
            "no pagination",
        ):
            self.assertIn(text, catalog["description"])


class HTTPStages(unittest.TestCase):
    def setUp(self):
        self.client = RemoteClient(
            "https://synthetic.invalid",
            credential_provider=lambda: "SYNTHETIC_TOKEN_12345678901234567890",
        )
        self.client.opener = MagicMock()
        self.request = {
            "request_id": "synthetic_read_123456789",
            "method": "content_read",
            "params": {},
        }

    def response(self, raw, *, write=False):
        self.client.opener.open.return_value.__enter__.return_value.read.return_value = raw
        return self.client.request(
            dict(self.request, method="content_put" if write else "content_read")
        )

    def test_parse_and_validation_are_not_transport_and_preserve_unknown_write_retry(
        self,
    ):
        for raw, code, stage in [
            (b"SYNTHETIC_PRIVATE_RESPONSE", "invalid_response_json", "response-parse"),
            (b"[]", "invalid_response_shape", "response-validation"),
            (b"{}", "response_id_mismatch", "response-validation"),
            (b"x" * 262145, "response_too_large", "response-parse"),
        ]:
            for write in (False, True):
                with self.subTest(code=code, write=write):
                    result = self.response(raw, write=write)
                    self.assertEqual(
                        result["error"],
                        {"code": code, "stage": stage, "retryable": write},
                    )
                    self.assertEqual(result["request_id"], self.request["request_id"])
                    self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))

    def test_http_reports_status_and_drops_untrusted_body_and_detail(self):
        for status in (401, 429, 503):
            self.client.opener.open.side_effect = HTTPError(
                "https://synthetic.invalid",
                status,
                "SYNTHETIC_PRIVATE_DETAIL",
                {},
                io.BytesIO(b"SYNTHETIC_PRIVATE_RESPONSE"),
            )
            result = self.client.request(self.request)
            self.assertEqual(result["error"]["stage"], "http")
            self.assertEqual(result["error"]["http_status"], status)
            self.assertEqual(result["error"]["retryable"], status in (429, 503))
            self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        body = {
            "request_id": self.request["request_id"],
            "error": {
                "code": "forbidden",
                "retryable": False,
                "debug": "SYNTHETIC_PRIVATE_DEBUG",
            },
        }
        self.client.opener.open.side_effect = HTTPError(
            "https://synthetic.invalid",
            403,
            "",
            {},
            io.BytesIO(json.dumps(body).encode()),
        )
        self.assertEqual(
            self.client.request(self.request)["error"],
            {
                "code": "forbidden",
                "retryable": False,
                "stage": "http",
                "http_status": 403,
            },
        )

    def test_transport_categories_have_no_http_status_or_secret_exception(self):
        for exception, kind in [
            (URLError(TimeoutError("SYNTHETIC_PRIVATE")), "timeout"),
            (URLError(ssl.SSLError("SYNTHETIC_PRIVATE")), "tls"),
            (http.client.IncompleteRead(b"SYNTHETIC_PRIVATE"), "http-framing"),
            (URLError("SYNTHETIC_PRIVATE"), "connection"),
        ]:
            self.client.opener.open.side_effect = exception
            result = self.client.request(self.request)
            self.assertEqual(
                result["error"],
                {
                    "code": "transport_unconfirmed",
                    "stage": "transport",
                    "retryable": True,
                    "failure_kind": kind,
                },
            )
            self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))

    def test_success_status_error_is_sanitized_and_invalid_error_is_validation(self):
        response = {
            "request_id": self.request["request_id"],
            "error": {
                "code": "forbidden",
                "retryable": False,
                "debug": "SYNTHETIC_PRIVATE_DEBUG",
            },
        }
        self.client.opener.open.return_value.__enter__.return_value.status = 200
        result = self.response(json.dumps(response).encode())
        self.assertEqual(
            result["error"],
            {
                "code": "forbidden",
                "retryable": False,
                "stage": "http",
                "http_status": 200,
            },
        )
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        response["error"]["retryable"] = "false"
        self.assertEqual(
            self.response(json.dumps(response).encode())["error"]["code"],
            "invalid_response_error",
        )


if __name__ == "__main__":
    unittest.main()

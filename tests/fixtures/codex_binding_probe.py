"""Run real MCP stdio from the installed synthetic Codex command."""

from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.adapters.storage.registration import content_location
from agent_brain_client.interfaces.mcp.content import ContentServer
from agent_brain_client.interfaces.mcp.server import serve
from agent_brain_client.runtime.harness_content import content_host

home = Path(sys.argv[2])
harness = sys.argv[sys.argv.index("--harness") + 1]
runtime = {"codex_home": str(home / ".config/agent-brain-client/shared-native")}
native, cache = content_location(home, runtime, harness)

POLICY = {
    "schema": 1, "adapter": "codex", "kinds": ["memory"], "ids": {"memory": "*"},
    "dependencies": {"commands": [], "python_modules": []},
}


class SyntheticService:
    grant = {
        "adapter": "codex", "actor": "synthetic-owner", "scopes": ["read", "put"],
        "artifact_types": POLICY["kinds"], "ids": POLICY["ids"],
    }

    def request(self, request):
        assert request["method"] == "content_catalog"
        return {"request_id": request["request_id"], "result": {
            "content_api_version": 1, "commit": "a" * 40, "artifacts": [],
        }}


adapter = ContentSession(home, cache, POLICY, SyntheticService(),
                         host=content_host(harness), native_home=native)
serve(ContentServer(adapter), sys.stdin.buffer, sys.stdout)

"""Codex native instruction and Skill rendering over shared transactions."""

import json
import re
from agent_brain_client.adapters.storage.native_mapping import NativeMapping
from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked, CollectionCapacityExceeded
from agent_brain_client.application.memory.guidance import HOST_GUIDANCE
from agent_brain_client.adapters.harnesses.codex.content_limits import BEGIN, END, BLOCK
from agent_brain_client.adapters.storage.content_limits import MAPPING_BYTES


class CodexContentMapping(NativeMapping):
    skill_prefix = ".agents/skills"
    instruction_keys = {".codex/AGENTS.md"}

    def logical_name(self, path):
        return ".codex/AGENTS.md" if path == self.session.native_home / "AGENTS.md" else super().logical_name(path)

    def detached_bytes(self, name, raw):
        if name not in self.instruction_keys:
            return None
        text = raw.decode()
        if text.count(BEGIN) != 1 or text.count(END) != 1 or len(BLOCK.findall(text)) != 1:
            raise CacheBlocked()
        return BLOCK.sub("", text).encode()

    def _path(self, name):
        if name == ".codex/AGENTS.md":
            path = self.session.native_home / "AGENTS.md"
            self.session._safe(path, self.session.native_home)
            return path
        else:
            matched = re.fullmatch(
                r"\.config/agent-brain-client/content/(memory|rule)/([a-z0-9-]+)\.md",
                name,
            )
            skill = re.fullmatch(r"\.agents/skills/([a-z0-9-]+)/(.+)", name)
            if matched:
                contract.validate(
                    "content_read", {"kind": matched[1], "id": matched[2]}
                )
                if not self.session._allows(matched[1], matched[2]):
                    raise CacheBlocked()
            elif skill:
                if not self.session._allows("skill", skill[1]):
                    raise CacheBlocked()
                contract.skill_path(skill[2])
            else:
                raise CacheBlocked()
        path = self.session.home / name
        self.session._safe(path, self.session.home)
        return path

    def _mapping(self, snapshot, owned):
        desired = {}
        lines = [
            "# Agent Brain cloud content",
            "This mapping is a rebuildable cache. Read/upload through the approved Server API; do not write GitHub directly.",
            "Cloud refresh does not replace already loaded model context.",
        ]
        lines.append(HOST_GUIDANCE)
        for value in snapshot["records"]:
            kind, identity, record = value["kind"], value["id"], value["record"]
            if kind in ("memory", "rule"):
                name = (
                    ".config/agent-brain-client/content/"
                    + kind
                    + "/"
                    + identity
                    + ".md"
                )
                desired[name] = record["body"].encode()
                if kind == "memory":
                    # Discover/read Memory through authorized content tools, not a global AGENTS index.
                    pass
                elif record["enabled"]:
                    trigger = record["trigger"]
                    if trigger["mode"] == "always":
                        lines.extend(["## " + record["title"], record["body"]])
                    else:
                        lines.append(
                            "Rule "
                            + identity
                            + " / "
                            + record["title"]
                            + " ("
                            + trigger["mode"]
                            + ", patterns="
                            + json.dumps(trigger["patterns"])
                            + "): "
                            + str(self.session.home / name)
                        )
            elif record["enabled"]:
                desired.update(self.skill_files(record, identity))
        block = BEGIN + "\n" + "\n".join(lines) + "\n" + END + "\n"
        if block.count(BEGIN) != 1 or block.count(END) != 1:
            raise CacheBlocked()
        name = ".codex/AGENTS.md"
        raw = self._raw(name)
        text = (raw or b"").decode()
        if (
            text.count(BEGIN) != text.count(END)
            or text.count(BEGIN) > 1
            or BEGIN in text
            and name not in owned["files"]
        ):
            raise CacheBlocked()
        text = BLOCK.sub("", text)
        desired[name] = (
            text.rstrip() + "\n\n" + block if text.strip() else block
        ).encode()
        if sum(len(raw) for raw in desired.values()) > MAPPING_BYTES:
            raise CollectionCapacityExceeded()
        return desired

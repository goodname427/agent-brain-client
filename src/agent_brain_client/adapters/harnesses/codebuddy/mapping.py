"""CodeBuddy Rules/Skills paths and frontmatter over shared snapshot transactions."""

import json
import re
from agent_brain_client.adapters.storage.native_mapping import NativeMapping
from agent_brain_client.adapters.storage.content_limits import MAPPING_BYTES
from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked, CollectionCapacityExceeded


class CodeBuddyContentMapping(NativeMapping):
    skill_prefix = ".codebuddy/skills"
    instruction_keys = frozenset()
    index = ".codebuddy/rules/agent-brain-client/index.md"
    documents = ".config/agent-brain-client/content/codebuddy"

    def _path(self, name):
        if name == self.index:
            path = self.session.home / name
            self.session._safe(path, self.session.home)
            return path
        document = re.fullmatch(re.escape(self.documents) + r"/(memory|rule)/([a-z0-9-]+)\.md", name)
        rule = re.fullmatch(r"\.codebuddy/rules/agent-brain-([a-z0-9-]+)\.md", name)
        skill = re.fullmatch(re.escape(self.skill_prefix) + r"/([a-z0-9-]+)/(.+)", name)
        if document:
            kind, identity = document.groups()
        elif rule:
            kind, identity = "rule", rule[1]
        elif skill:
            kind, identity = "skill", skill[1]
            contract.skill_path(skill[2])
        else:
            raise CacheBlocked()
        contract.validate("content_read", {"kind": kind, "id": identity})
        if not self.session._allows(kind, identity):
            raise CacheBlocked()
        path = self.session.home / name
        self.session._safe(path, self.session.home)
        return path

    def _mapping(self, snapshot, owned):
        desired = {}
        index = ["---", "alwaysApply: true", "enabled: true", "---", "# Agent Brain cloud Rule index",
                 "Use a referenced Rule when its title and trigger match the current task. Read its file for the full instructions; preserve the trigger and current project scope."]
        for value in snapshot["records"]:
            kind, identity, record = value["kind"], value["id"], value["record"]
            if kind in ("memory", "rule"):
                desired[self.documents + "/" + kind + "/" + identity + ".md"] = record["body"].encode()
            if kind == "rule" and record["enabled"]:
                trigger = record["trigger"]
                if trigger["mode"] != "always":
                    index.append(record["title"] + " (" + trigger["mode"] + ", paths=" + json.dumps(trigger["patterns"]) + "): " + str(self.session.home / self.documents / "rule" / (identity + ".md")))
                always = trigger["mode"] == "always"
                # CLI paths controls automatic matching; IDE uses description for
                # agent-requested rules. Keep both fields, never widen to always.
                header = ("---\n" + "description: " + json.dumps(record["title"], ensure_ascii=False)
                          + "\nenabled: true\nalwaysApply: " + str(always).lower() + "\n")
                if trigger["patterns"]:
                    patterns = json.dumps(trigger["patterns"], ensure_ascii=False)
                    header += "paths: " + patterns + "\nglobs: " + patterns + "\n"
                header += "---\n\n"
                desired[".codebuddy/rules/agent-brain-" + identity + ".md"] = (
                    header + "# " + record["title"] + "\n\n" + record["body"] + "\n"
                ).encode()
            elif kind == "skill" and record["enabled"]:
                desired.update(self.skill_files(record, identity))
        desired[self.index] = ("\n".join(index) + "\n").encode()
        if sum(map(len, desired.values())) > MAPPING_BYTES:
            raise CollectionCapacityExceeded()
        return desired

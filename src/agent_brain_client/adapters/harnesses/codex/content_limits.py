"""Codex managed instruction block syntax."""

import re

BEGIN = "<!-- agent-brain-content:begin -->"
END = "<!-- agent-brain-content:end -->"


BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"(?:\r?\n)?", re.S)

"""Tool contract identity shared by plans and the running service."""

TOOL_CONTRACT_VERSION = 4
# Verified 0.8.x packages use the same descriptor envelope with different tools.
DESCRIBED_TOOL_CONTRACT_VERSIONS = (3, TOOL_CONTRACT_VERSION)

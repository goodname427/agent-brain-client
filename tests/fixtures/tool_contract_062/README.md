# Frozen tool-contract fixture

This is the exact public `adapters/codex/content_mcp.py` from
`client-0.6.2-windows`, commit `35961021114a04a1a5a68706249b10accdba85b3`.
SHA-256: `506fd93821989460b5243c0bb7a32821503630f5f8b9d582ec2cc218655cc530`.

It declares nine tools for a Memory/Rule/Skill read+put grant. Tests place it in
synthetic, separately verified packages to reproduce inspection by a newer
updater. It is never installed on a real device, used for credentials, or
shipped in runtime packages.

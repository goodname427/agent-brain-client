#!/bin/sh
# Repository entry; never reads invitation/token from shell or stdin.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
if [ "$(/usr/bin/uname -s)" != Darwin ] || [ "$(/usr/bin/uname -m)" != arm64 ]; then
    echo '{"status":"install-platform-review-required","installed":false}'
    exit 2
fi
# Standard local tools for Git HTTPS and compiling the existing Swift helper.
# This opens the normal tool installer only; restart this script when it finishes.
if ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
    /usr/bin/xcode-select --install >/dev/null 2>&1 || true
    echo '{"status":"install-tools-requested-rerun-after-completion","installed":false,"credential_accessed":false}'
    exit 2
fi
PYTHON=$(/usr/bin/xcrun --find python3)
exec "$PYTHON" -I -B "$HERE/tools/prepare_install_runtime.py" "$@"

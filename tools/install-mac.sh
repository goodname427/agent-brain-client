#!/bin/sh
# Repository entry; never reads invitation/token from shell or stdin.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
if [ "$(/usr/bin/uname -s)" != Darwin ] || [ "$(/usr/bin/uname -m)" != arm64 ]; then
    echo '{"status":"install-platform-review-required","installed":false}'
    exit 2
fi
# Only installation/reinstallation refreshes its source checkout. Local
# status, repair, uninstall and release-channel checks stay in this checkout.
if /usr/bin/python3 -I -B "$HERE/tools/bootstrap_main.py" --should-bootstrap "$@"; then
    # Standard local tools for Git HTTPS and compiling the existing Swift helper.
    # This opens the normal tool installer only; restart this script when it finishes.
    if ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
        /usr/bin/xcode-select --install >/dev/null 2>&1 || true
        echo '{"status":"install-tools-requested-rerun-after-completion","installed":false,"credential_accessed":false}'
        exit 2
    fi
    PYTHON=$(/usr/bin/xcrun --find python3)
    if ! CHECKOUT=$("$PYTHON" -I -B "$HERE/tools/bootstrap_main.py" --ensure); then
        echo '{"status":"official-main-checkout-review-required","installed":false,"native_changed":false}'
        exit 2
    fi
    if [ "$CHECKOUT" != "$HERE" ]; then
        exec /bin/sh "$CHECKOUT/tools/install-mac.sh" "$@"
    fi
    exec "$PYTHON" -I -B "$HERE/tools/prepare_install_runtime.py" "$@"
fi
# No Git/network checkout and no dependency preparation for local maintenance.
exec /usr/bin/python3 -I -B "$HERE/tools/prepare_install_runtime.py" "$@"

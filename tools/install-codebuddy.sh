#!/bin/sh
# Enroll/verify the shared Client once, then bind the CodeBuddy IDE and CLI.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
REQUESTED_ACTION=install
ACTION_NEXT=false
for ARGUMENT in "$@"; do
    if [ "$ACTION_NEXT" = true ]; then
        REQUESTED_ACTION=$ARGUMENT
        ACTION_NEXT=false
    elif [ "$ARGUMENT" = --action ]; then
        ACTION_NEXT=true
    fi
done
case "$REQUESTED_ACTION" in
    install|update) ;;
    *) echo '{"status":"codebuddy-action-required","next_action":"Use tools/codebuddy.py for status, probe or uninstall."}'; exit 2 ;;
esac
# The frozen Server device protocol keeps its enrolled adapter name. A fresh
# CodeBuddy installation uses a private shared backend, without the Codex app.
NATIVE_BACKEND=$(/usr/bin/python3 -I -B "$HERE/tools/codebuddy.py" backend-home)
EXISTING_BACKEND=false
if [ -f "$HOME/.config/agent-brain-client/codex-mcp.json" ]; then EXISTING_BACKEND=true; fi
CODEX_HOME="$NATIVE_BACKEND" sh "$HERE/tools/install-mac.sh" "$@"
if [ "$EXISTING_BACKEND" = true ] && [ "$REQUESTED_ACTION" = install ]; then
    # Repeating the shared install only verifies registration. Refresh the
    # existing approved runtime before using the newly shared 0.7 modules.
    CODEX_HOME="$NATIVE_BACKEND" sh "$HERE/tools/install-mac.sh" --action check-update
    CODEX_HOME="$NATIVE_BACKEND" sh "$HERE/tools/install-mac.sh" --action update
fi
exec /usr/bin/python3 -I -B "$HERE/tools/codebuddy.py" install

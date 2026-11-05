#!/bin/sh
# Enroll/verify the shared Client once, then bind the CodeBuddy IDE and CLI.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
# The frozen Server device protocol keeps its enrolled adapter name. A fresh
# CodeBuddy installation uses a private shared backend, without the Codex app.
NATIVE_BACKEND=$(/usr/bin/python3 -I -B "$HERE/tools/codebuddy.py" backend-home)
CODEX_HOME="$NATIVE_BACKEND" sh "$HERE/tools/install-mac.sh" "$@"
exec /usr/bin/python3 -I -B "$HERE/tools/codebuddy.py" install

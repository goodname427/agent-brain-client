#!/bin/sh
# Compatibility entry for previously published CodeBuddy commands.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec /bin/sh "$HERE/install.sh" --codebuddy-entry "$@"

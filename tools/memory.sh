#!/bin/sh
# Reuse the installed Python; never download or request new credentials.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
exec /usr/bin/python3 -I -B -c '
import os,sys
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,str(root/"tools"))
from prepare_install_runtime import runtime
try:
    python=runtime(Path.home(),prepare=False)
except Exception:
    print("{\"status\":\"existing-client-python-required\",\"installed\":false}")
    raise SystemExit(2)
entry="global_memory.py" if len(sys.argv)>2 and sys.argv[2].startswith("global-") else "memory_automation.py"
os.execv(str(python),[str(python),"-I","-B",str(root/"tools"/entry),*sys.argv[2:]])
' "$HERE" "$@"

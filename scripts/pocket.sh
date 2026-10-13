#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTIVATE="$REPO/.build/activate.sh"

usage() {
    cat <<'EOF'
Usage: ./scripts/pocket.sh <client|backend|admin|setup-admin>

  client       Start the Pocket web client at http://127.0.0.1:5176/pocket/
  backend      Start the Hermes API backend at http://127.0.0.1:9119/
  admin        Start Pocket Admin at http://127.0.0.1:9122/
  setup-admin  Set or reset the Pocket Admin password
EOF
}

if [[ $# -ne 1 ]]; then
    usage >&2
    exit 2
fi

if [[ ! -f "$ACTIVATE" ]]; then
    printf 'Pocket dev environment is missing: %s\nRun ./build-local.sh first.\n' "$ACTIVATE" >&2
    exit 1
fi

source "$ACTIVATE"
cd "$REPO"

resolve_app_python() {
    "$__hermes_boot_py" -c '
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from pm.environments import committed_venv, venv_bin_dir
venv = committed_venv(Path(sys.argv[1]))
if venv is None:
    raise SystemExit("PM-managed Hermes Python environment was not found")
bin_dir = venv_bin_dir(venv)
for name in ("python3", "python"):
    candidate = bin_dir / name
    if candidate.is_file():
        print(candidate)
        break
else:
    raise SystemExit(f"Python executable was not found in {bin_dir}")
' "$REPO"
}

require_admin_auth() {
    if [[ ! -f "${HERMES_HOME:?}/pocket-admin-auth.json" ]]; then
        printf 'Pocket Admin is not initialized. Run ./scripts/pocket.sh setup-admin first.\n' >&2
        exit 1
    fi
}

case "$1" in
    client)
        export POCKET_BACKEND="${POCKET_BACKEND:-http://127.0.0.1:9119}"
        exec npm run dev --workspace @hermes/pocket -- --host 127.0.0.1
        ;;
    backend)
        exec hermes serve
        ;;
    admin)
        require_admin_auth
        export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
        app_python="$(resolve_app_python)"
        exec "$app_python" \
            "$REPO/apps/pocket-admin/server.py" --port 9122
        ;;
    setup-admin)
        export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
        app_python="$(resolve_app_python)"
        exec "$app_python" \
            "$REPO/apps/pocket-admin/server.py" --setup
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
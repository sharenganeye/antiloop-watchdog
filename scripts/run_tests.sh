#!/usr/bin/env bash
# Creates ./.venv if needed, installs the dev requirement (pytest) INSIDE it, runs the suite.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ -x .venv/bin/python ] || "${PYTHON:-python3}" -m venv .venv
.venv/bin/python -m pip install -q -r requirements-dev.txt
exec .venv/bin/python -m pytest "$@"

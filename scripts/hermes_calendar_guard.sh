#!/usr/bin/env bash
set -euo pipefail

export TZ=Asia/Taipei
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VENV_PY="${HERMES_PYTHON:-$HERMES_HOME/hermes-agent/venv/bin/python}"

if [[ ! -x "$VENV_PY" ]]; then
  VENV_PY="${PYTHON:-python3}"
fi

exec "$VENV_PY" -m hermes_cli.calendar_guard --check

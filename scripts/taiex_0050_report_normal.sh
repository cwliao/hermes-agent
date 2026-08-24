#!/usr/bin/env bash
set -euo pipefail
script_dir="${HERMES_HOME:-$HOME/.hermes}/scripts"
exec "$script_dir/taiex_0050_report.sh" normal

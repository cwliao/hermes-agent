#!/bin/bash
# Wires Claude Code Remote sessions on this repo into Keven Liao's central-brain
# instruction/skill library (https://github.com/cwliao/central-brain), the same way
# it is wired into his personal machines (see that repo's SETUP.md / TOPOLOGY.md).
#
# Runs only in Claude Code Remote/web sessions, where each session starts from a
# fresh container, so the clone and the CLAUDE.md import both need to be redone
# every time rather than once.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

CB_DIR="$HOME/central-brain"
CLAUDE_MD="$HOME/.claude/CLAUDE.md"

if [ -d "$CB_DIR/.git" ]; then
  git -C "$CB_DIR" pull --ff-only --quiet || echo "warning: central-brain pull failed, using existing clone" >&2
else
  rm -rf "$CB_DIR"
  if ! git clone --quiet --depth 1 https://github.com/cwliao/central-brain "$CB_DIR"; then
    echo "warning: could not clone central-brain, skipping wiring for this session" >&2
    exit 0
  fi
fi

mkdir -p "$HOME/.claude"
touch "$CLAUDE_MD"

for line in "@$CB_DIR/AGENTS.md" "@$CB_DIR/overlays/claude-runtime.md"; do
  if ! grep -qxF "$line" "$CLAUDE_MD"; then
    printf '%s\n' "$line" >> "$CLAUDE_MD"
  fi
done

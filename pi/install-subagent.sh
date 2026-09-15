#!/usr/bin/env bash
# Install the official pi subagent extension from the installed
# @earendil-works/pi-coding-agent package, using pi's own documented symlink
# method. The extension and workflow prompts stay symlinks into the npm
# install (they track pi updates); sample agent definitions become local
# copies with any pinned `model:` line stripped so every subagent inherits
# the session model instead of an unauthenticated pinned model.
set -euo pipefail

PI_PKG_BASE="${PI_PKG_BASE:-$(pi --version >/dev/null 2>&1 && npm root -g 2>/dev/null)/@earendil-works/pi-coding-agent/examples/extensions/subagent}"
pi_home="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"

[[ -f "$PI_PKG_BASE/index.ts" ]] || { echo "subagent: extension source not found at $PI_PKG_BASE" >&2; exit 1; }

mkdir -p "$pi_home/extensions/subagent" "$pi_home/agents" "$pi_home/prompts"
ln -sfn "$PI_PKG_BASE/index.ts" "$pi_home/extensions/subagent/index.ts"
ln -sfn "$PI_PKG_BASE/agents.ts" "$pi_home/extensions/subagent/agents.ts"
for f in "$PI_PKG_BASE"/agents/*.md; do
  dst="$pi_home/agents/$(basename "$f")"
  if [[ ! -e "$dst" ]]; then
    sed '/^model: /d' "$f" > "$dst"
  fi
done
for f in "$PI_PKG_BASE"/prompts/*.md; do
  ln -sfn "$f" "$pi_home/prompts/$(basename "$f")"
done

echo "subagent: ok — extension, sample agents, and workflow prompts installed"
echo "  note: restart pi so the extension and agents are discovered"

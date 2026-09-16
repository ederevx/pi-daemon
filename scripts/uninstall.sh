#!/usr/bin/env bash
# Uninstall the Pi background service: remove manifest-owned files, stop and
# disable the unit, and drop now-empty directories. Never touches unrelated
# files.
set -euo pipefail

pi_home="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
manifest="$pi_home/.pi-daemon/manifest.json"

if [[ ! -f "$manifest" ]]; then
  echo "uninstall: no manifest at $manifest — nothing installed by this repo" >&2
  exit 1
fi

systemctl --user disable --now pi-daemon.service 2>/dev/null || true
systemctl --user daemon-reload

mapfile -t owned < <(python3 - "$manifest" <<'EOF'
import json, sys
for path in json.load(open(sys.argv[1]))["owned"]:
    print(path)
EOF
)

for path in "${owned[@]}"; do
  if [[ -f "$path" || -L "$path" ]]; then
    rm -f "$path"
    echo "uninstall: removed $path"
  else
    echo "uninstall: missing (skipped) $path"
  fi
  # Drop the directory when this uninstall emptied it.
  dir="$(dirname "$path")"
  rmdir "$dir" 2>/dev/null || true
done

rm -f "$manifest"
rmdir "$pi_home/.pi-daemon" 2>/dev/null || true
# Legacy: older installs owned a tmux.conf copy here.
rmdir "$HOME/.config/pi-daemon" 2>/dev/null || true
echo "uninstall: ok"

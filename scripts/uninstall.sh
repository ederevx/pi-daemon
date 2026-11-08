#!/usr/bin/env bash
# Uninstall the Pi background service: remove manifest-owned files, stop and
# disable the unit, and drop now-empty directories. Never touches unrelated
# files.
#
# A v3 manifest owns public files plus one published generation (and any
# earlier generation still retained). Only recorded files are removed, then
# their directories when empty: a manifest-provided root is never deleted
# recursively, so a directory holding anything this repo did not install
# survives.
set -euo pipefail

pi_home="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
manifest="$pi_home/.pi-daemon/manifest.json"

if [[ ! -f "$manifest" ]]; then
  echo "uninstall: no manifest at $manifest — nothing installed by this repo" >&2
  exit 1
fi

systemctl --user disable --now pi-daemon.service 2>/dev/null || true
systemctl --user daemon-reload 2>/dev/null || true

mapfile -t recorded < <(python3 - "$manifest" <<'EOF'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
seen = []
for path in document.get("owned") or []:
    seen.append(path)
for tree, files in sorted((document.get("trees") or {}).items()):
    for relative in sorted(files or {}):
        seen.append(tree.rstrip("/") + "/" + relative)
for tree in document.get("retained") or []:
    # An earlier generation is fully described by the manifest that
    # published it; this record only knows the tree root, so its files are
    # listed from disk and removed one by one.
    for base, _directories, files in os.walk(tree):
        for name in sorted(files):
            seen.append(os.path.join(base, name))
for path in seen:
    print(path)
EOF
)

for path in "${recorded[@]}"; do
  if [[ -f "$path" || -L "$path" ]]; then
    rm -f "$path"
    echo "uninstall: removed $path"
  fi
done

# Drop the directories this uninstall emptied, in reverse path order so a
# child goes before its parent. A directory that still holds a foreign file
# stays, and rmdir never recurses.
mapfile -t directories < <(for path in "${recorded[@]}"; do
  printf '%s\n' "$(dirname "$path")"
  printf '%s\n' "$(dirname "$(dirname "$path")")"
done | LC_ALL=C sort -ru)
for dir in "${directories[@]}"; do
  rmdir "$dir" 2>/dev/null || true
done

rm -f "$manifest"
rmdir "$pi_home/.pi-daemon" 2>/dev/null || true
# Legacy: older installs owned a tmux.conf copy here.
rmdir "$HOME/.config/pi-daemon" 2>/dev/null || true
echo "uninstall: ok"
